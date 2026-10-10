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
        for state in ("idle", "listening", "thinking", "speaking"):
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
    from .audio import Audio, RATE, BYTES_PER_SECOND, CHUNK_BYTES
    audio = Audio(config.alsa_device, config.speaker_gain, config.mic_capture_volume)
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
    from .audio import Audio, Playback
    from .audio_adapter import ModelToPiAudio
    from .leds import Ring
    from .realtime import Realtime
    if not config.api_key:
        raise ValueError("Set OPENAI_API_KEY in .env first")
    client = Realtime(config)
    audio = Audio(config.alsa_device, config.speaker_gain, config.mic_capture_volume)
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
    print(f"Audio: {config.alsa_device}, software gain {config.speaker_gain}")
    print(f"LEDs: GPIO{config.led_gpio}, {config.led_count} pixels, channel cap {config.led_max_channel}/255")
    for command in ("aplay", "arecord"):
        print(f"{command}: {shutil.which(command) or 'missing'}")
    print(f"PIO device: {'present' if Path('/dev/pio0').exists() else 'missing'}")
    print("Sensors: VEML7700 0x10, STEMMA soil 0x36, SHT41 0x44 (run sensor-test)")
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
        # Invoking this command explicitly asserts the named physical condition.
        result = await Tools(sensors).execute("calibrate_soil", {"point": point, "confirmed": True})
        print(json.dumps(result, indent=2), flush=True)
        if "error" in result:
            raise ValueError("Calibration not saved; see the reason above")
    finally:
        await sensors.close()


def main():
    parser = argparse.ArgumentParser(description="Standalone Flora desk voice hub")
    parser.add_argument("command", choices=("run", "check", "led-test", "audio-test", "voice-test", "sensor-test", "memory", "calibrate-soil"), nargs="?", default="run")
    parser.add_argument("point", choices=("water-now", "watered", "reset"), nargs="?",
                        help="calibrate-soil: actual soil ready for watering, watered/drained, or reset")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.load()
        if args.point and args.command != "calibrate-soil":
            raise ValueError("A calibration point is only valid with calibrate-soil")
        if args.command == "check":
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
            if not args.point:
                raise ValueError("Choose calibrate-soil water-now, watered, or reset")
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
