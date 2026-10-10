"""Conversation memory, pot-specific calibration, and live measurement evidence."""
import asyncio
import math
import sqlite3
from datetime import datetime, timezone

from .memory import Memory


def function_tool(handler, description, properties=None, required=()):
    return {"handler": handler, "definition": {
        "type": "function", "name": handler.__name__, "description": description,
        "parameters": {
            "type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False,
        },
    }}


def calibration_summary(points):
    if not all(name in points for name in ("water_now", "watered")):
        return {"status": "incomplete", "points": points}
    dry, wet = points["water_now"], points["watered"]
    span = wet["raw"] - dry["raw"]
    noise = max(dry["spread"], wet["spread"])
    # A sanity check for distinguishable readings, not a species care threshold.
    if span <= max(5, 3 * noise):
        return {"status": "invalid", "points": points,
                "reason_code": "indistinct_calibration_points",
                "span_counts": span, "noise_counts": noise}
    return {"status": "ready", "points": points,
            "uncertainty_counts": max(1, 2 * noise, math.ceil(span * 0.05))}


def soil_comparison(soil, calibration):
    """Compare measured counts with saved references; the model decides care."""
    if not soil["probe_in_soil"]:
        return {"status": "unavailable", "reason_code": "probe_outside_soil"}
    if soil["status"] != "ok":
        return {"status": "unavailable", "reason_code": "soil_reading_unavailable"}
    if calibration["status"] != "ready":
        return {"status": "unavailable", "reason_code": "calibration_" + calibration["status"]}
    raw = soil["raw_capacitance"]
    threshold = calibration["points"]["water_now"]["raw"]
    margin = calibration["uncertainty_counts"]
    if raw < threshold - margin:
        relative = "below"
    elif raw <= threshold + margin:
        relative = "within_uncertainty"
    else:
        relative = "above"
    return {"status": "available", "reference_point": "water_now",
            "reference_raw": threshold, "difference_counts": raw - threshold,
            "uncertainty_counts": margin, "relative_to_reference": relative,
            "basis": "user_confirmed_calibration"}


class Tools:
    def __init__(self, sensors, memory=None):
        self.sensors = sensors
        self.memory = memory if memory is not None else Memory()

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
        result = await self.sensors.get_plant_status()
        try:
            saved = await asyncio.to_thread(self.memory.recall)
        except (sqlite3.Error, OSError):
            result["memory_error"] = "storage_unavailable"
            result["soil_comparison"] = {"status": "unavailable", "reason_code": "storage_unavailable"}
            return result
        plant = saved["plant"]
        if plant["probe_in_soil"] is not None:
            result["soil"]["probe_in_soil"] = plant["probe_in_soil"]
        # The sensor layer reports raw counts; this layer knows the pot's saved calibration.
        calibration = calibration_summary(saved["soil_calibration"])
        result["soil"]["calibrated"] = calibration["status"] == "ready"
        if result["soil"]["probe_in_soil"]:
            result["soil"]["note"] = "Raw capacitive counts for the installed probe, not water-content percentage."
        else:
            result["soil"]["note"] = "Probe is outside soil; counts cannot describe a plant's moisture."
        result.update(plant=plant, calibration=calibration,
                      memories=saved["memories"], preferences=saved["preferences"],
                      soil_comparison=soil_comparison(result["soil"], calibration))
        return result

    async def calibrate_soil(self, point, confirmed):
        if confirmed is not True:
            return {"saved": False, "error": "physical_condition_not_confirmed"}
        if point == "reset":
            return await asyncio.to_thread(self.memory.reset_calibration)
        if point not in {"water_now", "watered"}:
            raise ValueError("Unknown calibration point")
        before = await asyncio.to_thread(self.memory.snapshot)
        plant = before["plant"]
        installed = plant["probe_in_soil"]
        if installed is None:
            installed = self.sensors.soil_probe_in_soil
        if not installed:
            return {"saved": False, "error": "probe_outside_soil"}
        if not plant["species"]:
            return {"saved": False, "error": "plant_type_required"}
        sample = await self.sensors.sample_soil()
        if "error" in sample:
            return {"saved": False, **sample}
        # Check repeatability of separate median windows. Keep the full raw
        # spread in saved anchors for separation checks and comparison uncertainty.
        if sample.get("median_spread", sample["spread"]) > max(10, math.ceil(sample["raw"] * 0.05)):
            return {"saved": False, "error": "unstable_soil_readings",
                    "sample": sample, "stability_limit_counts": max(10, math.ceil(sample["raw"] * 0.05))}
        after = await asyncio.to_thread(self.memory.snapshot)
        if after["plant"] != plant:
            return {"saved": False, "error": "plant_changed_during_capture"}
        proposed = {**after["soil_calibration"], point: {"point": point, "raw": sample["raw"],
                    "spread": sample["spread"], "measured_at": sample["measured_at"]}}
        calibration = calibration_summary(proposed)
        if calibration["status"] == "invalid":
            return {"saved": False, "error": "indistinct_calibration_points", "calibration": calibration}
        saved = await asyncio.to_thread(self.memory.save_calibration, point, sample["raw"],
                                       sample["spread"], sample["measured_at"], plant)
        if "error" in saved:
            return {"saved": False, **saved}
        return {"saved": True, "point": point, "sample": sample,
                "calibration": calibration_summary(saved["soil_calibration"])}

    async def set_plant_profile(self, **fields):
        return await asyncio.to_thread(self.memory.set_plant_profile, **fields)

    async def remember(self, subject, content, kind="fact"):
        return await asyncio.to_thread(self.memory.remember, subject, content, kind)

    async def recall(self, query=""):
        return await asyncio.to_thread(self.memory.recall, query)

    async def forget(self, subject):
        return await asyncio.to_thread(self.memory.forget, subject)

    async def execute(self, name, arguments):
        entry = TOOL_REGISTRY.get(name)
        if entry is None:
            return {"error": "unknown_tool"}
        schema = entry["definition"]["parameters"]
        if not isinstance(arguments, dict) or set(arguments) - schema["properties"].keys() \
                or not set(schema["required"]) <= arguments.keys():
            return {"error": "invalid_arguments"}
        try:
            return await entry["handler"](self, **arguments)
        except (ValueError, TypeError):
            return {"error": "invalid_arguments"}
        except (sqlite3.Error, OSError):
            return {"error": "storage_unavailable"}


TOOL_REGISTRY = {entry["definition"]["name"]: entry for entry in [
    function_tool(Tools.get_current_time,
                  "Check the current date and time from the Raspberry Pi's system clock, including "
                  "local timezone/UTC offset and UTC time. Call for date/time questions and to resolve "
                  "today/yesterday when recording user-reported plant care. This does not schedule reminders."),
    function_tool(Tools.get_plant_status,
                  "Read live light, raw soil capacitance, and air temperature/humidity, plus saved "
                  "plant profile, user preferences, recent memories, calibration references, and a numerical "
                  "soil comparison when available. Returns evidence, not a care decision. Soil counts are not "
                  "moisture percentages. Sensor notes describe measurement limitations."),
    function_tool(Tools.set_plant_profile,
                  "Save user-provided details for FLORA's one current plant. Update only supplied fields. "
                  "Set probe_in_soil only when the user says where the probe actually is. Changing species, "
                  "mix, or probe placement clears old calibration. For repotting/moving the probe, reset calibration.",
                  {**{name: {"type": "string", "minLength": 1, "maxLength": 1000}
                      for name in ("name", "species", "location", "potting_mix", "notes")},
                   "probe_in_soil": {"type": "boolean"}}),
    function_tool(Tools.calibrate_soil,
                  "Capture fifteen LIVE soil readings in three median windows for this pot's watering benchmark. water_now means "
                  "the user has checked the actual soil and says this plant is ready for watering; watered "
                  "means it was watered, drained, and allowed to settle. Require an installed probe, saved "
                  "species, and explicit user confirmation of the chosen physical condition. Never invent "
                  "counts, use readings in air, or mark today's air reading as a dry point. reset clears anchors.",
                  {"point": {"type": "string", "enum": ["water_now", "watered", "reset"]},
                   "confirmed": {"type": "boolean"}}, ("point", "confirmed")),
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
]}


def tool_definitions():
    """Advertise exactly the handlers available to the local dispatcher."""
    return [entry["definition"] for entry in TOOL_REGISTRY.values()]
