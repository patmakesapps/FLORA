"""Spoken volume control. The loudest level is fixed here, not by the model."""
import json
import logging

from .config import ROOT

log = logging.getLogger(__name__)
LEVELS = 10  # Level 10 plays at the configured SPEAKER_GAIN; nothing goes louder.
STEP_DB = 3.0
DEFAULT_LEVEL = 8  # 6 dB under the maximum.
PATH = ROOT / "data" / "volume.json"


def level_gain(level, max_gain):
    return max_gain * 10 ** (-STEP_DB * (LEVELS - level) / 20)


class Volume:
    def __init__(self, max_gain, path=PATH):
        self.max_gain = max_gain
        self.path = path
        self.audio = None  # The open sound card, whose software gain this sets.
        self.level = self._load()

    def _load(self):
        try:
            return min(LEVELS, max(1, int(json.loads(self.path.read_text())["level"])))
        except (OSError, ValueError, TypeError, KeyError):
            return DEFAULT_LEVEL

    @property
    def gain(self):
        return level_gain(self.level, self.max_gain)

    def attach(self, audio):
        self.audio = audio
        audio.gain = self.gain

    def set(self, level=None, change=None):
        """Set a level (1-10) or step up/down; neither reports the current level."""
        if level is not None and change is not None:
            raise ValueError("Give a level or a change, not both")
        if level is not None and not 1 <= level <= LEVELS:
            raise ValueError("Volume level out of range")
        if level is not None or change is not None:
            wanted = level if level is not None else self.level + (1 if change == "up" else -1)
            self.level = min(LEVELS, max(1, wanted))
            if self.audio is not None:
                self.audio.gain = self.gain
            self.path.parent.mkdir(exist_ok=True)
            self.path.write_text(json.dumps({"level": self.level}))
            log.info("Volume level %d", self.level)
        return {"level": self.level, "max_level": LEVELS,
                "at_maximum": self.level == LEVELS, "at_minimum": self.level == 1}
