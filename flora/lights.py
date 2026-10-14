"""Govee lamps on the local network, through Govee's LAN API. No cloud account or key.

Each lamp needs "LAN Control" switched on in the Govee Home app. Lamps answer
discovery and status requests on UDP port 4002, so only one socket may listen
there at a time. Saved lamp names live in data/lights.json, keyed by the
lamp's Govee device ID: router-assigned IP addresses change, device IDs do not.
"""
import json
import logging
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import ROOT
from .sensors import timestamp

log = logging.getLogger(__name__)
REGISTRY_PATH = ROOT / "data" / "lights.json"
MULTICAST = ("239.255.255.250", 4001)
REPLY_PORT = 4002
CONTROL_PORT = 4003
SCAN_SECONDS = 2.0
STATUS_SECONDS = 0.8
SETTLE_SECONDS = 0.4  # Lamps report their previous state for a moment after a command.
STATUS_ATTEMPTS = 3
KELVIN_RANGE = (2000, 9000)
ALL_LIGHTS = {"all", "all lights", "every light", "everything", "lights", "the lights"}
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _locked(method):
    """The voice tools and the timer thread share one lamp list file."""
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    wrapper.__name__, wrapper.__doc__ = method.__name__, method.__doc__
    return wrapper


class LightsError(RuntimeError):
    """A safe-to-report failure; `code` is returned to the model."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def normalize(name):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(name).lower()).split())


def parse_color(value):
    if not isinstance(value, str) or not HEX_COLOR.fullmatch(value):
        raise ValueError("color must be #RRGGBB")
    return {"r": int(value[1:3], 16), "g": int(value[3:5], 16), "b": int(value[5:7], 16)}


def describe_state(state):
    """Translate a devStatus payload into the units the tools speak."""
    if not isinstance(state, dict):
        return None
    color = state.get("color") if isinstance(state.get("color"), dict) else {}
    kelvin = state.get("colorTemInKelvin")
    return {
        "power": {1: "on", 0: "off"}.get(state.get("onOff")),
        "brightness_percent": state.get("brightness"),
        # A lamp in white mode reports color 0,0,0, which is not "black".
        "color": "#{:02x}{:02x}{:02x}".format(*(int(color.get(c, 0)) for c in "rgb"))
        if color and not kelvin else None,
        "color_temperature_k": kelvin or None,
    }


def matches(state, expected):
    if not isinstance(state, dict):
        return False
    if "onOff" in expected and state.get("onOff") != expected["onOff"]:
        return False
    if "brightness" in expected and state.get("brightness") != expected["brightness"]:
        return False
    if "colorTemInKelvin" in expected and abs(
            (state.get("colorTemInKelvin") or 0) - expected["colorTemInKelvin"]) > 100:
        return False
    if "color" in expected:
        color = state.get("color") if isinstance(state.get("color"), dict) else {}
        if any(abs(int(color.get(c, -99)) - expected["color"][c]) > 2 for c in "rgb"):
            return False
    return True


class UdpTransport:
    """Blocking UDP; call from a worker thread."""
    def __init__(self):
        self._lock = threading.Lock()

    @staticmethod
    def _packet(cmd, data):
        return json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()

    @contextmanager
    def _listener(self):
        with self._lock:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                try:
                    sock.bind(("", REPLY_PORT))
                except OSError as exc:
                    raise LightsError("reply_port_in_use") from exc
                yield sock
            finally:
                sock.close()

    @staticmethod
    def replies(sock, seconds):
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                raw, (ip, _) = sock.recvfrom(4096)
            except TimeoutError:
                return
            except ConnectionResetError:
                continue  # Windows surfaces ICMP port-unreachable from an earlier send here.
            try:
                msg = json.loads(raw)["msg"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(msg, dict) and isinstance(msg.get("data"), dict):
                yield ip, msg.get("cmd"), msg["data"]

    def scan(self, seconds):
        with self._listener() as sock:
            sock.sendto(self._packet("scan", {"account_topic": "reserve"}), MULTICAST)
            return [(ip, data) for ip, cmd, data in self.replies(sock, seconds) if cmd == "scan"]

    def send(self, ip, cmd, data):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.sendto(self._packet(cmd, data), (ip, CONTROL_PORT))

    def status(self, ip, seconds):
        with self._listener() as sock:
            sock.sendto(self._packet("devStatus", {}), (ip, CONTROL_PORT))
            for reply_ip, cmd, data in self.replies(sock, seconds):
                if reply_ip == ip and cmd == "devStatus":
                    return data
        return None


class Lights:
    def __init__(self, path=REGISTRY_PATH, transport=None, settle=SETTLE_SECONDS):
        self.path = Path(path)
        self.transport = transport or UdpTransport()
        self.settle = settle
        self._lock = threading.RLock()

    # Saved lamps

    def _load(self):
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Never overwrite a file we could not read; the user's names live there.
            raise LightsError("light_list_unreadable") from exc
        if not isinstance(saved, dict) or not all(
                isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in saved.values()):
            raise LightsError("light_list_unreadable")
        return saved

    def _save(self, lights):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(lights, indent=2, sort_keys=True))
        os.replace(temporary, self.path)

    @staticmethod
    def _unused_name(lights):
        names = {normalize(entry["name"]) for entry in lights.values()}
        number = 1
        while f"light {number}" in names:
            number += 1
        return f"light {number}"

    def _resolve(self, lights, target):
        """A lamp by exact name, device ID or IP, else every lamp whose name contains the words."""
        if not isinstance(target, str) or not normalize(target):
            raise ValueError("target must name a light")
        wanted = normalize(target)
        if wanted in ALL_LIGHTS:
            return list(lights.items())
        exact = [(device, entry) for device, entry in lights.items()
                 if wanted == normalize(entry["name"]) or target.strip().upper() == device.upper()
                 or target.strip() == entry.get("ip")]
        if exact:
            return exact
        return [(device, entry) for device, entry in lights.items()
                if f" {wanted} " in f" {normalize(entry['name'])} "]

    def _saved(self):
        """Saved lamps, scanning first on a fresh install so commands work before any setup."""
        return self._load() or self.discover()[0]

    def _no_match(self, lights):
        return {"error": "no_matching_light", "available": sorted(entry["name"] for entry in lights.values())}

    # Network

    @_locked
    def discover(self, seconds=SCAN_SECONDS):
        """Scan, save new lamps under placeholder names, and refresh changed IP addresses."""
        lights = self._load()
        seen = set()
        for ip, data in self.transport.scan(seconds):
            device = data.get("device")
            if not isinstance(device, str) or not device.strip():
                continue
            device = device.strip()
            seen.add(device)
            entry = lights.setdefault(device, {"name": self._unused_name(lights)})
            entry.update(ip=data.get("ip") or ip, sku=data.get("sku"), last_seen=timestamp())
        if seen:
            self._save(lights)
        return lights, seen

    def _confirm(self, ip, expected):
        state = None
        for _ in range(STATUS_ATTEMPTS):
            state = self.transport.status(ip, STATUS_SECONDS)
            if state is None or matches(state, expected):
                break
            time.sleep(self.settle)
        return state

    def _send_all(self, entries, commands):
        for _, entry in entries:
            for cmd, data in commands:
                self.transport.send(entry["ip"], cmd, data)
        time.sleep(self.settle)

    def _outcome(self, entry, state, expected):
        result = {"name": entry["name"], "reported": describe_state(state)}
        if state is None:
            result["result"] = "sent_unconfirmed"
        else:
            result["result"] = "confirmed" if matches(state, expected) else "state_differs"
        return result

    # Operations

    @_locked
    def report(self):
        lights, seen = self.discover()
        report = []
        for device, entry in sorted(lights.items(), key=lambda item: normalize(item[1]["name"])):
            state = self.transport.status(entry["ip"], STATUS_SECONDS) if device in seen else None
            report.append({"name": entry["name"], "device": device, "ip": entry.get("ip"),
                           "reachable": device in seen, "state": describe_state(state)})
        return report

    @staticmethod
    def plan(power=None, brightness_percent=None, color=None, color_temperature_k=None):
        """Validate a requested change and return its (commands, expected state)."""
        if power not in {None, "on", "off"}:
            raise ValueError("power must be on or off")
        if brightness_percent is not None and (type(brightness_percent) is not int
                                               or not 1 <= brightness_percent <= 100):
            raise ValueError("brightness_percent must be 1-100")
        if color_temperature_k is not None and (type(color_temperature_k) is not int
                                                or not KELVIN_RANGE[0] <= color_temperature_k <= KELVIN_RANGE[1]):
            raise ValueError("color_temperature_k out of range")
        rgb = parse_color(color) if color is not None else None
        settings = (brightness_percent, rgb, color_temperature_k)
        if power is None and all(value is None for value in settings):
            raise LightsError("nothing_to_change")
        if power == "off" and any(value is not None for value in settings):
            raise LightsError("cannot_adjust_and_turn_off")
        if rgb is not None and color_temperature_k is not None:
            raise LightsError("choose_color_or_temperature")

        if power == "off":
            return [("turn", {"value": 0})], {"onOff": 0}
        # Adjusting a lamp that is off implies turning it on.
        commands, expected = [("turn", {"value": 1})], {"onOff": 1}
        if brightness_percent is not None:
            commands.append(("brightness", {"value": brightness_percent}))
            expected["brightness"] = brightness_percent
        if rgb is not None:
            commands.append(("colorwc", {"color": rgb, "colorTemInKelvin": 0}))
            expected["color"] = rgb
        if color_temperature_k is not None:
            commands.append(("colorwc", {"color": {"r": 255, "g": 255, "b": 255},
                                         "colorTemInKelvin": color_temperature_k}))
            expected["colorTemInKelvin"] = color_temperature_k
        return commands, expected

    @_locked
    def matching(self, target):
        """Names of the saved lamps a target would change."""
        return sorted(entry["name"] for _, entry in self._resolve(self._saved(), target))

    @_locked
    def set(self, target, power=None, brightness_percent=None, color=None, color_temperature_k=None):
        try:
            commands, expected = self.plan(power, brightness_percent, color, color_temperature_k)
        except LightsError as exc:
            return {"error": exc.code}
        lights = self._saved()
        targets = [(device, entry) for device, entry in self._resolve(lights, target) if entry.get("ip")]
        if not targets:
            return self._no_match(lights)
        self._send_all(targets, commands)
        states = {device: self._confirm(entry["ip"], expected) for device, entry in targets}

        silent = [device for device, state in states.items() if state is None]
        if silent:
            # The router may have given a silent lamp a new address since it was saved.
            old = {device: lights[device].get("ip") for device in silent}
            lights, _ = self.discover()
            moved = [(device, lights[device]) for device in silent
                     if device in lights and lights[device].get("ip") != old[device]]
            if moved:
                self._send_all(moved, commands)
                states.update({device: self._confirm(entry["ip"], expected) for device, entry in moved})

        return {"lights": [self._outcome(lights[device], states[device], expected) for device, _ in targets]}

    @_locked
    def identify(self, target, flashes=3):
        """Flash matching lamps so a person can tell them apart, then restore their power."""
        lights = self._saved()
        targets = [(device, entry) for device, entry in self._resolve(lights, target) if entry.get("ip")]
        if not targets:
            return self._no_match(lights)
        before = {device: self.transport.status(entry["ip"], STATUS_SECONDS) for device, entry in targets}
        for _ in range(flashes):
            for value in (0, 1):
                for _, entry in targets:
                    self.transport.send(entry["ip"], "turn", {"value": value})
                time.sleep(0.8)
        for device, entry in targets:
            if (before[device] or {}).get("onOff") == 0:
                self.transport.send(entry["ip"], "turn", {"value": 0})
        return {"flashed": [entry["name"] for _, entry in targets]}

    @_locked
    def rename(self, target, new_name):
        if not isinstance(new_name, str) or not 1 <= len(new_name.strip()) <= 60 \
                or not normalize(new_name) or normalize(new_name) in ALL_LIGHTS:
            raise ValueError("new name must be 1-60 characters and not a group word")
        lights = self._saved()
        targets = self._resolve(lights, target)
        if len(targets) != 1:
            return {"error": "ambiguous_light" if targets else "no_matching_light",
                    "available": sorted(entry["name"] for entry in lights.values())}
        device, entry = targets[0]
        if any(normalize(other["name"]) == normalize(new_name)
               for key, other in lights.items() if key != device):
            return {"error": "name_in_use"}
        old_name, entry["name"] = entry["name"], new_name.strip()
        self._save(lights)
        return {"renamed": old_name, "to": entry["name"]}
