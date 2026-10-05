"""Humalien's proven ALSA PCM format, without its hardware WebSocket."""
import asyncio
import logging
import re

import numpy as np

log = logging.getLogger(__name__)
RATE = 48_000
CHANNELS = 2
CHUNK_BYTES = 3840  # 20 ms of 48 kHz stereo PCM16
BYTES_PER_SECOND = RATE * CHANNELS * 2


def apply_gain(audio, gain):
    samples = np.frombuffer(audio, dtype="<i2")
    return (samples.astype(np.float32) * gain).clip(-32768, 32767).astype("<i2").tobytes()


class Audio:
    def __init__(self, device, gain, mic_capture_volume=None):
        self.device = device
        self.gain = gain
        self.mic_capture_volume = mic_capture_volume
        self._old_capture = None
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
            log.info("%s: %s", label, line.decode(errors="replace").strip())

    def _args(self):
        return ["-D", self.device, "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", str(CHANNELS)]

    async def start(self):
        try:
            if self.mic_capture_volume is not None:
                # Adjust only the live mixer; never persist ALSA state.
                await self._capture_gain()
            self.speaker = await self._start(
                ["aplay", *self._args(), "--buffer-time", "80000", "--period-time", "20000"],
                stdin=asyncio.subprocess.PIPE,
            )
            self.microphone = await self._start(
                ["arecord", *self._args()], stdout=asyncio.subprocess.PIPE
            )
            await asyncio.sleep(0.2)
            for process in (self.microphone, self.speaker):
                if process.returncode is not None:
                    raise RuntimeError("Audio device failed to open; see ALSA messages above")
        except BaseException:
            await self.close()
            raise

    async def _mixer(self, *args):
        process = await asyncio.create_subprocess_exec(
            "amixer", "-c", "wm8960soundcard", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await process.communicate()
        if process.returncode:
            raise RuntimeError("Could not configure WM8960 capture gain")
        return output.decode(errors="replace")

    async def _capture_gain(self):
        output = await self._mixer("sget", "Capture")
        volumes = re.findall(r"Front (?:Left|Right): Capture (\d+)", output)
        if len(volumes) != 2:
            raise RuntimeError("Could not read original WM8960 capture gain")
        self._old_capture = ",".join(volumes)
        await self._mixer("sset", "Capture", str(self.mic_capture_volume))

    async def read(self):
        try:
            return await self.microphone.stdout.readexactly(CHUNK_BYTES)
        except asyncio.IncompleteReadError as exc:
            raise RuntimeError("Microphone stopped") from exc

    async def write(self, audio):
        if self.speaker.returncode is not None:
            raise RuntimeError("Speaker stopped")
        self.speaker.stdin.write(apply_gain(audio, self.gain))
        await self.speaker.stdin.drain()

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


class Playback:
    """Pace generated audio so ALSA never accumulates seconds of stale speech."""
    def __init__(self, audio):
        self.audio = audio
        self.queue = asyncio.Queue()
        self.buffered = 0
        self.until = 0.0

    @property
    def speaking(self):
        return self.buffered > 0 or asyncio.get_running_loop().time() < self.until

    def push(self, audio):
        if self.buffered + len(audio) > BYTES_PER_SECOND * 120:
            raise RuntimeError("Playback backlog exceeded 120 seconds")
        for offset in range(0, len(audio), CHUNK_BYTES):
            chunk = audio[offset:offset + CHUNK_BYTES]
            self.buffered += len(chunk)
            self.queue.put_nowait(chunk)

    async def run(self):
        loop = asyncio.get_running_loop()
        while True:
            chunk = await self.queue.get()
            # Keep the mic gated through the hardware buffer and acoustic tail.
            self.until = loop.time() + len(chunk) / BYTES_PER_SECOND + 0.3
            await self.audio.write(chunk)
            self.buffered -= len(chunk)
            await asyncio.sleep(len(chunk) / BYTES_PER_SECOND)
