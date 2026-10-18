import asyncio
import base64
import json
import logging

import numpy as np

from .audio import Playback, BYTES_PER_SECOND, open_audio
from .errors import AudioError, SessionError, error_code, failure_reason
from .audio_adapter import ModelToPiAudio, PiToModelAudio
from .leds import Ring
from .light_schedule import LightSchedule
from .lights import Lights
from .power import Power
from .tools import Tools
from .volume import Volume
from .realtime import Realtime
from .sensors import Sensors
from .waking import WakeWord, wake_word_ready

log = logging.getLogger(__name__)
RESPONSE_STALL_TIMEOUT = 60.0
HEALTH_INTERVAL = 20.0
PREROLL_LIMIT = 24000 * 2 * 15  # Speech heard while connecting, up to 15 s.


def indicator_state(speaking, responding, hearing):
    if speaking:
        return "speaking"
    if responding:
        return "thinking"
    if hearing:
        return "listening"
    return "idle"



class Session:
    """One Realtime conversation on the hub's already-open sound card."""
    def __init__(self, config, ring, sensors=None, memory=None, lights=None, schedule=None, *,
                 audio=None, playback=None, power=None, volume=None, idle_timeout=None, lead_in=()):
        self.audio = audio if audio is not None else open_audio(config)
        self.client = Realtime(config)
        self.playback = playback if playback is not None else Playback(self.audio)
        self.power = power
        self.idle_timeout = idle_timeout
        self._connected = False
        self._preroll = []
        self._preroll_bytes = 0
        self.input_adapter = PiToModelAudio()
        self.output_adapter = ModelToPiAudio()
        # Mic audio from just before the wake fired, sent ahead of the rest.
        for chunk in lead_in:
            converted = self.input_adapter.convert(chunk)
            self._preroll.append(converted)
            self._preroll_bytes += len(converted)
        self.ring = ring
        self.hearing = False
        self.responding = False
        self.sensors = sensors if sensors is not None else Sensors(config.soil_probe_in_soil, config.sensor_i2c_bus)
        self._owns_sensors = sensors is None
        self.tool_runner = Tools(self.sensors, memory, lights, schedule, power, volume)
        self.tool_queue = asyncio.Queue()
        self._tool_calls = set()
        self._response_version = 0
        self._response_progress = None
        self._mic_bytes = 0
        self._gated_bytes = 0
        self._sent_bytes = 0
        # Echo diagnostics: where unmutes fall in the stream sent to Realtime,
        # and how loud the mic is just after each one (levels only, no audio).
        self._stream_bytes = 0
        self._was_gated = False
        self._unmute_levels = None
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
            # No echo canceller on this HAT, and replies can't be interrupted
            # anyway: the mic is muted from the end of the user's turn until
            # playback and its acoustic tail finish. Capture keeps draining so
            # no stale audio returns.
            gated = self.responding or self.playback.speaking
            if gated:
                self._gated_bytes += len(audio)
                audio = bytes(len(audio))
            self._track_unmute(gated, samples)
            converted = self.input_adapter.convert(audio)
            if not self._connected:
                # Keep what is said right after the wake word, while Realtime connects.
                if converted and self._preroll_bytes < PREROLL_LIMIT:
                    self._preroll.append(converted)
                    self._preroll_bytes += len(converted)
                continue
            if self._preroll:
                converted = b"".join(self._preroll) + converted
                self._preroll.clear()
            if converted:
                await self.client.send_audio(converted)
                self._sent_bytes += len(converted)
                self._stream_bytes += len(converted)

    def _track_unmute(self, gated, samples):
        """Log the mic level in the 0.4 s after the mic unmutes.

        An echo tail decays from the first 0.2 s to the second; a steady room
        noise floor doesn't. Compare the stream position with speech_started.
        """
        if self._was_gated and not gated:
            self._unmute_at = self._stream_bytes // 48  # 24 kHz mono PCM16 -> ms
            self._unmute_levels = []
        self._was_gated = gated
        if self._unmute_levels is None:
            return
        if gated:
            self._unmute_levels = None
            return
        self._unmute_levels.append(float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))))
        if len(self._unmute_levels) == 20:  # 0.4 s of 20 ms chunks
            first, second = self._unmute_levels[:10], self._unmute_levels[10:]
            log.info("Mic unmuted at stream %d ms: rms %.0f then %.0f",
                     self._unmute_at, np.mean(first), np.mean(second))
            self._unmute_levels = None

    async def watch(self):
        """End the conversation after silence, or power off after the goodbye."""
        loop = asyncio.get_running_loop()
        last_activity = loop.time()
        while True:
            await asyncio.sleep(0.5)
            now = loop.time()
            if self.hearing or self.responding or self.playback.speaking:
                last_activity = now
                continue
            if self.power is not None and self.power.requested:
                try:
                    await self.power.power_off()
                    return
                except OSError:
                    log.warning("Power off failed; staying on")
            if self.idle_timeout is not None and now - last_activity >= self.idle_timeout:
                log.info("No speech for %.0f seconds; returning to standby", self.idle_timeout)
                return

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
                log.info("Speech detected by Realtime at stream %s ms", event.get("audio_start_ms"))
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
        """Converse until the idle timeout or a power off; raise on failures."""
        tasks = []
        try:
            self.ring.state = "connecting"
            # Listen while connecting; buffered speech is sent once configured.
            tasks.append(asyncio.create_task(self.microphone()))
            await self.client.open()
            self._connected = True
            log.info("Ready. Speak to Flora; mic pauses while she answers. Ctrl+C to stop.")
            tasks += [asyncio.create_task(fn()) for fn in (
                self.events, self.indicators, self.tools, self.health, self.watch
            )]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # The sound card stays open for the next conversation.
            self.playback.clear()
            await asyncio.gather(self.client.close(), return_exceptions=True)
            if self._owns_sensors:
                await self.sensors.close()


async def until_done(speaker, coroutine):
    """Run coroutine, failing early if the always-running speaker task stops."""
    task = asyncio.create_task(coroutine)
    try:
        await asyncio.wait({speaker, task}, return_when=asyncio.FIRST_COMPLETED)
        if speaker.done():
            speaker.result()
            raise AudioError("Speaker stopped")
        return task.result()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def serve(config, ring, sensors, lights, schedule, power, volume):
    """Hold the sound card open: wait for the wake word, then converse, repeatedly.

    Returns only after a power off. Audio and wake word failures propagate so the
    caller reopens everything. Opening the card once rather than per conversation
    also avoids most WM8960 start-ups.
    """
    problem = wake_word_ready(config)
    wake = None
    if problem:
        log.warning("Wake word off (%s); staying connected to Realtime instead", problem)
    else:
        wake = WakeWord(config)
    audio = open_audio(config)
    volume.attach(audio)
    playback = Playback(audio)
    speaker = None
    try:
        if wake:
            await wake.open()
        await audio.start()
        # Keep ALSA clocked between conversations so the card never idles.
        speaker = asyncio.create_task(playback.run())
        retry_delay = 2
        while True:
            if wake:
                ring.state = "standby"
                log.info("Standby: listening locally for the wake word")
                lead_in = await until_done(speaker, wake.wait(audio))
            else:
                lead_in = ()
            started = asyncio.get_running_loop().time()
            session = Session(config, ring, sensors, lights=lights, schedule=schedule, audio=audio,
                              playback=playback, power=power, volume=volume,
                              idle_timeout=config.conversation_timeout if wake else None,
                              lead_in=lead_in)
            try:
                await until_done(speaker, session.run())
            except AudioError:
                raise
            except Exception as exc:
                # Never log connection URLs, headers, or configuration containing keys.
                if asyncio.get_running_loop().time() - started >= 60:
                    retry_delay = 2
                delay = 2 if wake else retry_delay
                log.warning("Session stopped (%s). %s in %ss.", failure_reason(exc),
                            "Standby" if wake else "Retrying", delay)
                ring.state = "error"
                await asyncio.sleep(delay)
                if not wake:
                    retry_delay = min(30, retry_delay * 2)
            if power is not None and power.requested:
                return
    finally:
        if speaker is not None:
            speaker.cancel()
            await asyncio.gather(speaker, return_exceptions=True)
        await asyncio.gather(audio.close(), return_exceptions=True)
        if wake:
            wake.close()


async def run_hub(config):
    if not config.api_key:
        raise ValueError("Set OPENAI_API_KEY in this repo's .env before starting voice")
    ring = Ring(config)
    background = [asyncio.create_task(ring.run())]
    sensors = Sensors(config.soil_probe_in_soil, config.sensor_i2c_bus)
    lights = schedule = None
    power = Power() if config.power_off_enabled else None
    volume = Volume(config.speaker_gain)
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
                await serve(config, ring, sensors, lights, schedule, power, volume)
                # Powering off: stay dark until systemd stops this process.
                ring.state = "off"
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if asyncio.get_running_loop().time() - started >= 60:
                    retry_delay = 2
                log.warning("Audio or wake word stopped (%s). Reopening in %ss.",
                            failure_reason(exc), retry_delay)
                ring.state = "error"
                await asyncio.sleep(retry_delay)
                retry_delay = min(30, retry_delay * 2)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        ring.clear()
        await sensors.close()
