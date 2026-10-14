import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from datetime import datetime, timedelta

from flora.light_schedule import LightSchedule, next_occurrence
from flora.lights import Lights, LightsError, UdpTransport, describe_state
from flora.tools import Tools, tool_definitions

# Invented addresses and IDs; no real network is touched.
DESK = "AA:00:00:00:00:00:00:01"
SOFA_A = "AA:00:00:00:00:00:00:02"
SOFA_B = "AA:00:00:00:00:00:00:03"


class FakeLamp:
    def __init__(self, device, ip, answers_status=True):
        self.device, self.ip, self.answers_status = device, ip, answers_status
        self.state = {"onOff": 0, "brightness": 50, "color": {"r": 0, "g": 0, "b": 0},
                      "colorTemInKelvin": 2700}

    def apply(self, cmd, data):
        if cmd == "turn":
            self.state["onOff"] = data["value"]
        elif cmd == "brightness":
            self.state["brightness"] = data["value"]
        elif cmd == "colorwc":
            self.state["colorTemInKelvin"] = data["colorTemInKelvin"]
            self.state["color"] = {"r": 0, "g": 0, "b": 0} if data["colorTemInKelvin"] else data["color"]


class FakeTransport:
    def __init__(self, *lamps):
        self.lamps = list(lamps)
        self.sent = []
        self.scans = 0

    def scan(self, seconds):
        self.scans += 1
        return [(lamp.ip, {"ip": lamp.ip, "device": lamp.device, "sku": "H6004"}) for lamp in self.lamps]

    def send(self, ip, cmd, data):
        self.sent.append((ip, cmd, data))
        for lamp in self.lamps:
            if lamp.ip == ip:
                lamp.apply(cmd, data)

    def status(self, ip, seconds):
        for lamp in self.lamps:
            if lamp.ip == ip and lamp.answers_status:
                return dict(lamp.state)
        return None


class LightsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "lights.json"
        self.desk = FakeLamp(DESK, "192.0.2.10")
        self.sofa_a = FakeLamp(SOFA_A, "192.0.2.11")
        self.sofa_b = FakeLamp(SOFA_B, "192.0.2.12")
        self.transport = FakeTransport(self.desk, self.sofa_a, self.sofa_b)
        self.lights = Lights(self.path, self.transport, settle=0)
        sleep = patch("flora.lights.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def name_all(self):
        self.lights.discover()
        for device, name in ((DESK, "desk lamp"), (SOFA_A, "living room lamp A"),
                             (SOFA_B, "living room lamp B")):
            self.assertIn("renamed", self.lights.rename(device, name))

    def test_discovery_names_new_lamps_and_keeps_names_when_addresses_change(self):
        lights, seen = self.lights.discover()
        self.assertEqual(seen, {DESK, SOFA_A, SOFA_B})
        self.assertEqual(sorted(entry["name"] for entry in lights.values()), ["light 1", "light 2", "light 3"])
        self.lights.rename(DESK, "desk lamp")
        self.desk.ip = "192.0.2.99"
        lights, _ = self.lights.discover()
        self.assertEqual(lights[DESK], {**lights[DESK], "name": "desk lamp", "ip": "192.0.2.99"})
        self.assertEqual(json.loads(self.path.read_text())[DESK]["ip"], "192.0.2.99")

    def test_group_words_match_every_lamp_sharing_them(self):
        self.name_all()
        result = self.lights.set("Living Room", brightness_percent=30)
        self.assertEqual(sorted(light["name"] for light in result["lights"]),
                         ["living room lamp A", "living room lamp B"])
        self.assertTrue(all(light["result"] == "confirmed" for light in result["lights"]))
        self.assertEqual(self.desk.state["onOff"], 0)
        self.assertEqual((self.sofa_a.state["onOff"], self.sofa_a.state["brightness"]), (1, 30))

    def test_exact_name_wins_over_group_match(self):
        self.name_all()
        result = self.lights.set("living room lamp a", power="on")
        self.assertEqual([light["name"] for light in result["lights"]], ["living room lamp A"])

    def test_color_and_temperature_commands_are_confirmed_against_reported_state(self):
        self.name_all()
        result = self.lights.set("desk lamp", color="#FF8000")
        self.assertEqual(result["lights"][0]["result"], "confirmed")
        self.assertEqual(result["lights"][0]["reported"]["color"], "#ff8000")
        result = self.lights.set("all", color_temperature_k=6500)
        self.assertEqual([light["result"] for light in result["lights"]], ["confirmed"] * 3)
        self.assertEqual(self.sofa_b.state["colorTemInKelvin"], 6500)

    def test_lamp_that_reports_another_state_is_not_called_confirmed(self):
        self.name_all()
        self.sofa_b.apply = lambda cmd, data: None  # Accepts packets, changes nothing.
        result = self.lights.set("living room lamp B", power="on")
        self.assertEqual(result["lights"][0]["result"], "state_differs")
        self.assertEqual(result["lights"][0]["reported"]["power"], "off")

    def test_silent_lamp_is_unconfirmed_without_rescanning_when_address_unchanged(self):
        self.name_all()
        self.sofa_a.answers_status = False
        scans = self.transport.scans
        result = self.lights.set("living room lamp A", power="on")
        self.assertEqual(result["lights"][0]["result"], "sent_unconfirmed")
        self.assertIsNone(result["lights"][0]["reported"])
        self.assertEqual(self.transport.scans, scans + 1)
        self.assertEqual(self.sofa_a.state["onOff"], 1)

    def test_lamp_with_new_address_is_found_and_commanded_again(self):
        self.name_all()
        self.sofa_a.ip = "192.0.2.50"  # Router reassigned it; the saved address is stale.
        result = self.lights.set("living room lamp A", power="on", brightness_percent=80)
        self.assertEqual(result["lights"][0]["result"], "confirmed")
        self.assertIn(("192.0.2.50", "brightness", {"value": 80}), self.transport.sent)

    def test_turning_off_sends_only_power(self):
        self.name_all()
        self.lights.set("all", power="on")
        self.transport.sent.clear()
        result = self.lights.set("all", power="off")
        self.assertEqual({cmd for _, cmd, _ in self.transport.sent}, {"turn"})
        self.assertTrue(all(light["result"] == "confirmed" for light in result["lights"]))

    def test_unknown_lamp_lists_available_names(self):
        self.name_all()
        result = self.lights.set("kitchen", power="on")
        self.assertEqual(result["error"], "no_matching_light")
        self.assertIn("desk lamp", result["available"])
        self.assertEqual(self.transport.sent, [])

    def test_conflicting_or_empty_requests_are_rejected_before_sending(self):
        self.name_all()
        self.assertEqual(self.lights.set("all")["error"], "nothing_to_change")
        self.assertEqual(self.lights.set("all", power="off", brightness_percent=5)["error"],
                         "cannot_adjust_and_turn_off")
        self.assertEqual(self.lights.set("all", color="#ffffff", color_temperature_k=3000)["error"],
                         "choose_color_or_temperature")
        for bad in ({"color": "orange"}, {"brightness_percent": 0}, {"color_temperature_k": 1000},
                    {"power": "dim"}, {"brightness_percent": True}):
            with self.assertRaises(ValueError):
                self.lights.set("all", **bad)
        self.assertEqual(self.transport.sent, [])

    def test_rename_requires_one_lamp_and_an_unused_name(self):
        self.name_all()
        self.assertEqual(self.lights.rename("living room", "sofa")["error"], "ambiguous_light")
        self.assertEqual(self.lights.rename("desk lamp", "Living-Room Lamp a")["error"], "name_in_use")
        self.assertEqual(self.lights.rename("192.0.2.10", "reading lamp"),
                         {"renamed": "desk lamp", "to": "reading lamp"})
        for bad in ("all", "  ", "x" * 61):
            with self.assertRaises(ValueError):
                self.lights.rename("reading lamp", bad)

    def test_identify_flashes_and_restores_a_lamp_that_was_off(self):
        self.name_all()
        self.lights.set("living room lamp B", power="on")
        result = self.lights.identify("living room")
        self.assertEqual(sorted(result["flashed"]), ["living room lamp A", "living room lamp B"])
        self.assertEqual(self.sofa_a.state["onOff"], 0)
        self.assertEqual(self.sofa_b.state["onOff"], 1)

    def test_unreadable_saved_lamps_are_reported_and_never_overwritten(self):
        self.path.write_text("{not json")
        with self.assertRaises(Exception) as caught:
            self.lights.discover()
        self.assertEqual(caught.exception.code, "light_list_unreadable")
        self.assertEqual(self.path.read_text(), "{not json")

    def test_describe_state_uses_tool_units(self):
        self.assertEqual(describe_state({"onOff": 1, "brightness": 40, "color": {"r": 255, "g": 0, "b": 16},
                                         "colorTemInKelvin": 0}),
                         {"power": "on", "brightness_percent": 40, "color": "#ff0010",
                          "color_temperature_k": None})
        self.assertIsNone(describe_state(None))


class FakeSocket:
    def __init__(self, packets):
        self.packets = list(packets)

    def settimeout(self, seconds):
        pass

    def recvfrom(self, size):
        if not self.packets:
            raise TimeoutError
        packet = self.packets.pop(0)
        if isinstance(packet, BaseException):
            raise packet
        return packet


class TransportTests(unittest.TestCase):
    def test_replies_skip_garbage_and_windows_resets(self):
        good = json.dumps({"msg": {"cmd": "devStatus", "data": {"onOff": 1}}}).encode()
        sock = FakeSocket([ConnectionResetError(), (b"{oops", ("192.0.2.1", 4003)),
                           (b'{"msg": "x"}', ("192.0.2.1", 4003)), (good, ("192.0.2.2", 4003))])
        self.assertEqual(list(UdpTransport.replies(sock, 5)), [("192.0.2.2", "devStatus", {"onOff": 1})])


class LightToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.transport = FakeTransport(FakeLamp(DESK, "192.0.2.10"))
        self.lights = Lights(Path(directory.name) / "lights.json", self.transport, settle=0)
        self.schedule = LightSchedule(self.lights, Path(directory.name) / "light_schedules.json")
        self.tools = Tools(None, memory=object(), lights=self.lights, schedule=self.schedule)

    def test_light_tools_are_only_advertised_when_enabled(self):
        disabled = {tool["name"] for tool in tool_definitions()}
        enabled = {tool["name"] for tool in tool_definitions(True)}
        self.assertFalse(disabled & {"get_lights", "set_lights", "schedule_lights"})
        self.assertEqual(enabled - disabled, {"get_lights", "set_lights", "schedule_lights",
                                              "get_light_schedules", "cancel_light_schedule"})

    async def test_disabled_lights_are_an_unknown_tool(self):
        result = await Tools(None, memory=object()).execute("set_lights", {"target": "all", "power": "on"})
        self.assertEqual(result, {"error": "unknown_tool"})

    async def test_get_lights_hides_network_details(self):
        result = await self.tools.execute("get_lights", {})
        self.assertEqual(result["lights"][0].keys(), {"name", "reachable", "state"})

    async def test_set_lights_through_the_dispatcher(self):
        with patch("flora.lights.time.sleep"):
            result = await self.tools.execute("set_lights", {"target": "all", "brightness_percent": 20})
            self.assertEqual(result["lights"][0]["result"], "confirmed")
            bad = await self.tools.execute("set_lights", {"target": "all", "brightness_percent": 500})
            self.assertEqual(bad, {"error": "invalid_arguments"})

    async def test_network_failure_is_not_reported_as_storage(self):
        def unreachable(*args):
            raise OSError("network is unreachable")
        self.transport.scan = unreachable
        self.assertEqual(await self.tools.execute("get_lights", {}), {"error": "network_unavailable"})

    async def test_unexpected_tool_failure_returns_an_output_instead_of_ending_the_session(self):
        def broken():
            raise KeyError("surprise")
        self.lights.report = broken
        self.assertEqual(await self.tools.execute("get_lights", {}), {"error": "tool_failed"})

    async def test_schedule_tools_round_trip(self):
        result = await self.tools.execute("schedule_lights", {"target": "all", "in_minutes": 5, "power": "off"})
        schedule_id = result["scheduled"]["schedule_id"]
        listed = await self.tools.execute("get_light_schedules", {})
        self.assertEqual([entry["schedule_id"] for entry in listed["schedules"]], [schedule_id])
        self.assertEqual(await self.tools.execute("cancel_light_schedule", {"schedule_id": schedule_id}),
                         {"cancelled": schedule_id})
        self.assertEqual(await self.tools.execute("cancel_light_schedule", {"schedule_id": schedule_id}),
                         {"error": "no_such_schedule"})




class ScheduleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.desk = FakeLamp(DESK, "192.0.2.10")
        self.sofa = FakeLamp(SOFA_A, "192.0.2.11")
        self.transport = FakeTransport(self.desk, self.sofa)
        self.lights = Lights(Path(directory.name) / "lights.json", self.transport, settle=0)
        self.lights.discover()
        self.lights.rename(DESK, "desk lamp")
        self.lights.rename(SOFA_A, "living room lamp")
        self.clock = datetime(2026, 10, 13, 21, 0).astimezone()  # Timers use the Pi's local zone.
        self.path = Path(directory.name) / "light_schedules.json"
        self.schedule = LightSchedule(self.lights, self.path, now=lambda: self.clock)
        sleep = patch("flora.lights.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_one_time_timer_fires_once_then_disappears(self):
        self.lights.set("all", power="on")
        result = self.schedule.add("living room", in_minutes=30, power="off")
        self.assertEqual(result["lights_now_matching"], ["living room lamp"])
        self.assertEqual(self.schedule.run_due(), [])
        self.clock += timedelta(minutes=30)
        [(_, fired)] = self.schedule.run_due()
        self.assertEqual(fired["lights"][0]["result"], "confirmed")
        self.assertEqual((self.sofa.state["onOff"], self.desk.state["onOff"]), (0, 1))
        self.assertEqual(self.schedule.list()["schedules"], [])
        self.assertEqual(self.schedule.run_due(), [])

    def test_daily_timer_moves_to_the_next_day_after_firing(self):
        self.schedule.add("all", at="22:30", repeat="daily", brightness_percent=10)
        self.clock = self.clock.replace(hour=22, minute=30, second=5)
        self.assertEqual(len(self.schedule.run_due()), 1)
        [entry] = self.schedule.list()["schedules"]
        self.assertTrue(entry["next_run_local"].startswith("2026-10-14T22:30"))

    def test_clock_time_already_past_today_means_tomorrow(self):
        result = self.schedule.add("all", at="07:00", power="on")
        self.assertTrue(result["scheduled"]["next_run_local"].startswith("2026-10-14T07:00"))

    def test_timer_missed_while_flora_was_off_is_skipped_not_fired_late(self):
        self.schedule.add("all", in_minutes=5, power="on")
        self.schedule.add("all", at="21:05", repeat="daily", power="on")
        self.clock += timedelta(hours=2)
        self.assertEqual(self.schedule.run_due(), [])
        self.assertEqual(self.transport.sent, [])
        [daily] = self.schedule.list()["schedules"]
        self.assertEqual(daily["repeat"], "daily")

    def test_invalid_timers_are_rejected_when_requested(self):
        with self.assertRaises(LightsError):
            self.schedule.add("all", power="on")
        with self.assertRaises(LightsError):
            self.schedule.add("all", at="22:00", in_minutes=5, power="on")
        with self.assertRaises(LightsError):
            self.schedule.add("all", in_minutes=5, repeat="daily", power="on")
        with self.assertRaises(LightsError):
            self.schedule.add("all", in_minutes=5)
        with self.assertRaises(ValueError):
            self.schedule.add("all", at="25:00", power="on")
        self.assertEqual(self.schedule.add("kitchen", in_minutes=5, power="on")["error"], "no_matching_light")
        self.assertFalse(self.path.exists())

    def test_next_occurrence_is_strictly_after(self):
        after = datetime(2026, 10, 13, 21, 0).astimezone()
        self.assertEqual(next_occurrence("21:00", after).date(), after.date() + timedelta(days=1))
        self.assertEqual(next_occurrence("21:01", after).date(), after.date())


if __name__ == "__main__":
    unittest.main()
