"""Conversation memory, pot-specific calibration, and live measurement evidence."""
import asyncio
import logging
import math
import sqlite3
from datetime import datetime, timezone

from .lights import KELVIN_RANGE, LightsError
from .memory import Memory

log = logging.getLogger(__name__)


def function_tool(handler, description, properties=None, required=()):
    return {"handler": handler, "definition": {
        "type": "function", "name": handler.__name__, "description": description,
        "parameters": {
            "type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False,
        },
    }}


def calibration_summary(points):
    warnings = sorted({warning for point in points.values() for warning in point.get("warnings", [])})
    for point in points.values():
        if not point.get("confirmed", True):
            warnings.append("physical_condition_not_confirmed")
        if not point.get("probe_in_soil", True):
            warnings.append("reference_probe_outside_soil")
    complete = all(name in points for name in ("water_now", "watered"))
    noise = max((point["spread"] for point in points.values()), default=0)
    span = points["watered"]["raw"] - points["water_now"]["raw"] if complete else None
    # Numerical quality hints, never prerequisites for collecting or saving evidence.
    if complete and span <= max(5, 3 * noise):
        warnings.append("indistinct_calibration_points")
    return {"status": ("provisional" if warnings else "ready") if complete else "incomplete",
            "points": points, "warnings": sorted(set(warnings)), "span_counts": span,
            "noise_counts": noise,
            "uncertainty_counts": max(1, 2 * noise, math.ceil(abs(span or 0) * 0.05))}


def soil_comparison(soil, calibration):
    """Compare measured counts with saved references; the model decides care."""
    if soil["status"] != "ok":
        return {"status": "unavailable", "reason_code": "soil_reading_unavailable"}
    if not calibration["points"]:
        return {"status": "unavailable", "reason_code": "no_soil_references"}
    raw = soil["raw_capacitance"]
    margin = max(calibration["uncertainty_counts"], 2 * soil.get("sample_spread", 0))
    references = {}
    for name, point in calibration["points"].items():
        difference = raw - point["raw"]
        relative = "below" if difference < -margin else "above" if difference > margin else "within_uncertainty"
        references[name] = {"reference_raw": point["raw"], "reference_measured_at": point["measured_at"],
                            "difference_counts": difference, "uncertainty_counts": margin,
                            "relative_to_reference": relative, "confirmed": point.get("confirmed", True)}
    warnings = list(calibration["warnings"])
    if not soil["probe_in_soil"]:
        warnings.append("probe_outside_soil")
    primary = "water_now" if "water_now" in references else "watered"
    return {"status": "available", "reference_point": primary, **references[primary],
            "references": references, "warnings": warnings, "reference_status": calibration["status"],
            "basis": "raw_count_comparison_not_a_care_decision"}


class Tools:
    def __init__(self, sensors, memory=None, lights=None, schedule=None, power=None, volume=None):
        self.sensors = sensors
        self.memory = memory if memory is not None else Memory()
        self.lights = lights  # None unless LIGHTS_ENABLED; light tools are then not offered.
        self.schedule = schedule
        self.power = power  # None unless POWER_OFF_ENABLED.
        self.volume = volume  # None outside a voice session.

    async def get_current_time(self):
        utc = datetime.now(timezone.utc)
        local = utc.astimezone()
        return {
            "local_datetime": local.isoformat(timespec="seconds"),
            "date": local.date().isoformat(),
            "time": local.strftime("%H:%M:%S"),
            "weekday": local.strftime("%A"),
            "timezone_abbreviation": local.tzname(),
            "utc_datetime": utc.isoformat(timespec="seconds"),
            "source": "raspberry_pi_system_clock",
        }

    async def get_plant_status(self):
        try:
            saved = await asyncio.to_thread(self.memory.recall)
        except (sqlite3.Error, OSError):
            result = await self.sensors.get_plant_status()
            result["memory_error"] = "storage_unavailable"
            result["observation_saved"] = False
            result["soil_comparison"] = {"status": "unavailable", "reason_code": "storage_unavailable"}
            return result
        result = await self.sensors.get_plant_status()
        plant = saved["plant"]
        if plant["probe_in_soil"] is not None:
            result["soil"]["probe_in_soil"] = plant["probe_in_soil"]
        # The sensor layer reports raw counts; this layer knows the pot's saved calibration.
        calibration = calibration_summary(saved["soil_calibration"])
        if result["soil"]["probe_in_soil"]:
            result["soil"]["note"] = "Raw capacitive counts for the installed probe, not water-content percentage."
        else:
            result["soil"]["note"] = "Probe is outside soil; counts cannot describe a plant's moisture."
        result.update(plant=plant, calibration=calibration,
                      setup_id=saved["setup_id"],
                      memories=saved["memories"], preferences=saved["preferences"],
                      soil_comparison=soil_comparison(result["soil"], calibration))
        # Store measurements and capture context, not recursively nested memories/history.
        payload = {name: result[name] for name in ("requested_at", "light", "soil", "environment")}
        try:
            stored = await asyncio.to_thread(self.memory.record_observation, "plant_status", payload, saved)
            result.update(observation_saved=True, observation_id=stored["observation_id"],
                          context_changed_during_capture=stored["context_changed_during_capture"])
        except (sqlite3.Error, OSError):
            result.update(observation_saved=False, memory_error="storage_unavailable")
        return result

    async def calibrate_soil(self, point="sample", confirmed=False):
        if type(confirmed) is not bool or point not in {"sample", "water_now", "watered", "reset"}:
            raise ValueError("Invalid soil capture arguments")
        if point == "reset":
            return await asyncio.to_thread(self.memory.reset_calibration)
        try:
            before = await asyncio.to_thread(self.memory.snapshot)
        except (sqlite3.Error, OSError):
            sample = await self.sensors.sample_soil()
            return {"saved": False, "sample": sample, "measurement_available": "error" not in sample,
                    "memory_error": "storage_unavailable"}
        installed = before["plant"]["probe_in_soil"]
        if installed is None:
            installed = self.sensors.soil_probe_in_soil
        sample = await self.sensors.sample_soil()
        warnings = []
        if not installed:
            warnings.append("probe_outside_soil")
        if point != "sample" and not confirmed:
            warnings.append("physical_condition_not_confirmed")
        if "error" not in sample:
            stability_limit = max(10, math.ceil(sample["raw"] * 0.05))
            if sample.get("median_spread", sample["spread"]) > stability_limit:
                warnings.append("unstable_soil_readings")
        payload = {"point": point, "confirmed": confirmed, "probe_in_soil": installed,
                   "sample": sample, "warnings": warnings}
        try:
            stored = await asyncio.to_thread(self.memory.record_observation, "soil_capture", payload, before)
        except (sqlite3.Error, OSError):
            return {"saved": False, **payload, "measurement_available": "error" not in sample,
                    "memory_error": "storage_unavailable"}
        if stored["context_changed_during_capture"]:
            warnings.append("plant_changed_during_capture")
        calibration = calibration_summary(stored["soil_calibration"])
        return {"saved": True, **payload, "observation_id": stored["observation_id"],
                "reference_saved": stored["reference_saved"], "measurement_available": "error" not in sample,
                "calibration": calibration}

    async def get_observation_history(self, limit=20, before_id=None, include_previous_setups=False):
        return await asyncio.to_thread(self.memory.observation_history, limit, before_id, include_previous_setups)

    async def set_plant_profile(self, **fields):
        return await asyncio.to_thread(self.memory.set_plant_profile, **fields)

    async def remember(self, subject, content, kind="fact"):
        return await asyncio.to_thread(self.memory.remember, subject, content, kind)

    async def recall(self, query=""):
        return await asyncio.to_thread(self.memory.recall, query)

    async def forget(self, subject):
        return await asyncio.to_thread(self.memory.forget, subject)

    async def _lights(self, operation, *args, **kwargs):
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except LightsError as exc:
            return {"error": exc.code}
        except OSError:
            # Network failures, not storage: keep them out of execute's storage handler.
            return {"error": "network_unavailable"}

    async def get_lights(self):
        lights = await self._lights(self.lights.report)
        if isinstance(lights, dict):
            return lights
        # IP addresses and device IDs are setup details, not conversation.
        return {"lights": [{key: light[key] for key in ("name", "reachable", "state")} for light in lights]}

    async def set_lights(self, target, power=None, brightness_percent=None, color=None,
                         color_temperature_k=None):
        return await self._lights(self.lights.set, target, power, brightness_percent, color,
                                  color_temperature_k)

    async def schedule_lights(self, target, at=None, in_minutes=None, repeat="once", power=None,
                              brightness_percent=None, color=None, color_temperature_k=None):
        return await self._lights(self.schedule.add, target, at, in_minutes, repeat, power=power,
                                  brightness_percent=brightness_percent, color=color,
                                  color_temperature_k=color_temperature_k)

    async def get_light_schedules(self):
        return await self._lights(self.schedule.list)

    async def cancel_light_schedule(self, schedule_id):
        return await self._lights(self.schedule.cancel, schedule_id)

    async def power_off(self, confirmed):
        if not confirmed:
            return {"status": "not_confirmed"}
        if not await self.power.permitted():
            return {"error": "power_off_not_permitted"}
        # The hub shuts down once this reply has finished playing.
        self.power.requested = True
        return {"status": "powering_off_after_reply"}

    async def set_volume(self, level=None, change=None):
        return await asyncio.to_thread(self.volume.set, level, change)

    async def execute(self, name, arguments):
        entry = TOOL_REGISTRY.get(name)
        if entry is None or (name in LIGHT_TOOLS and (self.lights is None or self.schedule is None))                 or (name in POWER_TOOLS and self.power is None) \
                or (name == "set_volume" and self.volume is None):
            return {"error": "unknown_tool"}
        schema = entry["definition"]["parameters"]
        if not isinstance(arguments, dict) or set(arguments) - schema["properties"].keys() \
                or not set(schema["required"]) <= arguments.keys():
            return {"error": "invalid_arguments"}
        try:
            result = await entry["handler"](self, **arguments)
            # Operation metadata only: no speech, memory contents, or tool arguments.
            log.info("Tool %s: saved=%s reference_saved=%s error=%s warnings=%s", name,
                     result.get("saved", result.get("observation_saved")), result.get("reference_saved"),
                     result.get("error", result.get("memory_error", result.get("sample", {}).get("error"))),
                     sorted(set(result.get("warnings", []) + result.get("calibration", {}).get("warnings", []))))
            return result
        except (ValueError, TypeError):
            return {"error": "invalid_arguments"}
        except (sqlite3.Error, OSError):
            return {"error": "storage_unavailable"}
        except Exception as exc:
            # One failing tool must not end the voice session; the call still gets an output.
            log.warning("Tool %s failed (%s)", name, type(exc).__name__)
            return {"error": "tool_failed"}


LIGHT_SETTINGS = {
    "power": {"type": "string", "enum": ["on", "off"]},
    "brightness_percent": {"type": "integer", "minimum": 1, "maximum": 100},
    "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "color_temperature_k": {"type": "integer", "minimum": KELVIN_RANGE[0], "maximum": KELVIN_RANGE[1]},
}
TOOL_REGISTRY = {entry["definition"]["name"]: entry for entry in [
    function_tool(Tools.get_current_time,
                  "Check the current date and time from the Raspberry Pi's system clock, including "
                  "local timezone/UTC offset and UTC time. Call for date/time questions and to resolve "
                  "today/yesterday when recording user-reported plant care. This does not schedule reminders."),
    function_tool(Tools.get_plant_status,
                  "Read and retain live light, raw soil capacitance, and air temperature/humidity, plus saved "
                  "plant profile, user preferences, recent memories, calibration references, and a numerical "
                  "soil comparison when available. Returns evidence, not a care decision. Soil counts are not "
                  "moisture percentages. Always available to request, even without plant details or calibration. "
                  "Use get_observation_history to compare conditions over time. Sensor notes describe limitations."),
    function_tool(Tools.get_observation_history,
                  "Retrieve recorded light, soil, temperature/humidity and soil captures, newest first, with "
                  "timestamps, physical-condition labels, uncertainty, and the plant profile at capture. "
                  "Use this evidence for trends and changes; gaps are unobserved time, not continuous monitoring. "
                  "Defaults to the current setup. To inspect older plants/pots use include_previous_setups. "
                  "Pass next_before_id as before_id for older pages. No scheduled checks are running.",
                  {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
                   "before_id": {"type": "integer", "minimum": 1},
                   "include_previous_setups": {"type": "boolean"}}),
    function_tool(Tools.set_plant_profile,
                  "Save user-provided details for FLORA's one current plant. Update only supplied fields. "
                  "Set probe_in_soil only when the user says where the probe actually is. Text corrections "
                  "preserve references and history. Set new_setup=true for a user-reported replacement plant, "
                  "repotting, or probe repositioning so old references are archived separately. "
                  "Fields not supplied are preserved; when replacing a plant, update its details too.",
                  {**{name: {"type": "string", "minLength": 1, "maxLength": 1000}
                      for name in ("name", "species", "location", "potting_mix", "notes")},
                   "probe_in_soil": {"type": "boolean"}, "new_setup": {"type": "boolean"}}),
    function_tool(Tools.calibrate_soil,
                  "Capture and retain fifteen LIVE soil readings with their spread and timestamp. No waiting "
                  "period, species, placement, or physical-condition confirmation is required to take a reading. "
                  "point=sample (default) records an unlabeled observation; water_now labels a candidate "
                  "ready-for-watering reference; watered labels a candidate watered/drained reference. "
                  "confirmed=true ONLY if the user reported that physical condition; otherwise false records "
                  "a tentative label. Saving is not proof the label or reference is reliable. Noisy/overlapping "
                  "readings are saved with caveats. Air counts are not dry-soil evidence. "
                  "reset starts a new setup, clearing active references while retaining history.",
                  {"point": {"type": "string", "enum": ["sample", "water_now", "watered", "reset"]},
                   "confirmed": {"type": "boolean"}}),
    function_tool(Tools.remember,
                  "Save a user-shared name, preference, or fact in FLORA's own long-term memory. "
                  "Reuse the same subject to correct/update a fact. Use set_plant_profile for plant details. "
                  "A name is a remembered fact, not voice identification or authentication.",
                  {"subject": {"type": "string", "minLength": 1, "maxLength": 120},
                   "content": {"type": "string", "minLength": 1, "maxLength": 2000},
                   "kind": {"type": "string", "enum": ["person", "fact", "preference"]}}, ("subject", "content")),
    function_tool(Tools.recall, "Retrieve saved plant details/calibration and memories. Use short keywords, "
                  "or an empty query for recent memories. Call before claiming to remember someone.",
                  {"query": {"type": "string", "maxLength": 200}}),
    function_tool(Tools.forget, "Forget a saved fact when the user asks. Recall first for its exact subject.",
                  {"subject": {"type": "string", "minLength": 1, "maxLength": 120}}, ("subject",)),
    function_tool(Tools.get_lights,
                  "Scan the local network for the user's lamps and report each lamp's name, whether it "
                  "answered, and its current power, brightness, color and color temperature. Use it to answer "
                  "questions about the lights and to learn lamp names before set_lights. These are room lamps, "
                  "not grow lights, and reachability is not proof a lamp is physically lit."),
    function_tool(Tools.set_lights,
                  "Change lamps on the local network. target is 'all', a lamp's name, or words shared by "
                  "several names (e.g. 'living room' matches every lamp with those words). Supply only what "
                  "the user asked to change. Brightness, color or color temperature also turn a lamp on. Use "
                  "color for named colors (convert to #RRGGBB) and color_temperature_k for white light: about "
                  "2700 warm, 4000 neutral, 6500 cool daylight. Each lamp reports a result: confirmed means "
                  "it reported the new state; sent_unconfirmed means it did not answer, so say the command "
                  "was sent but unconfirmed; state_differs means it reported something else. On "
                  "no_matching_light, use the available names instead of guessing.",
                  {"target": {"type": "string", "minLength": 1, "maxLength": 80},
                   **LIGHT_SETTINGS}, ("target",)),
    function_tool(Tools.schedule_lights,
                  "Schedule a lamp change for later, with the same target and settings as set_lights. Give "
                  "either at (local 24-hour HH:MM, the next time that clock time comes, within 24 hours) or "
                  "in_minutes, not both. repeat=daily (needs at) runs every day at that time. Only lights can "
                  "be scheduled; this is not a reminder or alarm. Check get_current_time when the user's time "
                  "is ambiguous. Timers run only while FLORA's program is running; confirm the scheduled "
                  "time from the result.",
                  {"target": {"type": "string", "minLength": 1, "maxLength": 80},
                   "at": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                   "in_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                   "repeat": {"type": "string", "enum": ["once", "daily"]},
                   **LIGHT_SETTINGS}, ("target",)),
    function_tool(Tools.get_light_schedules,
                  "List scheduled lamp changes with their schedule_id, target, settings, repeat, and next "
                  "local run time."),
    function_tool(Tools.cancel_light_schedule,
                  "Cancel one scheduled lamp change by schedule_id. Call get_light_schedules first to find it.",
                  {"schedule_id": {"type": "integer", "minimum": 1}}, ("schedule_id",)),
    function_tool(Tools.set_volume,
                  "Change how loudly FLORA speaks. level is 1 (quietest) to 10 (loudest allowed); change "
                  "steps one level (about 3 dB) up or down, for 'a bit louder/quieter'. Give one of them, "
                  "or neither to just report the level. The maximum is fixed; at_maximum means FLORA "
                  "cannot get louder, so say so rather than claiming a change.",
                  {"level": {"type": "integer", "minimum": 1, "maximum": 10},
                   "change": {"type": "string", "enum": ["up", "down"]}}),
    function_tool(Tools.power_off,
                  "Shut down FLORA's Raspberry Pi. Only when the user asks to power off, shut down or turn "
                  "FLORA off; going quiet or ending a conversation is not a request. First ask the user to "
                  "confirm and call with confirmed=true only after they say yes in their next reply. On "
                  "powering_off_after_reply, say a short goodbye: the Pi shuts down when you finish speaking "
                  "and needs its power unplugged and replugged to start again.",
                  {"confirmed": {"type": "boolean"}}, ("confirmed",)),
]}
LIGHT_TOOLS = {"get_lights", "set_lights", "schedule_lights", "get_light_schedules", "cancel_light_schedule"}
POWER_TOOLS = {"power_off"}


def tool_definitions(lights_enabled=False, power_off_enabled=False):
    """Advertise exactly the handlers available to the local dispatcher."""
    return [entry["definition"] for name, entry in TOOL_REGISTRY.items()
            if (lights_enabled or name not in LIGHT_TOOLS)
            and (power_off_enabled or name not in POWER_TOOLS)]
