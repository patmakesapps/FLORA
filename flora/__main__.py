import argparse
import asyncio
import base64
import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .config import Config, ROOT


async def led_test(config):
    from .leds import Ring
    ring = Ring(config)
    if ring._write is None:
        raise RuntimeError("LED driver unavailable")
    task = asyncio.create_task(ring.run())
    try:
        for state in ("connecting", "idle", "listening", "thinking", "speaking"):
            print(f"LED: {state} (6 seconds, channel maximum {config.led_max_channel}/255)", flush=True)
            ring.state = state
            await asyncio.sleep(6)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ring.clear()
        print("LEDs off.", flush=True)


async def audio_test(config):
    import numpy as np
    from .audio import RATE, BYTES_PER_SECOND, CHUNK_BYTES, open_audio
    audio = open_audio(config)
    try:
        await audio.start()
        print("Recording microphone levels for 3 seconds. Speak near the HAT.", flush=True)
        chunks = [await audio.read() for _ in range(150)]
        samples = np.frombuffer(b"".join(chunks), dtype="<i2").astype(np.float64)
        rms = np.sqrt(np.mean(samples ** 2, axis=0))
        peak = np.max(np.abs(samples))
        print(f"Microphone RMS={rms:.0f}, peak={peak:.0f} / 32768. Audio was not saved.", flush=True)
        clipped = np.mean(np.abs(samples) >= 32767) * 100
        print(f"Clipped samples: {clipped:.2f}% (mic capture setting {config.mic_capture_volume}/63)", flush=True)
        print("Playing a quiet half-second test tone.", flush=True)
        t = np.arange(RATE // 2) / RATE
        envelope = np.minimum(1, t / 0.025) * np.minimum(1, (0.5 - t) / 0.025)
        tone = (np.sin(t * 2 * np.pi * 440) * envelope * 2000).astype("<i2")
        pcm = np.repeat(tone[:, None], 2, axis=1).tobytes()
        for offset in range(0, len(pcm), CHUNK_BYTES):
            await audio.write(pcm[offset:offset + CHUNK_BYTES])
            await asyncio.sleep(CHUNK_BYTES / BYTES_PER_SECOND)
        await asyncio.sleep(0.3)
    finally:
        await audio.close()


async def voice_test(config):
    """One generated greeting, without sending microphone input."""
    from .audio import Playback, open_audio
    from .audio_adapter import ModelToPiAudio
    from .leds import Ring
    from .realtime import Realtime
    if not config.api_key:
        raise ValueError("Set OPENAI_API_KEY in .env first")
    client = Realtime(config)
    audio = open_audio(config)
    playback = Playback(audio)
    adapter = ModelToPiAudio()
    ring = Ring(config)
    tasks = []
    try:
        async with asyncio.timeout(30):
            await client.open()
            await audio.start()
            tasks = [asyncio.create_task(playback.run()), asyncio.create_task(ring.run())]
            ring.state = "thinking"
            await client.send({"type": "response.create", "response": {
                "instructions": "Say exactly: Hi, I'm Flora. My voice is ready.",
                "tool_choice": "none",
            }})
            async for event in client.events():
                kind = event.get("type")
                if kind == "response.output_audio.delta":
                    playback.push(adapter.convert(base64.b64decode(event["delta"], validate=True)))
                    ring.state = "speaking"
                elif kind == "response.output_audio.done":
                    playback.push(adapter.convert(b"", final=True))
                elif kind == "response.done":
                    if event.get("response", {}).get("status") != "completed":
                        raise RuntimeError("Voice preview response failed")
                    while playback.speaking:
                        for task in tasks:
                            if task.done():
                                task.result()
                        await asyncio.sleep(0.05)
                    print(f"Voice preview complete: {config.model} / {config.voice}", flush=True)
                    return
                elif kind == "error":
                    raise RuntimeError("Voice preview error: " + event.get("error", {}).get("code", "unknown"))
            raise RuntimeError("Voice preview connection closed early")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(audio.close(), client.close(), return_exceptions=True)
        ring.clear()


def check(config):
    print(f"Repo: {ROOT}")
    print(f"API key: {'configured' if config.api_key else 'missing (set OPENAI_API_KEY in .env)'}")
    print(f"Realtime: {config.model}, voice {config.voice}")
    print(f"Audio: {config.alsa_device}, software gain {config.speaker_gain}, "
          f"Speaker mixer {config.speaker_volume}/127")
    print(f"LEDs: GPIO{config.led_gpio}, {config.led_count} pixels, channel cap {config.led_max_channel}/255")
    for command in ("aplay", "arecord"):
        print(f"{command}: {shutil.which(command) or 'missing'}")
    print(f"PIO device: {'present' if Path('/dev/pio0').exists() else 'missing'}")
    print("Sensors: VEML7700 0x10, STEMMA soil 0x36, SHT41 0x44 (run sensor-test)")
    print(f"Lights: {'enabled' if config.lights_enabled else 'disabled'} (LIGHTS_ENABLED; list lamps with: lights)")
    from .waking import wake_word_ready
    problem = wake_word_ready(config)
    print(f"Wake word: {problem or 'ready, ' + config.wake_word_path.name}; "
          f"conversations end after {config.conversation_timeout:.0f}s of silence")
    print(f"Power off tool: {'enabled' if config.power_off_enabled else 'disabled'} (POWER_OFF_ENABLED)")
    for command in (["aplay", "-l"], ["arecord", "-l"]):
        subprocess.run(command, check=True)


async def sensor_test(config):
    """One live snapshot; no audio, LEDs, or API connection required."""
    from .sensors import Sensors
    from .tools import Tools
    sensors = Sensors(config.soil_probe_in_soil)
    try:
        status = await Tools(sensors).get_plant_status()
        print(json.dumps(status, indent=2, allow_nan=False), flush=True)
        return all(status[name]["status"] == "ok" for name in ("light", "soil", "environment"))
    finally:
        await sensors.close()


async def calibrate_soil(config, point):
    from .tools import Tools
    from .sensors import Sensors
    sensors = Sensors(config.soil_probe_in_soil)
    try:
        # Only a named physical condition asserts a label; sample is unlabelled.
        result = await Tools(sensors).execute("calibrate_soil", {
            "point": point, "confirmed": point in {"water_now", "watered"},
        })
        print(json.dumps(result, indent=2), flush=True)
        if "error" in result or result.get("saved") is False or result.get("measurement_available") is False:
            raise ValueError("Capture unavailable or not saved; see the result above")
    finally:
        await sensors.close()


def lights_command(args):
    """Setup over SSH: find lamps, flash one to tell them apart, then name it."""
    from .lights import Lights, LightsError
    lights = Lights()
    try:
        if args.command == "lights":
            report = lights.report()
            if not report:
                print("No lamps found. Turn on LAN Control for each lamp in the Govee Home app.")
            for light in report:
                state = light["state"] or {}
                print(f"{light['name']:<24} {light['device']:<24} {light['ip'] or '-':<16} "
                      f"{'reachable' if light['reachable'] else 'not answering':<14} "
                      f"{state.get('power') or '-'} {state.get('brightness_percent') or ''}")
            return
        if args.command == "lights-identify":
            result = lights.identify(args.target)
        elif args.command == "lights-rename":
            result = lights.rename(args.target, args.new_name)
        else:
            result = lights.set(args.target, args.power, args.brightness, args.color, args.kelvin)
    except LightsError as exc:
        raise RuntimeError(exc.code) from exc
    print(json.dumps(result, indent=2))
    if "error" in result:
        raise ValueError(result["error"])


def main():
    parser = argparse.ArgumentParser(description="Standalone Flora desk voice hub")
    commands = parser.add_subparsers(dest="command", metavar="command")
    for name in ("run", "check", "led-test", "audio-test", "voice-test", "sensor-test", "memory"):
        commands.add_parser(name)
    calibrate = commands.add_parser("calibrate-soil")
    calibrate.add_argument("point", choices=("sample", "water-now", "watered", "reset"),
                           help="unlabelled sample, ready-for-watering, watered/drained, or new setup")
    commands.add_parser("lights", help="find Govee lamps and list saved names, addresses and state")
    commands.add_parser("lights-identify", help="flash lamps to tell them apart").add_argument(
        "target", help="lamp name, device ID, IP address, or all")
    rename = commands.add_parser("lights-rename", help="name a lamp")
    rename.add_argument("target", help="current name, device ID, or IP address")
    rename.add_argument("new_name")
    adjust = commands.add_parser("lights-set", help="change lamps, as the set_lights tool would")
    adjust.add_argument("target")
    adjust.add_argument("--power", choices=("on", "off"))
    adjust.add_argument("--brightness", type=int, metavar="1-100")
    adjust.add_argument("--color", metavar="#RRGGBB")
    adjust.add_argument("--kelvin", type=int, metavar="2000-9000")
    args = parser.parse_args()
    args.command = args.command or "run"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load()
        if args.command.startswith("lights"):
            lights_command(args)
        elif args.command == "check":
            check(config)
        elif args.command == "led-test":
            asyncio.run(led_test(config))
        elif args.command == "audio-test":
            asyncio.run(audio_test(config))
        elif args.command == "voice-test":
            asyncio.run(voice_test(config))
        elif args.command == "sensor-test":
            if not asyncio.run(sensor_test(config)):
                parser.exit(1, "One or more sensors unavailable; see readings above.\n")
        elif args.command == "memory":
            from .memory import Memory
            print(json.dumps(Memory().recall(), indent=2))
        elif args.command == "calibrate-soil":
            asyncio.run(calibrate_soil(config, args.point.replace("-", "_")))
        else:
            from .hub import run_hub
            asyncio.run(run_hub(config))
    except KeyboardInterrupt:
        print("Flora stopped.")
    except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
