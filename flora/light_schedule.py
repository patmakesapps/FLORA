"""Timed light changes, saved in data/light_schedules.json so they survive restarts.

Timers run inside `flora run` (the service) whether or not a conversation is
active. A timer that comes due while FLORA is not running fires on startup if
it is at most LATE_LIMIT late; otherwise a one-time timer is dropped and a
daily one waits for its next day.
"""
import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .config import ROOT
from .lights import LightsError

log = logging.getLogger(__name__)
SCHEDULE_PATH = ROOT / "data" / "light_schedules.json"
CHECK_SECONDS = 15
LATE_LIMIT = timedelta(minutes=10)
MAX_SCHEDULES = 50
CLOCK = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
SETTINGS = ("power", "brightness_percent", "color", "color_temperature_k")


def local_now():
    return datetime.now().astimezone()


def next_occurrence(clock, after):
    """The next local wall-clock HH:MM strictly after `after`, across DST changes."""
    hour, minute = map(int, clock.split(":"))
    day = after.date()
    while True:
        candidate = datetime(day.year, day.month, day.day, hour, minute).astimezone()
        if candidate > after:
            return candidate
        day += timedelta(days=1)


class LightSchedule:
    def __init__(self, lights, path=SCHEDULE_PATH, now=local_now):
        self.lights = lights
        self.path = Path(path)
        self.now = now
        self._lock = threading.Lock()

    def _load(self):
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise LightsError("schedule_unreadable") from exc
        if not isinstance(saved, list):
            raise LightsError("schedule_unreadable")
        return saved

    def _save(self, schedules):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(schedules, indent=2))
        os.replace(temporary, self.path)

    @staticmethod
    def _public(entry):
        return {"schedule_id": entry["id"], "target": entry["target"], "repeat": entry["repeat"],
                "next_run_local": entry["due"], **entry["settings"]}

    def add(self, target, at=None, in_minutes=None, repeat="once", **settings):
        if repeat not in {"once", "daily"}:
            raise ValueError("repeat must be once or daily")
        if (at is None) == (in_minutes is None):
            raise LightsError("give_at_or_in_minutes")
        if at is not None and (not isinstance(at, str) or not CLOCK.fullmatch(at)):
            raise ValueError("at must be HH:MM")
        if in_minutes is not None and (type(in_minutes) is not int or not 1 <= in_minutes <= 1440):
            raise ValueError("in_minutes must be 1-1440")
        if repeat == "daily" and at is None:
            raise LightsError("daily_needs_at")
        settings = {key: value for key, value in settings.items() if value is not None}
        if set(settings) - set(SETTINGS):
            raise ValueError("unknown light setting")
        self.lights.plan(**settings)  # Reject a bad change now, not when it fires.
        names = self.lights.matching(target)
        if not names:
            return {"error": "no_matching_light", "available": self.lights.matching("all")}

        now = self.now()
        due = next_occurrence(at, now) if at else now + timedelta(minutes=in_minutes)
        with self._lock:
            schedules = self._load()
            if len(schedules) >= MAX_SCHEDULES:
                raise LightsError("too_many_schedules")
            entry = {"id": max((item["id"] for item in schedules), default=0) + 1,
                     "target": target.strip(), "settings": settings, "repeat": repeat,
                     "at": at, "due": due.isoformat(timespec="seconds")}
            schedules.append(entry)
            self._save(schedules)
        return {"scheduled": self._public(entry), "lights_now_matching": names}

    def list(self):
        with self._lock:
            schedules = sorted(self._load(), key=lambda entry: entry["due"])
        return {"schedules": [self._public(entry) for entry in schedules], "now_local": self.now().isoformat(
            timespec="seconds")}

    def cancel(self, schedule_id):
        if type(schedule_id) is not int:
            raise ValueError("schedule_id must be an integer")
        with self._lock:
            schedules = self._load()
            remaining = [entry for entry in schedules if entry["id"] != schedule_id]
            if len(remaining) == len(schedules):
                return {"error": "no_such_schedule"}
            self._save(remaining)
        return {"cancelled": schedule_id}

    def run_due(self):
        """Fire every timer that has come due; returns the light results for logging and tests."""
        now = self.now()
        with self._lock:
            schedules = self._load()
            fire, keep = [], []
            for entry in schedules:
                due = datetime.fromisoformat(entry["due"])
                if due > now:
                    keep.append(entry)
                    continue
                if now - due <= LATE_LIMIT:
                    fire.append(entry)
                else:
                    log.warning("Skipped light timer %s: %s late", entry["id"], now - due)
                if entry["repeat"] == "daily":
                    keep.append({**entry, "due": next_occurrence(entry["at"], now).isoformat(timespec="seconds")})
            if keep != schedules:
                self._save(keep)
        results = []
        for entry in fire:
            try:
                result = self.lights.set(entry["target"], **entry["settings"])
            except (LightsError, OSError, ValueError) as exc:
                result = {"error": getattr(exc, "code", type(exc).__name__)}
            outcomes = result.get("error") or [light["result"] for light in result["lights"]]
            log.info("Light timer %s fired: %s", entry["id"], outcomes)
            results.append((entry["id"], result))
        return results

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.run_due)
            except Exception as exc:
                log.warning("Light timer check failed (%s)", type(exc).__name__)
            await asyncio.sleep(CHECK_SECONDS)
