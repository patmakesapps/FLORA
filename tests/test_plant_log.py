import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from flora.memory import Memory
from flora.plant_log import KIND, PlantLog, seconds_to_next_hour, summarize
from flora.sensors import Sensors
from flora.tools import Tools


def status(lux=None, temperature=None, humidity=None, soil=None, error=None):
    result = Sensors(soil_probe_in_soil=True)._empty_status(error or "unavailable")
    if lux is not None:
        result["light"].update(status="ok", lux=lux, error=None)
    if temperature is not None:
        result["environment"].update(status="ok", temperature_c=temperature,
                                     relative_humidity_percent=humidity, error=None)
    if soil is not None:
        result["soil"].update(status="ok", raw_capacitance=soil, error=None)
    return result


class PlantLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.memory = Memory(folder.name + "/flora.sqlite3")

    async def test_hourly_reading_is_saved_compactly(self):
        sensors = AsyncMock()
        sensors.get_plant_status.return_value = status(1500.0, 22.5, 48.0, 610)
        self.assertTrue(await PlantLog(sensors, self.memory).record())
        rows = self.memory.readings_since(KIND, datetime(2000, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(rows[0]["data"], {"lux": 1500.0, "temperature_c": 22.5, "humidity_percent": 48.0,
                                           "soil_raw": 610, "probe_in_soil": True,
                                           "context_changed_during_capture": False})

    async def test_nothing_saved_without_sensors_and_busy_bus_retries(self):
        sensors = AsyncMock()
        sensors.get_plant_status.return_value = status()
        self.assertTrue(await PlantLog(sensors, self.memory).record())
        sensors.get_plant_status.return_value = status(error="read_in_progress")
        self.assertFalse(await PlantLog(sensors, self.memory).record())
        self.assertEqual(self.memory.readings_since(KIND, datetime(2000, 1, 1, tzinfo=timezone.utc)), [])

    def test_waits_until_the_top_of_the_hour(self):
        self.assertEqual(seconds_to_next_hour(datetime(2026, 10, 18, 12, 59, 30)), 30)

    def test_days_are_summarized(self):
        def row(hour, lux, temp, soil):
            return {"recorded_at": datetime(2026, 10, 18, hour).astimezone().isoformat(),
                    "data": {"lux": lux, "temperature_c": temp, "humidity_percent": 50.0,
                             "soil_raw": soil, "probe_in_soil": True}}
        day, = summarize([row(9, 50, 20.0, 700), row(12, 2000, 24.0, 680), row(15, 12000, 22.0, 650)])
        self.assertEqual(day["readings"], 3)
        self.assertEqual(day["light"], {"max_lux": 12000,
                                        "hourly_readings_at_or_above_lux": {"100": 2, "1000": 2, "10000": 1}})
        self.assertEqual(day["temperature_c"], {"min": 20.0, "avg": 22.0, "max": 24.0})
        self.assertEqual(day["soil_raw"], {"first": 700, "last": 650, "min": 650, "max": 700})

    async def test_trends_tool_returns_days_and_plant(self):
        sensors = AsyncMock()
        sensors.get_plant_status.return_value = status(300.0, 21.0, 40.0, 600)
        await PlantLog(sensors, self.memory).record()
        tools = Tools(sensors, self.memory)
        result = await tools.execute("get_plant_trends", {"days": 3})
        self.assertEqual(len(result["days"]), 1)
        self.assertIn("plant", result)
        self.assertEqual(await tools.execute("get_plant_trends", {"days": 0}), {"error": "invalid_arguments"})


if __name__ == "__main__":
    unittest.main()
