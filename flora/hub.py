import asyncio
import base64
import logging

from .audio import Audio, Playback
from .audio_adapter import ModelToPiAudio, PiToModelAudio
from .battery import Battery
from .leds import Ring
from .realtime import Realtime

log = logging.getLogger(__name__)


def indicator_state(speaking, responding, hearing):
    if speaking:
        return "speaking"
    if responding:
        return "thinking"
    if hearing:
        return "listening"
    return "idle"



class Session:
    def __init__(self, config, ring):
        self.audio = Audio(config.alsa_device, config.speaker_gain, config.mic_capture_volume)
        self.client = Realtime(config)
        self.playback = Playback(self.audio)
        self.input_adapter = PiToModelAudio()
        self.output_adapter = ModelToPiAudio()
        self.ring = ring
        self.hearing = False
        self.responding = False

    async def microphone(self):
        while True:
            audio = await self.audio.read()
            # No echo canceller on this HAT. Feed silence during playback and
            # its acoustic tail, continuing to drain capture so no stale audio returns.
            if self.playback.speaking:
                audio = bytes(len(audio))
            converted = self.input_adapter.convert(audio)
            if converted:
                await self.client.send_audio(converted)

    async def indicators(self):
        while True:
            self.ring.state = indicator_state(self.playback.speaking, self.responding, self.hearing)
            await asyncio.sleep(0.05)

    async def events(self):
        async for event in self.client.events():
            kind = event.get("type")
            if kind == "input_audio_buffer.speech_started":
                self.hearing = True
            elif kind == "input_audio_buffer.speech_stopped":
                self.hearing = False
                self.responding = True
            elif kind == "response.created":
                self.responding = True
                self.output_adapter = ModelToPiAudio()
            elif kind == "response.output_audio.delta":
                pcm = base64.b64decode(event["delta"], validate=True)
                self.playback.push(self.output_adapter.convert(pcm))
            elif kind == "response.output_audio.done":
                self.playback.push(self.output_adapter.convert(b"", final=True))
            elif kind == "response.done":
                self.responding = False
                response = event.get("response", {})
                if response.get("status") == "failed":
                    raise RuntimeError("Realtime response failed")
            elif kind == "error":
                code = event.get("error", {}).get("code", "unknown")
                # Log the error code only; server messages can contain user input.
                raise RuntimeError("Realtime error: " + str(code))
        raise ConnectionError("Realtime connection closed")

    async def run(self):
        tasks = []
        try:
            self.ring.state = "connecting"
            await self.client.open()
            await self.audio.start()
            log.info("Ready. Speak to Flora; mic pauses during speaker playback. Ctrl+C to stop.")
            tasks = [asyncio.create_task(fn()) for fn in (
                self.microphone, self.events, self.playback.run, self.indicators
            )]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Both resources must close even if one cleanup encounters an error.
            await asyncio.gather(self.audio.close(), self.client.close(), return_exceptions=True)


async def run_hub(config):
    if not config.api_key:
        raise ValueError("Set OPENAI_API_KEY in this repo's .env before starting voice")
    ring = Ring(config)
    animation = asyncio.create_task(ring.run())
    battery_task = asyncio.create_task(Battery(config.battery_enabled).run(ring))
    try:
        retry_delay = 2
        while True:
            try:
                await Session(config, ring).run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never log connection URLs, headers, or configuration containing keys.
                log.warning("Session stopped (%s). Retrying in %ss.", type(exc).__name__, retry_delay)
                ring.state = "error"
                await asyncio.sleep(retry_delay)
                retry_delay = min(30, retry_delay * 2)
    finally:
        animation.cancel()
        battery_task.cancel()
        await asyncio.gather(animation, battery_task, return_exceptions=True)
        ring.clear()
