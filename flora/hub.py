import asyncio
import base64
import json
import logging

import numpy as np

from .audio import Audio, Playback, BYTES_PER_SECOND
from .errors import SessionError, error_code, failure_reason
from .audio_adapter import ModelToPiAudio, PiToModelAudio
from .leds import Ring
from .light_schedule import LightSchedule
from .lights import Lights
from .tools import Tools
from .realtime import Realtime
from .sensors import Sensors

log = logging.getLogger(__name__)
RESPONSE_STALL_TIMEOUT = 60.0
HEALTH_INTERVAL = 20.0


def indicator_state(speaking, responding, hearing):
    if speaking:
        return "speaking"
    if responding:
        return "thinking"
    if hearing:
        return "listening"
    return "idle"



class Session:
    def __init__(self, config, ring, sensors=None, memory=None, lights=None, schedule=None):
        self.audio = Audio(config.alsa_device, config.speaker_gain, config.mic_capture_volume)
        self.client = Realtime(config)
        self.playback = Playback(self.audio)
        self.input_adapter = PiToModelAudio()
        self.output_adapter = ModelToPiAudio()
        self.ring = ring
        self.hearing = False
        self.responding = False
        self.sensors = sensors if sensors is not None else Sensors(config.soil_probe_in_soil)
        self._owns_sensors = sensors is None
        self.tool_runner = Tools(self.sensors, memory, lights, schedule)
        self.tool_queue = asyncio.Queue()
        self._tool_calls = set()
        self._response_version = 0
        self._response_progress = None
        self._mic_bytes = 0
        self._gated_bytes = 0
        self._sent_bytes = 0
        self._mic_peak = 0
        self._reply_bytes = 0
        self._reply_peak = 0

    def response_progress(self):
        self._response_progress = asyncio.get_running_loop().time()

    async def health(self):
        """Log counters, never recordings/transcripts, and recover stuck turns."""
        last_report = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(1)
            now = asyncio.get_running_loop().time()
            if (self._response_progress is not None
                    and now - self._response_progress > RESPONSE_STALL_TIMEOUT
                    and not self.playback.speaking and not self.hearing):
                raise SessionError("Response stalled for 60 seconds")
            if now - last_report >= HEALTH_INTERVAL:
                log.info(
                    "Audio health: mic=%.1fs peak=%d gated=%.1fs sent=%.1fs "
                    "reply=%.1fs reply_peak=%d played=%.1fs queued=%.2fs state=%s",
                    self._mic_bytes / BYTES_PER_SECOND, self._mic_peak,
                    self._gated_bytes / BYTES_PER_SECOND, self._sent_bytes / 48000,
                    self._reply_bytes / 48000, self._reply_peak,
                    self.playback.played_bytes / BYTES_PER_SECOND,
                    self.playback.buffered / BYTES_PER_SECOND,
                    indicator_state(self.playback.speaking, self.responding, self.hearing),
                )
                self._mic_bytes = self._gated_bytes = self._sent_bytes = 0
                self._mic_peak = self._reply_bytes = self._reply_peak = 0
                self.playback.played_bytes = 0
                last_report = now

    async def tools(self):
        """Send outputs after a completed response, without pausing event reception."""
        while True:
            response_version, calls = await self.tool_queue.get()
            for call in calls:
                try:
                    arguments = json.loads(call.get("arguments", ""))
                except (ValueError, TypeError):
                    arguments = None
                result = await self.tool_runner.execute(call.get("name"), arguments)
                await self.client.send({
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": call["call_id"],
                             "output": json.dumps(result, allow_nan=False)},
                })
            # One continuation after all outputs, including multiple calls. If
            # the user started another response while sensors were reading,
            # leave the outputs in context without starting a competing reply.
            if response_version == self._response_version:
                self.response_progress()
                await self.client.send({"type": "response.create"})

    async def microphone(self):
        while True:
            audio = await self.audio.read()
            self._mic_bytes += len(audio)
            samples = np.frombuffer(audio, dtype="<i2").astype(np.int32)
            self._mic_peak = max(self._mic_peak, int(np.max(np.abs(samples), initial=0)))
            # No echo canceller on this HAT. Feed silence during playback and
            # its acoustic tail, continuing to drain capture so no stale audio returns.
            if self.playback.speaking:
                self._gated_bytes += len(audio)
                audio = bytes(len(audio))
            converted = self.input_adapter.convert(audio)
            if converted:
                await self.client.send_audio(converted)
                self._sent_bytes += len(converted)

    async def indicators(self):
        while True:
            self.ring.state = indicator_state(self.playback.speaking, self.responding, self.hearing)
            await asyncio.sleep(0.05)

    async def events(self):
        async for event in self.client.events():
            kind = event.get("type")
            if kind == "input_audio_buffer.speech_started":
                # Mark the new turn immediately; its response.created event
                # may arrive after an older sensor worker finishes.
                self._response_version += 1
                self.hearing = True
                log.info("Speech detected by Realtime")
            elif kind == "input_audio_buffer.speech_stopped":
                self.hearing = False
                self.responding = True
                self.response_progress()
                log.info("Speech ended; waiting for response")
            elif kind == "response.created":
                self._response_version += 1
                self.responding = True
                self.output_adapter = ModelToPiAudio()
                self.response_progress()
                log.info("Response started")
            elif kind == "response.output_audio.delta":
                pcm = base64.b64decode(event["delta"], validate=True)
                self.response_progress()
                self._reply_bytes += len(pcm)
                samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.int32)
                self._reply_peak = max(self._reply_peak, int(np.max(np.abs(samples), initial=0)))
                self.playback.push(self.output_adapter.convert(pcm))
            elif kind == "response.output_audio.done":
                self.playback.push(self.output_adapter.convert(b"", final=True))
            elif kind == "response.done":
                self.responding = False
                self._response_progress = None
                response = event.get("response", {})
                log.info("Response ended: %s", error_code({"code": response.get("status")}))
                if response.get("status") == "failed":
                    details = response.get("status_details") or {}
                    raise SessionError("Realtime response failed: " + error_code(details.get("error") or {}))
                if response.get("status") == "completed":
                    calls = []
                    for item in response.get("output", []):
                        call_id = item.get("call_id")
                        if item.get("type") == "function_call" and call_id and call_id not in self._tool_calls:
                            self._tool_calls.add(call_id)
                            calls.append(item)
                    if calls:
                        self.responding = True
                        self.response_progress()
                        self.tool_queue.put_nowait((self._response_version, calls))
            elif kind == "error":
                code = error_code(event.get("error") or {})
                # Log the error code only; server messages can contain user input.
                raise SessionError("Realtime error: " + code)
        raise ConnectionError("Realtime connection closed")

    async def run(self):
        tasks = []
        try:
            self.ring.state = "connecting"
            await self.client.open()
            await self.audio.start()
            log.info("Ready. Speak to Flora; mic pauses during speaker playback. Ctrl+C to stop.")
            tasks = [asyncio.create_task(fn()) for fn in (
                self.microphone, self.events, self.playback.run, self.indicators, self.tools, self.health
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
            if self._owns_sensors:
                await self.sensors.close()


async def run_hub(config):
    if not config.api_key:
        raise ValueError("Set OPENAI_API_KEY in this repo's .env before starting voice")
    ring = Ring(config)
    background = [asyncio.create_task(ring.run())]
    sensors = Sensors(config.soil_probe_in_soil)
    lights = schedule = None
    if config.lights_enabled:
        lights = Lights()
        schedule = LightSchedule(lights)
        # Timers keep running between conversations and across reconnects.
        background.append(asyncio.create_task(schedule.run()))
    try:
        retry_delay = 2
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await Session(config, ring, sensors, lights=lights, schedule=schedule).run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if asyncio.get_running_loop().time() - started >= 60:
                    retry_delay = 2
                # Never log connection URLs, headers, or configuration containing keys.
                log.warning("Session stopped (%s). Retrying in %ss.", failure_reason(exc), retry_delay)
                ring.state = "error"
                await asyncio.sleep(retry_delay)
                retry_delay = min(30, retry_delay * 2)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        ring.clear()
        await sensors.close()
