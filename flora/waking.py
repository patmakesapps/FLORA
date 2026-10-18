"""Offline "Hey Flora" detection with openWakeWord, running on the Pi."""
import collections
import logging
import wave
from datetime import datetime

import numpy as np

from .audio_adapter import PI_CHANNELS, PI_SAMPLE_RATE, WAKE_SAMPLE_RATE, PiToModelAudio
from .config import ROOT

log = logging.getLogger(__name__)
FRAME = 1280  # 80 ms at 16 kHz, openWakeWord's step size.
LOOKBACK_CHUNKS = 100  # 2 s of 20 ms mic chunks, kept to replay into the conversation.
NEAR_MISS_FLOOR = 0.05  # Standby sounds scoring at least this, but under the threshold, are logged.
NEAR_MISS_GAP = 12  # About 1 s of frames under the floor ends one sound.
CLIP_CHUNKS = 200  # 4 s of mic audio kept on the Pi for each wake and near miss.
CLIP_LIMIT = 40  # Oldest clips are removed beyond this many.
CLIP_DIR = ROOT / "data" / "wake_clips"


def wake_word_ready(config):
    """Why the wake word can't run, or None when its model is present."""
    if not config.wake_word_path.is_file():
        return f"wake word model {config.wake_word_path.name} not found"
    return None


def save_clip(chunks, label):
    """Keep the last few seconds of standby audio on the Pi, for tuning.

    Nothing leaves the Pi. tools/wakeword/score_clips.py scores these clips
    at several input gains so the threshold and mic level can be set from the
    user's own voice rather than synthetic test clips.
    """
    try:
        CLIP_DIR.mkdir(parents=True, exist_ok=True)
        path = CLIP_DIR / f"{label}-{datetime.now():%Y%m%d-%H%M%S}.wav"
        with wave.open(str(path), "wb") as clip:
            clip.setnchannels(PI_CHANNELS)
            clip.setsampwidth(2)
            clip.setframerate(PI_SAMPLE_RATE)
            clip.writeframes(b"".join(chunks))
        for old in sorted(CLIP_DIR.glob("*.wav"))[:-CLIP_LIMIT]:
            old.unlink()
    except OSError as exc:
        log.warning("Could not save a wake clip (%s)", type(exc).__name__)


class NearMisses:
    """Log the peak score of each standby sound that fell short of the threshold.

    Real wakes have scored anywhere from 0.20 to 0.49, so a missed "Hey Flora"
    usually lands just under the line. These lines show how far under, and how
    loud the mic was, so the threshold can be set from evidence.
    """
    def __init__(self, threshold):
        self.threshold = threshold
        self.best = 0.0
        self.peak = 0
        self.quiet = 0

    def frame(self, score, samples):
        """Returns the peak score of a sound that has just ended short, else None."""
        if score >= NEAR_MISS_FLOOR:
            self.best = max(self.best, score)
            self.peak = max(self.peak, int(np.max(np.abs(samples.astype(np.int32)), initial=0)))
            self.quiet = 0
        elif self.best:
            self.quiet += 1
            if self.quiet >= NEAR_MISS_GAP:
                log.info("Near miss: wake score peaked at %.2f, under %.2f (mic peak %d)",
                         self.best, self.threshold, self.peak)
                best, (self.best, self.peak, self.quiet) = self.best, (0.0, 0, 0)
                return best
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
        """Read the microphone until the wake word is heard; return the last 2 s.

        Nothing leaves the Pi while waiting. The returned audio holds the quiet
        before "Hey Flora" and the phrase itself, so Realtime's voice detection
        sees speech begin instead of a stream that starts mid-sentence.
        """
        adapter = PiToModelAudio(WAKE_SAMPLE_RATE)
        pending = np.zeros(0, dtype=np.int16)
        recent = collections.deque(maxlen=CLIP_CHUNKS)
        misses = NearMisses(self.config.wake_word_threshold)
        while True:
            chunk = await audio.read()
            recent.append(chunk)
            samples = np.frombuffer(adapter.convert(chunk), dtype="<i2")
            pending = np.concatenate((pending, samples))
            while len(pending) >= FRAME:
                frame, pending = pending[:FRAME], pending[FRAME:]
                score = self._model.predict(frame)[self._name]
                if score >= self.config.wake_word_threshold:
                    log.info("Wake word heard (score %.2f, mic peak %d)", score,
                             max(misses.peak, int(np.max(np.abs(frame.astype(np.int32)), initial=0))))
                    # Clear the model's audio history so it does not fire again at once.
                    self._model.reset()
                    if self.config.wake_clips:
                        save_clip(recent, f"wake-{score:.2f}")
                    return list(recent)[-LOOKBACK_CHUNKS:]
                missed = misses.frame(score, frame)
                if missed is not None and self.config.wake_clips:
                    save_clip(recent, f"miss-{missed:.2f}")

    def close(self):
        self._model = None
