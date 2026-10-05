"""Read-only X1200 fuel gauge: I2C bus 1, address 0x36."""
import asyncio
import logging
import time

log = logging.getLogger(__name__)


def decode_reading(voltage_bytes, percent_bytes):
    voltage = (((voltage_bytes[0] << 8) | voltage_bytes[1]) >> 4) * 0.00125
    percent = percent_bytes[0] + percent_bytes[1] / 256.0
    return {"percent": min(100.0, max(0.0, percent)), "voltage": voltage}


def read_battery():
    from smbus2 import SMBus
    with SMBus(1) as bus:
        voltage = bus.read_i2c_block_data(0x36, 0x02, 2)
        percent = bus.read_i2c_block_data(0x36, 0x04, 2)
    return decode_reading(voltage, percent)


class Battery:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.reading = None
        self.updated_at = 0.0

    @property
    def percent(self):
        if not self.reading or time.monotonic() - self.updated_at > 15:
            return None
        return self.reading["percent"]

    async def run(self, ring):
        warned = False
        while True:
            if self.enabled:
                try:
                    self.reading = await asyncio.to_thread(read_battery)
                    self.updated_at = time.monotonic()
                    warned = False
                except (OSError, ImportError):
                    self.reading = None
                    if not warned:
                        log.warning("X1200 gauge unavailable; battery level is unknown")
                        warned = True
            ring.battery_percent = self.percent
            await asyncio.sleep(5)
