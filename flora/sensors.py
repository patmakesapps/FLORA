"""Live I2C measurements, isolated from the Realtime/audio event loop."""
import asyncio
import logging
import math
import statistics
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)
READ_TIMEOUT = 5.0
SENSOR_NAMES = ("light", "soil", "environment")
READ_ERRORS = (ImportError, OSError, RuntimeError, ValueError)
SOIL_WINDOW_SIZE = 5
SOIL_SAMPLE_INTERVAL = 0.1


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def finite_value(value, minimum, maximum=None):
    value = float(value)
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError("Invalid sensor measurement")
    return value


class Sensors:
    def __init__(self, soil_probe_in_soil=False, bus=1):
        # Hardware imports and initialization happen only in the worker thread.
        self.soil_probe_in_soil = soil_probe_in_soil
        self.bus = bus
        self._i2c = None
        self._devices = {}
        self._pending = None
        self._closed = False

    def _empty_status(self, error="unavailable"):
        return {
            "requested_at": timestamp(),
            "light": {
                "sensor": "VEML7700", "address": "0x10", "status": "unavailable",
                "lux": None, "raw_als": None, "measured_at": None, "error": error,
                "note": "Measured behind a thin PETG window; transmission is uncalibrated. "
                        "This is light at the sensor, not necessarily at the leaves.",
            },
            "soil": {
                "sensor": "STEMMA #4026", "address": "0x36", "status": "unavailable",
                "raw_capacitance": None, "measured_at": None, "error": error,
                "probe_in_soil": self.soil_probe_in_soil,
                "note": "Raw capacitive counts, not a moisture percentage. "
                        + ("Interpret with observed soil conditions and saved references." if self.soil_probe_in_soil else
                           "Probe is outside soil; this reading cannot describe a plant's moisture."),
            },
            "environment": {
                "sensor": "SHT41", "address": "0x44", "status": "unavailable",
                "temperature_c": None, "temperature_f": None, "relative_humidity_percent": None,
                "measured_at": None, "error": error,
                "note": "Air conditions at the sensor; enclosure/Pi heat may affect readings.",
            },
        }

    def _device(self, name):
        if name not in self._devices:
            if name == "light":
                import adafruit_veml7700
                device = adafruit_veml7700.VEML7700(self._i2c, address=0x10)
                device.light_psm_en = False
            elif name == "soil":
                from adafruit_seesaw.seesaw import Seesaw
                device = Seesaw(self._i2c, addr=0x36)
            else:
                import adafruit_sht4x
                device = adafruit_sht4x.SHT4x(self._i2c, address=0x44)
                device.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION
            self._devices[name] = device
        return self._devices[name]

    def _ensure_bus(self):
        if self._i2c is None:
            if self.bus == 1:
                import board
                self._i2c = board.I2C()  # Pi SDA/SCL: I2C bus 1.
            else:
                # Its own bus keeps the sensor wiring off the sound chip's bus 1.
                from adafruit_extended_bus import ExtendedI2C
                self._i2c = ExtendedI2C(self.bus)

    def _measure(self, name):
        device = self._device(name)
        if name == "light":
            # autolux starts its search at these settings. Reset each request so
            # changing from darkness to bright light cannot leave it at high gain.
            device.light_gain = device.ALS_GAIN_1_8
            device.light_integration_time = device.ALS_100MS
            time.sleep(0.2)  # Allow a fresh conversion after changing settings.
            lux = finite_value(device.autolux, 0)
            raw = int(device.light)
            finite_value(raw, 0, 65534)  # 65535 means the light ADC saturated.
            return {"lux": round(lux, 2), "raw_als": raw}
        if name == "soil":
            sample = self._soil_samples(SOIL_WINDOW_SIZE)
            return {"raw_capacitance": sample["raw"], "raw_samples": sample["samples"],
                    "sample_spread": sample["spread"], "sample_count": len(sample["samples"]),
                    "sampling": "median_of_live_counts"}
        temperature, humidity = device.measurements  # One CRC-checked conversion.
        temperature = finite_value(temperature, -40, 125)
        return {
            "temperature_c": round(temperature, 2),
            "temperature_f": round(temperature * 9 / 5 + 32, 2),
            "relative_humidity_percent": round(finite_value(humidity, 0, 100), 2),
        }

    @staticmethod
    def _error_code(exc):
        if isinstance(exc, ImportError):
            return "missing_dependency"
        if isinstance(exc, OSError):
            return "i2c_error"
        if isinstance(exc, ValueError):
            return "invalid_reading"
        return "device_error"

    def _read(self):
        result = self._empty_status()
        try:
            self._ensure_bus()
            for name in SENSOR_NAMES:
                # Retry once, rebuilding only the affected driver. A missing
                # sensor must not suppress the other sensors or break voice.
                for attempt in range(2):
                    try:
                        values = self._measure(name)
                        result[name].update(values, status="ok", error=None, measured_at=timestamp())
                        break
                    except READ_ERRORS as exc:
                        self._devices.pop(name, None)
                        result[name]["error"] = self._error_code(exc)
                        if isinstance(exc, ImportError) or attempt == 1:
                            log.warning("%s sensor unavailable (%s)", name, type(exc).__name__)
                            break
                        time.sleep(0.05)
        except READ_ERRORS as exc:
            result = self._empty_status(self._error_code(exc))
            log.warning("Sensor bus unavailable (%s)", type(exc).__name__)
        finally:
            if self._closed:
                self._release_bus()
        return result

    def _soil_samples(self, count):
        device = self._device("soil")
        samples = []
        for index in range(count):
            raw = device.moisture_read()
            finite_value(raw, 0, 4095)
            samples.append(int(raw))
            if index < count - 1:
                time.sleep(SOIL_SAMPLE_INTERVAL)
        return {"samples": samples, "raw": int(statistics.median(samples)),
                "spread": max(samples) - min(samples), "measured_at": timestamp()}

    def _sample_soil(self):
        try:
            self._ensure_bus()
            sample = self._soil_samples(3 * SOIL_WINDOW_SIZE)
            samples = sample["samples"]
            medians = [int(statistics.median(samples[i:i + SOIL_WINDOW_SIZE]))
                       for i in range(0, len(samples), SOIL_WINDOW_SIZE)]
            return {**sample, "window_medians": medians,
                    "median_spread": max(medians) - min(medians)}
        except READ_ERRORS as exc:
            self._devices.pop("soil", None)
            return {"error": self._error_code(exc)}
        finally:
            if self._closed:
                self._release_bus()

    async def _run_read(self, reader):
        if self._closed:
            raise RuntimeError("Sensors are closed")
        if self._pending is not None and not self._pending.done():
            return {"error": "read_in_progress"}
        self._pending = asyncio.create_task(asyncio.to_thread(reader))
        # Retrieve exceptions even if the caller times out or is cancelled.
        self._pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(self._pending), READ_TIMEOUT)
        except asyncio.TimeoutError:
            # A timed-out thread cannot be cancelled safely. Keep it tracked so
            # repeated calls never pile up workers or access the bus concurrently.
            return {"error": "read_timeout"}
        except Exception as exc:
            log.warning("Sensor read failed (%s)", type(exc).__name__)
            return {"error": "read_failed"}

    async def get_plant_status(self):
        result = await self._run_read(self._read)
        return self._empty_status(result["error"]) if "error" in result else result

    async def sample_soil(self):
        return await self._run_read(self._sample_soil)

    def _release_bus(self):
        if self._i2c is not None:
            try:
                self._i2c.deinit()
            except READ_ERRORS:
                pass
            self._i2c = None
            self._devices.clear()

    async def close(self):
        self._closed = True
        if self._pending is not None and not self._pending.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._pending), READ_TIMEOUT)
            except Exception:
                # A still-running reader releases the bus in its own finally.
                return
        await asyncio.to_thread(self._release_bus)
