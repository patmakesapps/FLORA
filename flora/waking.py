"""Offline "Hey Flora" detection with Picovoice Porcupine, running on the Pi."""
import asyncio
import logging

import numpy as np

from .audio_adapter import WAKE_SAMPLE_RATE, PiToModelAudio

log = logging.getLogger(__name__)


def wake_word_ready(config):
    """Why the wake word can't run, or None when its key and model are present."""
    if not config.picovoice_access_key:
        return "PICOVOICE_ACCESS_KEY is not set"
    if not config.wake_word_path.is_file():
        return f"wake word model {config.wake_word_path.name} not found"
    return None


class WakeWord:
    def __init__(self, config):
        self.config = config
        self._porcupine = None

    async def open(self):
        import pvporcupine
        # Creation checks the AccessKey online, so keep it off the event loop.
        self._porcupine = await asyncio.to_thread(
            pvporcupine.create, access_key=self.config.picovoice_access_key,
            keyword_paths=[str(self.config.wake_word_path)],
            sensitivities=[self.config.wake_word_sensitivity],
        )
        if self._porcupine.sample_rate != WAKE_SAMPLE_RATE:
            raise RuntimeError("Porcupine expects a different sample rate")

    async def wait(self, audio):
        """Read the microphone until the wake word is heard. Nothing leaves the Pi."""
        adapter = PiToModelAudio(WAKE_SAMPLE_RATE)
        frame = self._porcupine.frame_length
        pending = np.zeros(0, dtype=np.int16)
        while True:
            samples = np.frombuffer(adapter.convert(await audio.read()), dtype="<i2")
            pending = np.concatenate((pending, samples))
            while len(pending) >= frame:
                if self._porcupine.process(pending[:frame]) >= 0:
                    log.info("Wake word heard")
                    return
                pending = pending[frame:]

    def close(self):
        if self._porcupine is not None:
            self._porcupine.delete()
            self._porcupine = None
