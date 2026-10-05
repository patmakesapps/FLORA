import asyncio
import logging
import math
import time

log = logging.getLogger(__name__)
COLORS = {
    "connecting": (1, 0.35, 0),
    "idle": (1, 0, 1),
    "listening": (1, 0, 1),
    "hearing": (0, 1, 0.6),
    "thinking": (0.3, 0.3, 1),
    "speaking": (0, 1, 0),
    "error": (1, 0, 0),
    "off": (0, 0, 0),
}


def encode_frame(rgb, count, maximum, intensity=1.0):
    maximum = min(63, max(0, maximum))
    intensity = min(1.0, max(0.0, intensity))
    red, green, blue = [round(min(1.0, max(0.0, c)) * maximum * intensity) for c in rgb]
    return bytes([green, red, blue]) * count


def animation_frame(state, count, maximum, elapsed, battery_percent=None):
    if state == "idle" and battery_percent is not None and battery_percent < 15:
        return encode_frame((1, 0, 0), count, maximum)
    if state == "listening":
        # Six-second breath; never brighter than the selected bench setting.
        intensity = 0.25 + 0.75 * (0.5 - 0.5 * math.cos(elapsed * math.tau / 6))
        return encode_frame(COLORS[state], count, maximum, intensity)
    if state in {"thinking", "connecting"}:
        position = int(elapsed * 5) % count
        rgb = COLORS["idle"] if state == "thinking" else COLORS[state]
        return b"".join(
            encode_frame(rgb, 1, maximum, {0: 1.0, 1: 0.5, 2: 0.25}.get((position - index) % count, 0))
            for index in range(count)
        )
    return encode_frame(COLORS[state], count, maximum)


class Ring:
    def __init__(self, config):
        self.count = config.led_count
        self.maximum = config.led_max_channel
        self.state = "connecting"
        self.battery_percent = None
        self._last = None
        self._write = None
        if config.leds_enabled:
            try:
                import board
                from adafruit_raspberry_pi5_neopixel_write import neopixel_write
                self._write = lambda data: neopixel_write(board.D12, data)
            except (ImportError, OSError, RuntimeError) as exc:
                log.warning("LEDs unavailable (%s); voice can still run", type(exc).__name__)

    def show(self, state, intensity=1.0):
        data = encode_frame(COLORS[state], self.count, self.maximum, intensity)
        self.write(data)

    def write(self, data):
        if self._write and data != self._last:
            try:
                self._write(data)
                self._last = data
            except (OSError, RuntimeError) as exc:
                log.warning("LED write failed (%s); disabling LEDs", type(exc).__name__)
                self._write = None

    def clear(self):
        self.show("off")

    async def run(self):
        try:
            while True:
                self.write(animation_frame(self.state, self.count, self.maximum, time.monotonic(), self.battery_percent))
                await asyncio.sleep(0.05)
        finally:
            self.clear()
