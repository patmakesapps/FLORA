"""Score saved wake clips at several input gains, on the Pi.

    .venv/bin/python tools/wakeword/score_clips.py [data/wake_clips/*.wav]

Each clip is the 4 s of 48 kHz stereo mic audio FLORA kept for a wake or a
near miss. The table shows the peak "Hey Flora" score per clip when the audio
is fed to the model as recorded and boosted 2x, 4x and 8x, so you can see
whether a quieter mic is what keeps scores low.
"""
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from flora.audio_adapter import WAKE_SAMPLE_RATE, PiToModelAudio  # noqa: E402
from flora.config import Config  # noqa: E402
from flora.waking import CLIP_DIR, FRAME  # noqa: E402

GAINS = (1, 2, 4, 8)


def peak_score(model, name, samples):
    model.reset()
    best = 0.0
    for start in range(0, len(samples) - FRAME + 1, FRAME):
        best = max(best, float(model.predict(samples[start:start + FRAME])[name]))
    return best


def main(paths):
    from openwakeword.model import Model
    config = Config.load()
    model = Model(wakeword_models=[str(config.wake_word_path)], inference_framework="onnx")
    name = next(iter(model.models))
    print(f"{'clip':34} {'peak':>5} " + " ".join(f"x{gain:<4}" for gain in GAINS))
    for path in paths:
        with wave.open(str(path), "rb") as clip:
            raw = clip.readframes(clip.getnframes())
        mono = np.frombuffer(PiToModelAudio(WAKE_SAMPLE_RATE).convert(raw), dtype="<i2")
        peak = int(np.max(np.abs(mono.astype(np.int32)), initial=0))
        scores = []
        for gain in GAINS:
            boosted = (mono.astype(np.float32) * gain).clip(-32768, 32767).astype(np.int16)
            scores.append(peak_score(model, name, boosted))
        print(f"{path.name:34} {peak:5d} " + " ".join(f"{score:5.2f}" for score in scores))


if __name__ == "__main__":
    clips = [Path(arg) for arg in sys.argv[1:]] or sorted(CLIP_DIR.glob("*.wav"))
    if not clips:
        sys.exit(f"No clips in {CLIP_DIR}")
    main(clips)
