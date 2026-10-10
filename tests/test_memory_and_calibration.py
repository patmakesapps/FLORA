import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from flora.memory import Memory
from flora.tools import Tools, TOOL_REGISTRY, function_tool, calibration_summary, watering_advice
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

    def test_profile_updates_preserve_unsupplied_fields_and_invalidate_changed_pot(self):
        self.memory.set_plant_profile(species="pothos", name="Pip", potting_mix="bark/perlite", probe_in_soil=True)
        self.memory.save_calibration("water_now", 450, 2, "test-time")
        self.memory.save_calibration("watered", 850, 3, "test-time")
        self.memory.set_plant_profile(location="bedroom")
        self.assertEqual(self.memory.snapshot()["plant"]["name"], "Pip")
        self.assertEqual(len(self.memory.snapshot()["soil_calibration"]), 2)
        for changed in ({"species": "fern"}, {"potting_mix": "new soil"}, {"probe_in_soil": False}):
            self.memory.save_calibration("water_now", 450, 2, "test-time")
            self.assertTrue(self.memory.set_plant_profile(**changed)["calibration_reset"])
            self.assertEqual(self.memory.snapshot()["soil_calibration"], {})
        with self.assertRaises(ValueError):
            self.memory.set_plant_profile(unknown="value")
        with self.assertRaises(ValueError):
            self.memory.set_plant_profile(probe_in_soil="true")
        with self.assertRaises(ValueError):
            self.memory.remember("", "a fact")

    def test_multiple_connections_can_save_and_retrieve_without_shared_sqlite_connections(self):
        self.memory.snapshot()  # Initialize schema before simultaneous writes.
        def write(index):
            Memory(self.path).remember(f"person:{index}", f"Name {index}", "person")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(write, range(12)))
        self.assertEqual(len(self.memory.recall()["memories"]), 12)

    def test_final_calibration_save_rechecks_profile_and_separation_atomically(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        old_profile = self.memory.snapshot()["plant"]
        self.memory.set_plant_profile(species="fern")
        result = self.memory.save_calibration("water_now", 450, 2, "time", old_profile)
        self.assertEqual(result["error"], "plant_changed_during_capture")
        self.assertEqual(self.memory.snapshot()["soil_calibration"], {})
        self.memory.save_calibration("water_now", 450, 2, "time")
        result = self.memory.save_calibration("watered", 450, 2, "time")
        self.assertEqual(result["error"], "indistinct_calibration_points")
        self.assertEqual(set(self.memory.snapshot()["soil_calibration"]), {"water_now"})


class WateringTests(unittest.TestCase):
    def setUp(self):
        self.points = {
            "water_now": {"raw": 450, "spread": 2, "measured_at": "test-time"},
            "watered": {"raw": 850, "spread": 3, "measured_at": "test-time"},
        }
        self.plant = {"species": "pothos"}
        self.soil = {"status": "ok", "probe_in_soil": True, "raw_capacitance": 300}

    def test_recommendations_use_this_pots_benchmark_and_a_boundary_uncertainty_band(self):
        calibration = calibration_summary(self.points)
        self.assertEqual(calibration["status"], "ready")
        for raw, expected in ((300, "water"), (450, "check_soil"), (460, "check_soil"), (750, "wait")):
            self.soil["raw_capacitance"] = raw
            advice = watering_advice(self.soil, self.plant, calibration)
            self.assertEqual(advice["recommendation"], expected)
            self.assertNotIn("moisture_percent", advice)

    def test_missing_failed_outside_probe_or_invalid_calibration_never_yields_watering_claim(self):
        ready = calibration_summary(self.points)
        for soil, plant, calibration in (
            ({**self.soil, "probe_in_soil": False}, self.plant, ready),
            ({**self.soil, "status": "unavailable", "raw_capacitance": None}, self.plant, ready),
            (self.soil, {}, ready),
            (self.soil, self.plant, calibration_summary({})),
        ):
            self.assertEqual(watering_advice(soil, plant, calibration)["recommendation"], "unknown")
        for wet, spread in ((450, 2), (400, 2), (460, 20)):
            points = {**self.points, "watered": {"raw": wet, "spread": spread}}
            self.assertEqual(calibration_summary(points)["status"], "invalid")


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

    async def test_calibration_requires_confirmation_actual_placement_and_species(self):
        for arguments, expected in (
            ({"point": "water_now", "confirmed": False}, "physical_condition_not_confirmed"),
            ({"point": "water_now", "confirmed": True}, "probe_outside_soil"),
        ):
            result = await self.tools.execute("calibrate_soil", arguments)
            self.assertEqual(result["error"], expected)
        await self.tools.execute("set_plant_profile", {"probe_in_soil": True})
        result = await self.tools.execute("calibrate_soil", {"point": "water_now", "confirmed": True})
        self.assertEqual(result["error"], "plant_type_required")
        self.sensors.sample_soil.assert_not_awaited()
        self.assertEqual(self.memory.snapshot()["soil_calibration"], {})

    async def test_two_live_points_enable_advice_after_restart_and_reset_disables_it(self):
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
        self.assertEqual(status["watering"]["recommendation"], "water")
        self.assertTrue(status["soil"]["calibrated"])
        self.assertTrue(status["soil"]["probe_in_soil"])
        self.assertEqual(status["plant"]["species"], "pothos")
        self.assertEqual(status["soil"]["raw_capacitance"], 300)
        await reopened.execute("calibrate_soil", {"point": "reset", "confirmed": True})
        self.assertEqual((await reopened.get_plant_status())["watering"]["recommendation"], "unknown")

    async def test_failed_unstable_reversed_samples_do_not_change_existing_calibration(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        self.memory.save_calibration("water_now", 450, 2, "test-time")
        for sample, expected in (
            ({"error": "i2c_error"}, "i2c_error"),
            ({"raw": 850, "spread": 200, "measured_at": "time"}, "unstable_soil_readings"),
            ({"raw": 400, "spread": 2, "measured_at": "time"}, "indistinct_calibration_points"),
        ):
            self.sensors.sample_soil.return_value = sample
            result = await self.tools.execute("calibrate_soil", {"point": "watered", "confirmed": True})
            self.assertFalse(result["saved"])
            self.assertEqual(result["error"], expected)
            self.assertEqual(set(self.memory.snapshot()["soil_calibration"]), {"water_now"})

    async def test_plant_change_during_capture_discards_the_reading(self):
        self.memory.set_plant_profile(species="pothos", probe_in_soil=True)
        async def change_plant():
            self.memory.set_plant_profile(species="fern")
            return {"raw": 450, "spread": 2, "measured_at": "time"}
        self.sensors.sample_soil.side_effect = change_plant
        result = await self.tools.execute("calibrate_soil", {"point": "water_now", "confirmed": True})
        self.assertEqual(result["error"], "plant_changed_during_capture")
        self.assertEqual(self.memory.snapshot()["soil_calibration"], {})

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
        with patch.object(self.memory, "snapshot", side_effect=OSError("unavailable")):
            result = await self.tools.get_plant_status()
        self.assertEqual(result["light"]["lux"], 56.29)
        self.assertEqual(result["watering"]["recommendation"], "unknown")
        with patch.object(self.memory, "remember", side_effect=OSError("unavailable")):
            result = await self.tools.execute("remember", {"subject": "name", "content": "Pat"})
        self.assertEqual(result["error"], "storage_unavailable")
        self.assertNotIn("saved", result)


if __name__ == "__main__":
    unittest.main()
