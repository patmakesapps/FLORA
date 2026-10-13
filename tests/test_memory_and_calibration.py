import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from flora.memory import Memory
from flora.tools import Tools, TOOL_REGISTRY, function_tool, calibration_summary, soil_comparison
from flora.config import Config
from flora.realtime import session_update
from flora.sensors import Sensors


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data" / "flora.sqlite3"
        self.memory = Memory(self.path)

    def test_names_and_plant_details_survive_new_instances_and_corrections(self):
        self.memory.remember("person:Pat", "Pat's name is Pat", "person")
        self.memory.set_plant_profile(name="Fernie", species="Boston fern", location="desk")
        reopened = Memory(self.path)
        self.assertEqual(reopened.recall("name")["memories"][0]["content"], "Pat's name is Pat")
        self.assertEqual(reopened.snapshot()["plant"]["species"], "Boston fern")
        reopened.remember("PERSON:pat", "Pat prefers Patrick", "person")
        memories = reopened.recall("Patrick")["memories"]
        self.assertEqual(len(memories), 1)
        self.assertEqual(len(reopened.recall()["memories"]), 1)
        self.assertEqual(memories[0]["kind"], "person")
        self.assertIsNotNone(memories[0]["created_at"])
        self.assertTrue(reopened.forget("person:pat")["deleted"])
        self.assertFalse(reopened.forget("person:pat")["deleted"])
        self.assertEqual(reopened.recall()["memories"], [])

    def test_keyword_search_is_literal_bounded_and_sql_is_parameterized(self):
        self.memory.remember("O'Brien", "likes 100% organic potting_mix")
        self.memory.remember("other", "likes other soil")
        self.assertEqual(len(self.memory.recall("100%")["memories"]), 1)
        self.assertEqual(len(self.memory.recall("potting_mix")["memories"]), 1)
        # The apostrophe can match O'Brien literally, but the SQL-looking query
        # must not turn into an expression that returns every row.
        self.assertEqual({row["subject"] for row in self.memory.recall("' OR 1=1 --")["memories"]}, {"O'Brien"})
        self.assertEqual(self.memory.recall("O'Brien")["memories"][0]["subject"], "O'Brien")
        for index in range(25):
            self.memory.remember(f"fact:{index}", "test fact")
        self.assertEqual(len(self.memory.recall()["memories"]), 20)

    def test_profile_corrections_preserve_references_and_explicit_setup_archives_them(self):
        self.memory.set_plant_profile(species="pothos", name="Pip", potting_mix="bark/perlite", probe_in_soil=True)
        self.memory.save_calibration("water_now", 450, 2, "test-time")
        self.memory.save_calibration("watered", 850, 3, "test-time")
        self.memory.set_plant_profile(location="bedroom")
        self.assertEqual(self.memory.snapshot()["plant"]["name"], "Pip")
        self.assertEqual(len(self.memory.snapshot()["soil_calibration"]), 2)
        for changed in ({"species": "Pothos"}, {"potting_mix": "bark and perlite"}, {"probe_in_soil": False}):
            self.assertFalse(self.memory.set_plant_profile(**changed)["calibration_reset"])
            self.assertEqual(len(self.memory.snapshot()["soil_calibration"]), 2)
        self.assertTrue(self.memory.set_plant_profile(new_setup=True, species="fern")["calibration_reset"])
        self.assertEqual(self.memory.snapshot()["soil_calibration"], {})
        self.assertEqual(self.memory.observation_history()["observations"], [])
        self.assertEqual(len(self.memory.observation_history(include_previous_setups=True)["observations"]), 2)
        with self.assertRaises(ValueError):
            self.memory.set_plant_profile(unknown="value")
        with self.assertRaises(ValueError):
            self.memory.set_plant_profile(probe_in_soil="true")
        with self.assertRaises(ValueError):
            self.memory.remember("", "a fact")

    def test_preferences_survive_recent_fact_window_and_unrelated_searches(self):
        self.memory.remember("units", "Use Celsius", "preference")
        for index in range(25):
            self.memory.remember(f"event:{index}", "Plant care event")
        recalled = Memory(self.path).recall("event")
        self.assertEqual(len(recalled["memories"]), 20)
        self.assertEqual(recalled["preferences"][0]["content"], "Use Celsius")
        self.memory.remember("units", "Use Fahrenheit", "preference")
        self.assertEqual(self.memory.recall()["preferences"][0]["content"], "Use Fahrenheit")
        self.memory.forget("units")
        self.assertEqual(self.memory.recall()["preferences"], [])

    def test_multiple_connections_can_save_and_retrieve_without_shared_sqlite_connections(self):
        self.memory.snapshot()  # Initialize schema before simultaneous writes.
        def write(index):
            Memory(self.path).remember(f"person:{index}", f"Name {index}", "person")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(write, range(12)))
        self.assertEqual(len(self.memory.recall()["memories"]), 12)

    def test_changed_context_keeps_observation_without_promoting_and_overlap_is_saved(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        old_profile = self.memory.snapshot()["plant"]
        self.memory.set_plant_profile(species="fern")
        result = self.memory.save_calibration("water_now", 450, 2, "time", old_profile)
        self.assertTrue(result["saved"])
        self.assertTrue(result["context_changed_during_capture"])
        self.assertFalse(result["reference_saved"])
        self.assertEqual(self.memory.snapshot()["soil_calibration"], {})
        self.memory.save_calibration("water_now", 450, 2, "time")
        result = self.memory.save_calibration("watered", 450, 2, "time")
        self.assertTrue(result["saved"])
        self.assertTrue(result["reference_saved"])
        self.assertEqual(set(self.memory.snapshot()["soil_calibration"]), {"water_now", "watered"})

    def test_legacy_wet_reference_migrates_once_and_survives_restart_and_reset(self):
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TABLE soil_calibration (point TEXT PRIMARY KEY, raw INTEGER, spread INTEGER, measured_at TEXT)")
            conn.execute("INSERT INTO soil_calibration VALUES ('watered', 1008, 25, 'original-time')")
            conn.commit()
        for _ in range(2):
            reopened = Memory(self.path)
            self.assertEqual(reopened.snapshot()["soil_calibration"]["watered"]["raw"], 1008)
            history = reopened.observation_history()["observations"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["data"]["sample"]["measured_at"], "original-time")
            self.assertEqual(history[0]["data"]["source"], "legacy_calibration")
        self.memory.reset_calibration()
        self.assertEqual(self.memory.observation_history()["observations"], [])
        self.assertEqual(len(self.memory.observation_history(include_previous_setups=True)["observations"]), 1)

    def test_history_pages_preserve_raw_samples_profiles_and_replaced_references(self):
        self.memory.set_plant_profile(species="fern")
        self.memory.save_calibration("watered", 850, 3, "first-time")
        self.memory.save_calibration("watered", 870, 4, "second-time")
        page = self.memory.observation_history(limit=1)
        self.assertEqual(page["observations"][0]["data"]["sample"]["raw"], 870)
        earlier = self.memory.observation_history(limit=1, before_id=page["next_before_id"])
        self.assertEqual(earlier["observations"][0]["data"]["sample"]["raw"], 850)
        self.assertEqual(earlier["observations"][0]["plant"]["species"], "fern")
        self.assertIsNone(earlier["next_before_id"])
        for invalid in ({"limit": 101}, {"limit": True}, {"before_id": -1}, {"include_previous_setups": "true"}):
            with self.assertRaises(ValueError):
                self.memory.observation_history(**invalid)


class SoilComparisonTests(unittest.TestCase):
    def setUp(self):
        self.points = {
            "water_now": {"raw": 450, "spread": 2, "measured_at": "test-time"},
            "watered": {"raw": 850, "spread": 3, "measured_at": "test-time"},
        }
        self.soil = {"status": "ok", "probe_in_soil": True, "raw_capacitance": 300}

    def test_numeric_comparison_retains_uncertainty_without_a_care_decision(self):
        calibration = calibration_summary(self.points)
        self.assertEqual(calibration["status"], "ready")
        for raw, expected in ((300, "below"), (450, "within_uncertainty"),
                              (460, "within_uncertainty"), (750, "above")):
            self.soil["raw_capacitance"] = raw
            comparison = soil_comparison(self.soil, calibration)
            self.assertEqual(comparison["relative_to_reference"], expected)
            self.assertEqual(comparison["difference_counts"], raw - 450)
            self.assertEqual(comparison["uncertainty_counts"], 20)
            self.assertNotIn("recommendation", comparison)
            self.assertNotIn("moisture_percent", comparison)

    def test_only_missing_readings_or_references_prevent_numeric_comparison(self):
        ready = calibration_summary(self.points)
        for soil, calibration, reason in (
            ({**self.soil, "status": "unavailable", "raw_capacitance": None}, ready, "soil_reading_unavailable"),
            (self.soil, calibration_summary({}), "no_soil_references"),
        ):
            comparison = soil_comparison(soil, calibration)
            self.assertEqual(comparison, {"status": "unavailable", "reason_code": reason})
        for wet, spread in ((450, 2), (400, 2), (460, 20)):
            points = {**self.points, "watered": {"raw": wet, "spread": spread, "measured_at": "time"}}
            self.assertEqual(calibration_summary(points)["status"], "provisional")
            comparison = soil_comparison(self.soil, calibration_summary(points))
            self.assertEqual(comparison["status"], "available")
            self.assertIn("indistinct_calibration_points", comparison["warnings"])
        comparison = soil_comparison({**self.soil, "probe_in_soil": False}, ready)
        self.assertEqual(comparison["status"], "available")
        self.assertIn("probe_outside_soil", comparison["warnings"])

    def test_wet_only_reference_is_comparable_and_current_sample_noise_is_included(self):
        calibration = calibration_summary({"watered": self.points["watered"]})
        comparison = soil_comparison({**self.soil, "sample_spread": 100}, calibration)
        self.assertEqual(comparison["reference_point"], "watered")
        self.assertEqual(comparison["difference_counts"], -550)
        self.assertEqual(comparison["reference_status"], "incomplete")
        self.assertEqual(comparison["uncertainty_counts"], 200)


class PlantToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.memory = Memory(Path(self.directory.name) / "flora.sqlite3")
        self.sensors = SimpleNamespace(
            soil_probe_in_soil=False,
            get_plant_status=AsyncMock(side_effect=lambda: Sensors()._empty_status()),
            sample_soil=AsyncMock(return_value={"samples": [450, 451, 449, 450, 450],
                                               "raw": 450, "spread": 2, "measured_at": "test-time"}),
        )
        self.tools = Tools(self.sensors, self.memory)

    async def test_registry_is_the_source_for_advertised_definitions_and_dispatch(self):
        async def registry_probe(runner):
            return {"same_memory": runner.memory is self.memory}
        entry = function_tool(registry_probe, "Test registration")
        with patch.dict(TOOL_REGISTRY, registry_probe=entry):
            advertised = session_update(Config(api_key="test"))["session"]["tools"]
            definition = next(tool for tool in advertised if tool["name"] == "registry_probe")
            self.assertEqual(definition, entry["definition"])
            self.assertNotIn("handler", definition)
            self.assertEqual(await self.tools.execute("registry_probe", {}), {"same_memory": True})
        self.assertEqual(await self.tools.execute("registry_probe", {}), {"error": "unknown_tool"})
        self.assertEqual(await self.tools.execute("snapshot", {}), {"error": "unknown_tool"})

    async def test_first_use_samples_without_species_placement_or_confirmation(self):
        result = await self.tools.execute("calibrate_soil", {})
        self.assertTrue(result["saved"])
        self.assertTrue(result["measurement_available"])
        self.assertFalse(result["reference_saved"])
        self.assertIn("probe_outside_soil", result["warnings"])
        result = await self.tools.execute("calibrate_soil", {"point": "water_now", "confirmed": False})
        self.assertTrue(result["saved"])
        self.assertIn("physical_condition_not_confirmed", result["warnings"])
        reference = self.memory.snapshot()["soil_calibration"]["water_now"]
        self.assertFalse(reference["confirmed"])
        self.assertFalse(reference["probe_in_soil"])
        self.assertEqual(self.sensors.sample_soil.await_count, 2)
        self.assertEqual(len(self.memory.observation_history()["observations"]), 2)
        # Installed but still unidentified plants can collect confirmed references too.
        await self.tools.set_plant_profile(probe_in_soil=True)
        result = await self.tools.calibrate_soil("watered", True)
        self.assertTrue(result["reference_saved"])

    async def test_two_live_points_enable_comparison_after_restart_and_reset_disables_it(self):
        await self.tools.execute("set_plant_profile", {"species": "pothos", "probe_in_soil": True})
        first = await self.tools.execute("calibrate_soil", {"point": "water_now", "confirmed": True})
        self.assertTrue(first["saved"])
        self.assertEqual(first["calibration"]["status"], "incomplete")
        self.sensors.sample_soil.return_value = {
            "samples": [850, 851, 849, 850, 850], "raw": 850, "spread": 2, "measured_at": "later-time",
        }
        second = await self.tools.execute("calibrate_soil", {"point": "watered", "confirmed": True})
        self.assertTrue(second["saved"])
        self.assertEqual(second["calibration"]["status"], "ready")
        def live():
            result = Sensors()._empty_status()
            result["soil"].update(status="ok", raw_capacitance=300, error=None, measured_at="now")
            return result
        self.sensors.get_plant_status.side_effect = live
        reopened = Tools(self.sensors, Memory(self.memory.path))
        status = await reopened.execute("get_plant_status", {})
        self.assertEqual(status["soil_comparison"]["relative_to_reference"], "below")
        self.assertNotIn("watering", status)
        self.assertNotIn("calibrated", status["soil"])
        self.assertEqual(status["calibration"]["status"], "ready")
        self.assertTrue(status["soil"]["probe_in_soil"])
        self.assertEqual(status["plant"]["species"], "pothos")
        self.assertEqual(status["soil"]["raw_capacitance"], 300)
        await reopened.execute("calibrate_soil", {"point": "reset", "confirmed": True})
        self.assertEqual((await reopened.get_plant_status())["soil_comparison"]["reason_code"], "no_soil_references")

    async def test_status_exposes_persisted_profile_preferences_and_recent_context(self):
        self.memory.set_plant_profile(species="fern", notes="Newly purchased")
        self.memory.remember("style", "Explain in everyday terms", "preference")
        for index in range(25):
            self.memory.remember(f"event:{index}", "User-reported plant care")
        result = await Tools(self.sensors, Memory(self.memory.path)).get_plant_status()
        self.assertEqual(result["plant"]["species"], "fern")
        self.assertEqual(result["preferences"][0]["content"], "Explain in everyday terms")
        self.assertEqual(len(result["memories"]), 20)
        self.assertNotIn("watering", result)

    async def test_failures_are_recorded_and_noisy_reversed_samples_are_saved_with_caveats(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        self.memory.save_calibration("water_now", 450, 2, "test-time")
        for sample, expected in (
            ({"error": "i2c_error"}, "i2c_error"),
            ({"raw": 850, "spread": 200, "measured_at": "time"}, "unstable_soil_readings"),
            ({"raw": 400, "spread": 2, "measured_at": "time"}, "indistinct_calibration_points"),
        ):
            self.sensors.sample_soil.return_value = sample
            result = await self.tools.execute("calibrate_soil", {"point": "watered", "confirmed": True})
            self.assertTrue(result["saved"])
            if expected == "i2c_error":
                self.assertFalse(result["measurement_available"])
                self.assertFalse(result["reference_saved"])
                self.assertEqual(result["sample"]["error"], expected)
                self.assertEqual(set(self.memory.snapshot()["soil_calibration"]), {"water_now"})
            else:
                self.assertTrue(result["reference_saved"])
                self.assertIn(expected, result["calibration"]["warnings"])
                self.assertEqual(result["calibration"]["status"], "provisional")
        self.assertEqual(len(self.memory.observation_history()["observations"]), 4)

    async def test_setup_change_during_capture_archives_reading_with_original_plant(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        async def change_plant():
            self.memory.set_plant_profile(new_setup=True, species="fern")
            return {"raw": 450, "spread": 2, "measured_at": "time"}
        self.sensors.sample_soil.side_effect = change_plant
        result = await self.tools.execute("calibrate_soil", {"point": "water_now", "confirmed": True})
        self.assertTrue(result["saved"])
        self.assertFalse(result["reference_saved"])
        self.assertIn("plant_changed_during_capture", result["warnings"])
        self.assertEqual(self.memory.snapshot()["soil_calibration"], {})
        self.assertEqual(self.memory.observation_history()["observations"], [])
        previous = self.memory.observation_history(include_previous_setups=True)["observations"][0]
        self.assertEqual(previous["plant"]["species"], "pothos")
        self.assertTrue(previous["data"]["context_changed_during_capture"])

    async def test_repeatable_medians_keep_full_noise_in_saved_anchors_and_comparison(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        self.sensors.sample_soil.return_value = {
            "raw": 450, "spread": 80, "median_spread": 17, "measured_at": "time"}
        result = await self.tools.execute("calibrate_soil", {"point": "water_now", "confirmed": True})
        self.assertTrue(result["saved"])
        self.assertEqual(self.memory.snapshot()["soil_calibration"]["water_now"]["spread"], 80)
        # Smoothing must not make overlapping/noisy anchors look distinct.
        self.sensors.sample_soil.return_value = {
            "raw": 600, "spread": 80, "median_spread": 17, "measured_at": "time"}
        result = await self.tools.execute("calibrate_soil", {"point": "watered", "confirmed": True})
        self.assertTrue(result["saved"])
        self.assertIn("indistinct_calibration_points", result["calibration"]["warnings"])
        self.sensors.sample_soil.return_value["raw"] = 850
        result = await self.tools.execute("calibrate_soil", {"point": "watered", "confirmed": True})
        self.assertTrue(result["saved"])
        self.assertEqual(result["calibration"]["uncertainty_counts"], 160)

    async def test_drifting_window_medians_are_retained_with_warning(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        self.sensors.sample_soil.return_value = {
            "raw": 800, "spread": 120, "median_spread": 90, "measured_at": "time"}
        result = await self.tools.execute("calibrate_soil", {"point": "water_now", "confirmed": True})
        self.assertTrue(result["saved"])
        self.assertIn("unstable_soil_readings", result["warnings"])
        self.assertEqual(self.memory.observation_history()["observations"][0]["data"]["sample"]["raw"], 800)

    async def test_status_persists_all_sensors_and_history_survives_restart(self):
        def live():
            result = Sensors()._empty_status()
            result["soil"].update(status="ok", raw_capacitance=700, raw_samples=[700] * 5)
            result["light"].update(status="ok", lux=234.5)
            result["environment"].update(status="ok", temperature_c=23.4, relative_humidity_percent=56.7)
            return result
        self.sensors.get_plant_status.side_effect = live
        status = await self.tools.get_plant_status()
        self.assertTrue(status["observation_saved"])
        reopened = Tools(self.sensors, Memory(self.memory.path))
        history = await reopened.execute("get_observation_history", {})
        data = history["observations"][0]["data"]
        self.assertEqual(data["soil"]["raw_samples"], [700] * 5)
        self.assertEqual(data["light"]["lux"], 234.5)
        self.assertEqual(data["environment"]["temperature_c"], 23.4)
        self.assertEqual(data["environment"]["relative_humidity_percent"], 56.7)
        self.assertNotIn("memories", data)

    async def test_wet_first_second_capture_is_saved_even_when_references_overlap(self):
        await self.tools.set_plant_profile(probe_in_soil=True)
        self.sensors.sample_soil.return_value = {
            "raw": 1008, "spread": 25, "median_spread": 1, "measured_at": "first-time"}
        await self.tools.calibrate_soil("watered", True)
        self.sensors.sample_soil.return_value = {
            "raw": 933, "spread": 2, "median_spread": 1, "measured_at": "second-time"}
        result = await self.tools.calibrate_soil("water_now", True)
        self.assertTrue(result["saved"])
        self.assertTrue(result["reference_saved"])
        self.assertEqual(result["calibration"]["status"], "provisional")
        self.assertIn("indistinct_calibration_points", result["calibration"]["warnings"])
        self.assertEqual(len(self.memory.observation_history()["observations"]), 2)

    async def test_logging_reports_operation_without_user_details(self):
        with self.assertLogs("flora.tools", level="INFO") as captured:
            await self.tools.execute("remember", {"subject": "private-name", "content": "private-content"})
        message = " ".join(captured.output)
        self.assertIn("Tool remember", message)
        self.assertIn("saved=True", message)
        self.assertNotIn("private-name", message)
        self.assertNotIn("private-content", message)

    async def test_failed_storage_never_hides_fresh_measurements_or_claims_saved(self):
        with patch.object(self.memory, "record_observation", side_effect=OSError("disk full")):
            result = await self.tools.calibrate_soil()
            self.assertFalse(result["saved"])
            self.assertEqual(result["sample"]["raw"], 450)
            self.assertEqual(result["memory_error"], "storage_unavailable")
            status = await self.tools.get_plant_status()
            self.assertFalse(status["observation_saved"])
            self.assertIn("soil", status)
        with patch.object(self.memory, "snapshot", side_effect=OSError("disk unavailable")):
            result = await self.tools.calibrate_soil()
            self.assertFalse(result["saved"])
            self.assertEqual(result["sample"]["raw"], 450)

    async def test_memory_tool_routing_retrieval_and_bad_arguments(self):
        result = await self.tools.execute("remember", {"subject": "person:Pat", "content": "name is Pat", "kind": "person"})
        self.assertTrue(result["saved"])
        recalled = await self.tools.execute("recall", {"query": "Pat"})
        self.assertEqual(recalled["memories"][0]["content"], "name is Pat")
        self.assertTrue((await self.tools.execute("forget", {"subject": "person:Pat"}))["deleted"])
        for name, arguments in (("get_plant_status", {"fake": 1}), ("remember", {}),
                                ("recall", {"query": 42}), ("remember", None)):
            self.assertEqual((await self.tools.execute(name, arguments))["error"], "invalid_arguments")

    async def test_storage_failure_keeps_live_sensor_measurements_available(self):
        def live():
            result = Sensors()._empty_status()
            result["light"].update(status="ok", lux=56.29)
            return result
        self.sensors.get_plant_status.side_effect = live
        with patch.object(self.memory, "recall", side_effect=OSError("unavailable")):
            result = await self.tools.get_plant_status()
        self.assertEqual(result["light"]["lux"], 56.29)
        self.assertEqual(result["soil_comparison"]["reason_code"], "storage_unavailable")
        with patch.object(self.memory, "remember", side_effect=OSError("unavailable")):
            result = await self.tools.execute("remember", {"subject": "name", "content": "Pat"})
        self.assertEqual(result["error"], "storage_unavailable")
        self.assertNotIn("saved", result)


if __name__ == "__main__":
    unittest.main()
