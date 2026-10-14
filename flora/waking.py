"""Offline "Hey Flora" detection with openWakeWord, running on the Pi."""
import logging

import numpy as np

from .audio_adapter import WAKE_SAMPLE_RATE, PiToModelAudio

log = logging.getLogger(__name__)
FRAME = 1280  # 80 ms at 16 kHz, openWakeWord's step size.


def wake_word_ready(config):
    """Why the wake word can't run, or None when its model is present."""
    if not config.wake_word_path.is_file():
        return f"wake word model {config.wake_word_path.name} not found"
    return None


class WakeWord:
    def __init__(self, config):
        self.config = config
        self._model = None
        self._name = None

    async def open(self):
        from openwakeword.model import Model
        self._model = Model(wakeword_models=[str(self.config.wake_word_path)], inference_framework="onnx")
        self._name = next(iter(self._model.models))

    async def wait(self, audio):
        """Read the microphone until the wake word is heard. Nothing leaves the Pi."""
        adapter = PiToModelAudio(WAKE_SAMPLE_RATE)
        pending = np.zeros(0, dtype=np.int16)
        while True:
            samples = np.frombuffer(adapter.convert(await audio.read()), dtype="<i2")
            pending = np.concatenate((pending, samples))
            while len(pending) >= FRAME:
                score = self._model.predict(pending[:FRAME])[self._name]
                pending = pending[FRAME:]
                if score >= self.config.wake_word_threshold:
                    log.info("Wake word heard (score %.2f)", score)
                    # Clear the model's audio history so it does not fire again at once.
                    self._model.reset()
                    return

    def close(self):
        self._model = None
