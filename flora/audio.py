"""Humalien's proven ALSA PCM format, without its hardware WebSocket."""
import asyncio
import logging
import re

import numpy as np

from .errors import AudioError, SessionError

log = logging.getLogger(__name__)
RATE = 48_000
CHANNELS = 2
CHUNK_BYTES = 3840  # 20 ms of 48 kHz stereo PCM16
BYTES_PER_SECOND = RATE * CHANNELS * 2
PLAYBACK_PREROLL = 0.12  # Absorb uneven Realtime packet arrival before speaking.
AUDIO_IO_TIMEOUT = 5.0  # Recover if an ALSA pipe stops making progress.
PLAYBACK_LEAD = 0.06  # Feed ahead, staying within ALSA's 80 ms hardware buffer.
ECHO_TAIL = 0.5  # Keep the mic muted this long after the last speech is written.


def apply_gain(audio, gain):
    samples = np.frombuffer(audio, dtype="<i2")
    return (samples.astype(np.float32) * gain).clip(-32768, 32767).astype("<i2").tobytes()


def open_audio(config):
    return Audio(config.alsa_device, config.speaker_gain, config.mic_capture_volume, config.speaker_volume)


class Audio:
    def __init__(self, device, gain, mic_capture_volume=None, speaker_volume=None):
        self.device = device
        self.gain = gain
        self.mic_capture_volume = mic_capture_volume
        self.speaker_volume = speaker_volume
        self._old_capture = None
        self._old_speaker = None
        self.microphone = None
        self.speaker = None
        self._monitors = []

    async def _start(self, command, **kwargs):
        process = await asyncio.create_subprocess_exec(
            *command, stderr=asyncio.subprocess.PIPE, **kwargs
        )
        self._monitors.append(asyncio.create_task(self._monitor(process, command[0])))
        return process

    async def _monitor(self, process, label):
        while line := await process.stderr.readline():
            message = line.decode(errors="replace").strip()
            level = logging.WARNING if "underrun" in message.lower() else logging.INFO
            log.log(level, "%s: %s", label, message)

    def _args(self):
        return ["-D", self.device, "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", str(CHANNELS)]

    async def start(self):
        try:
            if self.mic_capture_volume is not None:
                # Adjust only the live mixer; never persist ALSA state.
                await self._capture_gain()
            if self.speaker_volume is not None:
                # Lower the amplifier before the card opens, so a bad start's
                # static and clicks are quieter; software gain makes up the level.
                self._old_speaker = await self._set_volume("Speaker", "Playback", self.speaker_volume)
            self.speaker = await self._start(
                ["aplay", *self._args(), "--buffer-time", "80000", "--period-time", "20000"],
                stdin=asyncio.subprocess.PIPE,
            )
            self._limit_speaker_backlog()
            self.microphone = await self._start(
                ["arecord", *self._args()], stdout=asyncio.subprocess.PIPE
            )
            await asyncio.sleep(0.2)
            for process in (self.microphone, self.speaker):
                if process.returncode is not None:
                    raise AudioError("Audio device failed to open; see ALSA messages above")
        except BaseException:
            await self.close()
            raise

    def _limit_speaker_backlog(self):
        """Make writes wait for the sound card instead of queueing ahead of it.

        The WM8960 clock runs slightly slow, so audio paced by the system clock
        piles up in aplay's 64 KiB stdin pipe over hours. The speaker then lags
        what we think was played and the mic unmutes before she has finished.
        """
        import fcntl  # Linux only; keeps the module importable for tests elsewhere.
        self.speaker.stdin.transport.set_write_buffer_limits(high=0)
        pipe = self.speaker.stdin.transport.get_extra_info("pipe")
        try:
            size = fcntl.fcntl(pipe.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            log.info("Speaker pipe limited to %d bytes (%.0f ms)", size, size / BYTES_PER_SECOND * 1000)
        except (AttributeError, OSError) as exc:
            log.warning("Could not shrink the speaker pipe (%s); echo gate may drift", exc)

    async def _mixer(self, *args):
        process = await asyncio.create_subprocess_exec(
            "amixer", "-c", "wm8960soundcard", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await process.communicate()
        if process.returncode:
            raise AudioError("Could not configure WM8960 mixer")
        return output.decode(errors="replace")

    async def _set_volume(self, control, kind, value):
        """Set a stereo mixer control and return its previous value for restoring."""
        output = await self._mixer("sget", control)
        volumes = re.findall(rf"Front (?:Left|Right): {kind} (\d+)", output)
        if len(volumes) != 2:
            raise AudioError(f"Could not read original WM8960 {control} volume")
        await self._mixer("sset", control, str(value))
        return ",".join(volumes)

    async def _capture_gain(self):
        self._old_capture = await self._set_volume("Capture", "Capture", self.mic_capture_volume)

    async def read(self):
        try:
            async with asyncio.timeout(AUDIO_IO_TIMEOUT):
                return await self.microphone.stdout.readexactly(CHUNK_BYTES)
        except TimeoutError as exc:
            raise AudioError("Microphone stalled for 5 seconds") from exc
        except asyncio.IncompleteReadError as exc:
            raise AudioError("Microphone stopped") from exc

    async def write(self, audio):
        if self.speaker.returncode is not None:
            raise AudioError("Speaker stopped")
        try:
            async with asyncio.timeout(AUDIO_IO_TIMEOUT):
                self.speaker.stdin.write(apply_gain(audio, self.gain))
                await self.speaker.stdin.drain()
        except TimeoutError as exc:
            raise AudioError("Speaker stalled for 5 seconds") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AudioError("Speaker pipe closed") from exc

    async def close(self):
        # Stop stderr monitors before communicate() takes ownership of the
        # pipes. Drain capture too: Process.wait() can hang when an unread
        # microphone stdout pipe has paused its asyncio transport.
        for task in self._monitors:
            task.cancel()
        await asyncio.gather(*self._monitors, return_exceptions=True)
        self._monitors.clear()
        for process in (self.microphone, self.speaker):
            if process and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.communicate(), 2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
        if self._old_capture is not None:
            await self._mixer("sset", "Capture", self._old_capture)
            self._old_capture = None
        if self._old_speaker is not None:
            await self._mixer("sset", "Speaker", self._old_speaker)
            self._old_speaker = None


class Playback:
    """Buffer packet jitter and feed ALSA against an absolute PCM timeline."""
    def __init__(self, audio):
        self.audio = audio
        self.queue = asyncio.Queue()
        self.buffered = 0
        self.until = 0.0
        self.played_bytes = 0

    @property
    def speaking(self):
        return self.buffered > 0 or asyncio.get_running_loop().time() < self.until

    def clear(self):
        """Drop queued speech, e.g. when a conversation ends abruptly."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.buffered = 0

    def push(self, audio):
        if self.buffered + len(audio) > BYTES_PER_SECOND * 120:
            raise SessionError("Playback backlog exceeded 120 seconds")
        for offset in range(0, len(audio), CHUNK_BYTES):
            chunk = audio[offset:offset + CHUNK_BYTES]
            self.buffered += len(chunk)
            self.queue.put_nowait(chunk)

    async def run(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        pending = bytearray()
        ready_at = None
        silence = bytes(CHUNK_BYTES)
        while True:
            # Keep ALSA clocking even between replies. Idle silence must not
            # extend `until`, otherwise it would permanently gate the mic.
            if self.buffered and ready_at is None:
                ready_at = loop.time() + PLAYBACK_PREROLL
            speech_bytes = 0
            chunk = silence
            if ready_at is not None and loop.time() >= ready_at:
                while len(pending) < CHUNK_BYTES and not self.queue.empty():
                    pending.extend(self.queue.get_nowait())
                speech_bytes = min(len(pending), CHUNK_BYTES)
                if speech_bytes:
                    chunk = bytes(pending[:speech_bytes]) + silence[speech_bytes:]
                    del pending[:speech_bytes]

            deadline = max(deadline, loop.time()) + CHUNK_BYTES / BYTES_PER_SECOND
            if speech_bytes:
                self.until = deadline + ECHO_TAIL
            await self.audio.write(chunk)
            self.buffered -= speech_bytes
            self.played_bytes += speech_bytes
            if not self.buffered:
                ready_at = None
            # Absolute pacing avoids accumulating write/scheduling overhead.
            await asyncio.sleep(max(0, deadline - loop.time() - PLAYBACK_LEAD))
