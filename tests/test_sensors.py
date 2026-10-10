import asyncio
import io
import json
import threading
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flora.__main__ import main, sensor_test
from flora.config import Config
from flora.memory import Memory
from flora.sensors import SENSOR_NAMES, Sensors


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.bus = Mock()
        self.light = SimpleNamespace(
            autolux=123.456, light=1000, ALS_GAIN_1_8=2, ALS_100MS=0,
        )
        self.soil = Mock()
        self.soil.moisture_read.return_value = 456
        self.environment = SimpleNamespace(measurements=(23.456, 54.321))
        self.modules = {
            "board": SimpleNamespace(I2C=Mock(return_value=self.bus)),
            "adafruit_veml7700": SimpleNamespace(VEML7700=Mock(return_value=self.light)),
            "adafruit_seesaw.seesaw": SimpleNamespace(Seesaw=Mock(return_value=self.soil)),
            "adafruit_sht4x": SimpleNamespace(
                SHT4x=Mock(return_value=self.environment),
                Mode=SimpleNamespace(NOHEAT_HIGHPRECISION=0xFD),
            ),
        }
        self.patches = [patch.dict("sys.modules", self.modules), patch("flora.sensors.time.sleep")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.sensors = Sensors()

    def test_reads_all_three_real_driver_interfaces_at_expected_addresses(self):
        result = self.sensors._read()
        for name in SENSOR_NAMES:
            self.assertEqual(result[name]["status"], "ok")
            self.assertIsNotNone(result[name]["measured_at"])
            self.assertIsNone(result[name]["error"])
        self.assertEqual(result["light"]["lux"], 123.46)
        self.assertEqual(result["light"]["raw_als"], 1000)
        self.assertIn("PETG", result["light"]["note"])
        self.assertEqual(result["soil"]["raw_capacitance"], 456)
        self.assertFalse(result["soil"]["probe_in_soil"])
        self.assertFalse(result["soil"]["calibrated"])
        self.assertNotIn("moisture_percent", result["soil"])
        self.assertEqual(result["environment"]["temperature_c"], 23.46)
        self.assertEqual(result["environment"]["relative_humidity_percent"], 54.32)
        self.modules["adafruit_veml7700"].VEML7700.assert_called_once_with(self.bus, address=0x10)
        self.modules["adafruit_seesaw.seesaw"].Seesaw.assert_called_once_with(self.bus, addr=0x36)
        self.modules["adafruit_sht4x"].SHT4x.assert_called_once_with(self.bus, address=0x44)
        self.assertEqual(self.light.light_gain, self.light.ALS_GAIN_1_8)
        self.assertEqual(self.light.light_integration_time, self.light.ALS_100MS)
        self.assertFalse(self.light.light_psm_en)
        self.assertEqual(self.environment.mode, 0xFD)
        json.dumps(result, allow_nan=False)

    def test_one_sensor_failure_does_not_hide_other_readings_or_return_stale_values(self):
        self.sensors._read()
        self.soil.moisture_read.side_effect = OSError("disconnected")
        result = self.sensors._read()
        self.assertEqual(result["soil"]["status"], "unavailable")
        self.assertEqual(result["soil"]["error"], "i2c_error")
        self.assertIsNone(result["soil"]["raw_capacitance"])
        self.assertIsNone(result["soil"]["measured_at"])
        self.assertEqual(result["light"]["status"], "ok")
        self.assertEqual(result["environment"]["status"], "ok")
        self.soil.moisture_read.side_effect = None
        self.assertEqual(self.sensors._read()["soil"]["status"], "ok")

    def test_transient_failure_reinitializes_and_retries_only_failed_sensor(self):
        self.soil.moisture_read.side_effect = [RuntimeError("bad read"), *([789] * 5)]
        result = self.sensors._read()
        self.assertEqual(result["soil"]["raw_capacitance"], 789)
        self.assertEqual(self.modules["adafruit_seesaw.seesaw"].Seesaw.call_count, 2)
        self.assertEqual(self.modules["adafruit_veml7700"].VEML7700.call_count, 1)

    def test_bus_and_missing_driver_failures_are_explicit(self):
        self.modules["board"].I2C.side_effect = PermissionError("no bus access")
        result = self.sensors._read()
        for name in SENSOR_NAMES:
            self.assertEqual(result[name]["error"], "i2c_error")
            self.assertIsNone(result[name]["measured_at"])
        self.modules["board"].I2C.side_effect = None
        self.modules["adafruit_seesaw.seesaw"].Seesaw.side_effect = ImportError("missing")
        result = self.sensors._read()
        self.assertEqual(result["soil"]["error"], "missing_dependency")
        self.assertEqual(result["environment"]["status"], "ok")

    def test_invalid_measurements_and_saturated_light_are_not_reported_as_valid(self):
        for field, value in (("autolux", float("nan")), ("autolux", -1), ("light", 65535)):
            with self.subTest(field=field, value=value):
                setattr(self.light, field, value)
                result = self.sensors._read()
                self.assertEqual(result["light"]["error"], "invalid_reading")
                self.assertIsNone(result["light"]["lux"])
                self.light.autolux, self.light.light = 123.456, 1000
        for measurements in ((float("inf"), 50), (20, 101), (-41, 50)):
            self.environment.measurements = measurements
            result = self.sensors._read()
            self.assertIsNone(result["environment"]["temperature_c"])
            self.assertIsNone(result["environment"]["relative_humidity_percent"])
        self.soil.moisture_read.return_value = 65535
        self.assertIsNone(self.sensors._read()["soil"]["raw_capacitance"])

    def test_probe_placement_is_metadata_and_does_not_calibrate_counts(self):
        result = Sensors(soil_probe_in_soil=True)._read()
        self.assertTrue(result["soil"]["probe_in_soil"])
        self.assertFalse(result["soil"]["calibrated"])
        self.assertIn("No calibrated", result["soil"]["note"])

    def test_soil_calibration_samples_only_soil_and_preserves_raw_units(self):
        samples = [450, 452, 449, 451, 450] * 3
        self.soil.moisture_read.side_effect = samples
        result = self.sensors._sample_soil()
        self.assertEqual(result["raw"], 450)
        self.assertEqual(result["spread"], 3)
        self.assertEqual(result["samples"], samples)
        self.assertEqual(result["window_medians"], [450, 450, 450])
        self.assertEqual(result["median_spread"], 0)
        self.modules["adafruit_veml7700"].VEML7700.assert_not_called()
        self.modules["adafruit_sht4x"].SHT4x.assert_not_called()

    def test_live_soil_status_uses_median_and_keeps_individual_counts(self):
        samples = [780, 810, 790, 770, 1000]
        self.soil.moisture_read.side_effect = samples
        result = self.sensors._measure("soil")
        self.assertEqual(result["raw_capacitance"], 790)
        self.assertEqual(result["raw_samples"], samples)
        self.assertEqual(result["sample_spread"], 230)
        self.assertEqual(result["sample_count"], 5)

    def test_calibration_detects_drift_between_sample_windows(self):
        self.soil.moisture_read.side_effect = [700] * 5 + [800] * 5 + [900] * 5
        result = self.sensors._sample_soil()
        self.assertEqual(result["raw"], 800)
        self.assertEqual(result["window_medians"], [700, 800, 900])
        self.assertEqual(result["median_spread"], 200)
        self.assertEqual(result["spread"], 200)


class AsyncSensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        memory = Memory(directory.name + "/flora.sqlite3")
        memory_patch = patch("flora.tools.Memory", return_value=memory)
        memory_patch.start()
        self.addCleanup(memory_patch.stop)

    async def test_timeout_keeps_one_worker_and_event_loop_responsive(self):
        sensors = Sensors()
        started = threading.Event()
        release = threading.Event()
        worker_ids = []
        def stalled_read():
            worker_ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return sensors._empty_status()
        sensors._read = stalled_read
        try:
            with patch("flora.sensors.READ_TIMEOUT", 0.03):
                call = asyncio.create_task(sensors.get_plant_status())
                await asyncio.sleep(0.01)
                self.assertTrue(started.is_set())
                self.assertFalse(call.done())
                result = await call
                self.assertEqual(result["soil"]["error"], "read_timeout")
                self.assertIsNone(result["soil"]["raw_capacitance"])
                self.assertEqual((await sensors.get_plant_status())["soil"]["error"], "read_in_progress")
                self.assertEqual(len(worker_ids), 1)
                self.assertNotEqual(worker_ids[0], threading.get_ident())
        finally:
            release.set()
            await sensors.close()

    async def test_cancellation_keeps_inflight_worker_tracked_until_shutdown(self):
        sensors = Sensors()
        release = threading.Event()
        sensors._read = lambda: release.wait(2)
        call = asyncio.create_task(sensors.get_plant_status())
        try:
            await asyncio.sleep(0.01)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
            self.assertFalse(sensors._pending.done())
            self.assertEqual((await sensors.get_plant_status())["light"]["error"], "read_in_progress")
        finally:
            release.set()
            await sensors.close()

    async def test_close_deinitializes_bus_and_rejects_later_reads(self):
        sensors = Sensors()
        bus = sensors._i2c = Mock()
        await sensors.close()
        bus.deinit.assert_called_once()
        with self.assertRaises(RuntimeError):
            await sensors.get_plant_status()

    async def test_unexpected_driver_failure_returns_error_instead_of_breaking_voice(self):
        sensors = Sensors()
        with patch.object(sensors, "_read", side_effect=AttributeError("driver failed")):
            result = await sensors.get_plant_status()
        self.assertEqual(result["light"]["error"], "read_failed")
        await sensors.close()

    async def test_cli_displays_partial_readings_without_opening_realtime(self):
        result = Sensors()._empty_status("i2c_error")
        result["light"].update(status="ok", lux=12.34)
        with patch.object(Sensors, "_read", return_value=result), redirect_stdout(io.StringIO()) as output:
            self.assertFalse(await sensor_test(Config(api_key="")))
        self.assertEqual(json.loads(output.getvalue())["light"]["lux"], 12.34)


class SensorCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        memory_patch = patch("flora.tools.Memory", return_value=Memory(directory.name + "/flora.sqlite3"))
        memory_patch.start()
        self.addCleanup(memory_patch.stop)

    def test_cli_exit_codes(self):
        for healthy in (False, True):
            result = Sensors()._empty_status()
            if healthy:
                for name in SENSOR_NAMES:
                    result[name]["status"] = "ok"
            with patch("sys.argv", ["flora", "sensor-test"]), \
                    patch.object(Config, "load", return_value=Config(api_key="")), \
                    patch.object(Sensors, "_read", return_value=result), redirect_stdout(io.StringIO()):
                if healthy:
                    main()
                else:
                    with self.assertRaises(SystemExit) as exc:
                        main()
                    self.assertEqual(exc.exception.code, 1)

    def test_stale_battery_env_is_ignored_and_probe_defaults_to_outside_soil(self):
        with patch("flora.config.load_dotenv"), patch.dict("os.environ", {"BATTERY_ENABLED": "true"}, clear=True):
            config = Config.load()
        self.assertFalse(config.soil_probe_in_soil)
        self.assertFalse(hasattr(config, "battery_enabled"))


if __name__ == "__main__":
    unittest.main()
