"""A second opinion on Realtime's speech detection, run on the Pi.

Realtime's server VAD reacts to any loud sound, keyboard typing included.
Silero VAD (shipped with openWakeWord) scores each 30 ms of the audio sent to
Realtime for actual voice, so a turn can be checked before Flora answers it.
"""
import collections
import logging

import numpy as np
import soxr

from .audio_adapter import MODEL_SAMPLE_RATE, WAKE_SAMPLE_RATE

log = logging.getLogger(__name__)
FRAME = 480  # 30 ms at 16 kHz, Silero's recommended frame.
FRAME_MS = 30
VOICE_SCORE = 0.5
# The Pi mic is quiet (normal speech peaks near 2500) and Silero misses voices
# at a quarter of that, so it scores a boosted copy. Realtime gets the original.
GAIN = 4.0
HISTORY_FRAMES = 2000  # One minute of scores is plenty for one turn.


class VoiceCheck:
    def __init__(self):
        from openwakeword.vad import VAD
        self._vad = VAD()
        self._resampler = self._new_resampler()
        self._pending = np.zeros(0, dtype=np.int16)
        self._position_ms = 0
        self._scores = collections.deque(maxlen=HISTORY_FRAMES)  # (start_ms, score)

    @classmethod
    def create(cls):
        try:
            return cls()
        except Exception as exc:  # Missing model or onnxruntime: rely on Realtime alone.
            log.warning("Local voice check off (%s)", type(exc).__name__)
            return None

    @staticmethod
    def _new_resampler():
        return soxr.ResampleStream(MODEL_SAMPLE_RATE, WAKE_SAMPLE_RATE, 1, dtype="int16", quality="HQ")

    def restart(self):
        """After a mute the audio jumps; don't join audio across it."""
        self._resampler = self._new_resampler()
        self._pending = np.zeros(0, dtype=np.int16)
        self._vad.reset_states()

    def feed(self, audio, stream_ms):
        """Score 24 kHz mono audio exactly as sent, starting at stream_ms."""
        if not len(self._pending):
            self._position_ms = stream_ms
        samples = self._resampler.resample_chunk(np.frombuffer(audio, dtype="<i2"))
        samples = (samples.astype(np.float32) * GAIN).clip(-32768, 32767).astype(np.int16)
        self._pending = np.concatenate((self._pending, samples))
        while len(self._pending) >= FRAME:
            score = float(self._vad.predict(self._pending[:FRAME], FRAME))
            self._scores.append((self._position_ms, score))
            self._pending = self._pending[FRAME:]
            self._position_ms += FRAME_MS

    def voiced_ms(self, start_ms, end_ms):
        return FRAME_MS * sum(1 for at, score in self._scores
                              if start_ms <= at < end_ms and score >= VOICE_SCORE)
