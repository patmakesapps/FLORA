import numpy as np
import soxr


PI_SAMPLE_RATE = 48_000
PI_CHANNELS = 2

MODEL_SAMPLE_RATE = 24_000
MODEL_CHANNELS = 1

# What the offline wake word listener wants. See waking.py.
WAKE_SAMPLE_RATE = 16_000

SAMPLE_WIDTH = 2


def _split_whole_frames(
    buffered: bytes,
    channels: int,
) -> tuple[bytes, bytes]:
    """Split a byte string on the last complete frame boundary.

    A pipe read can end part way through a sample, so the leftover bytes
    have to be carried into the next chunk. Dropping them instead would
    swap the left and right channels for the rest of the session.
    """

    frame_size = channels * SAMPLE_WIDTH
    usable = len(buffered) - (len(buffered) % frame_size)

    return buffered[:usable], buffered[usable:]


class PiToModelAudio:
    """Convert Pi PCM16 audio from 48 kHz stereo to 24 kHz mono.

    The wake word listener needs the same stereo-to-mono downsample at its
    own rate, so the target is a parameter. Each instance carries its own
    resampler state and they must not be shared: two consumers stepping the
    same stream would each get half the samples.
    """

    def __init__(self, rate: int = MODEL_SAMPLE_RATE):
        self.resampler = soxr.ResampleStream(
            PI_SAMPLE_RATE,
            rate,
            MODEL_CHANNELS,
            dtype="int16",
            quality="HQ",
        )
        self.residual = b""

    def convert(self, audio: bytes, *, final: bool = False) -> bytes:
        whole, self.residual = _split_whole_frames(
            self.residual + audio,
            PI_CHANNELS,
        )

        samples = np.frombuffer(whole, dtype="<i2")
        stereo = samples.reshape(-1, PI_CHANNELS)

        # Average left and right without overflowing a 16-bit integer.
        mono = (
            stereo.astype(np.int32)
            .mean(axis=1)
            .clip(-32768, 32767)
            .astype(np.int16)
        )

        converted = self.resampler.resample_chunk(mono, last=final)
        return converted.astype("<i2", copy=False).tobytes()


class ModelToPiAudio:
    """Convert model PCM16 audio from 24 kHz mono to 48 kHz stereo."""

    def __init__(self):
        self.resampler = soxr.ResampleStream(
            MODEL_SAMPLE_RATE,
            PI_SAMPLE_RATE,
            MODEL_CHANNELS,
            dtype="int16",
            quality="HQ",
        )
        self.residual = b""

    def convert(self, audio: bytes, *, final: bool = False) -> bytes:
        whole, self.residual = _split_whole_frames(
            self.residual + audio,
            MODEL_CHANNELS,
        )

        mono = np.frombuffer(whole, dtype="<i2")

        converted = self.resampler.resample_chunk(mono, last=final)

        # Send the same signal through the Pi's left and right channels.
        stereo = np.repeat(converted[:, np.newaxis], PI_CHANNELS, axis=1)

        return stereo.astype("<i2", copy=False).tobytes()
