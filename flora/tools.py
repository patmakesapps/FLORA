"""Conversation memory, pot-specific calibration, and grounded watering advice."""
import asyncio
import math
import sqlite3

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
                "reason": "Watered counts must be higher and clearly separated from watering-point counts/noise."}
    return {"status": "ready", "points": points,
            "uncertainty_counts": max(1, 2 * noise, math.ceil(span * 0.05))}


def watering_advice(soil, plant, calibration):
    def answer(recommendation, reason):
        return {"recommendation": recommendation, "reason": reason,
                "basis": "User-confirmed watering benchmark for this pot/mix/probe position; not water-content percent."}
    if not soil["probe_in_soil"]:
        return answer("unknown", "The probe is outside soil.")
    if soil["status"] != "ok":
        return answer("unknown", "No successful live soil measurement.")
    if not plant.get("species"):
        return answer("unknown", "Plant species/type has not been saved yet.")
    if calibration["status"] != "ready":
        return answer("unknown", "Both distinct, stable calibration points are needed for this plant.")
    raw = soil["raw_capacitance"]
    threshold = calibration["points"]["water_now"]["raw"]
    margin = calibration["uncertainty_counts"]
    if raw < threshold - margin:
        return answer("water", "Live counts are below this plant's learned watering point; watering is due.")
    if raw <= threshold + margin:
        return answer("check_soil", "Near the learned watering point; check the soil before deciding to water.")
    return answer("wait", "Live counts are above the learned watering point; no need to water yet by this benchmark.")


class Tools:
    def __init__(self, sensors, memory=None):
        self.sensors = sensors
        self.memory = memory if memory is not None else Memory()

    async def get_plant_status(self):
        result = await self.sensors.get_plant_status()
        try:
            saved = await asyncio.to_thread(self.memory.snapshot)
        except (sqlite3.Error, OSError):
            result["memory_error"] = "storage_unavailable"
            result["watering"] = {"recommendation": "unknown", "reason": "Saved calibration is unavailable."}
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
                      watering=watering_advice(result["soil"], plant, calibration))
        return result

    async def calibrate_soil(self, point, confirmed):
        if confirmed is not True:
            return {"saved": False, "error": "physical_condition_not_confirmed",
                    "note": "Ask the user to confirm the selected real soil condition before capturing it."}
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
        if sample["spread"] > max(10, math.ceil(sample["raw"] * 0.05)):
            return {"saved": False, "error": "unstable_soil_readings",
                    "note": "Let the soil/probe settle and retry; the five readings vary too much."}
        after = await asyncio.to_thread(self.memory.snapshot)
        if after["plant"] != plant:
            return {"saved": False, "error": "plant_changed_during_capture"}
        proposed = {**after["soil_calibration"], point: {"point": point, "raw": sample["raw"],
                    "spread": sample["spread"], "measured_at": sample["measured_at"]}}
        calibration = calibration_summary(proposed)
        if calibration["status"] == "invalid":
            return {"saved": False, "error": "indistinct_calibration_points", "note": calibration["reason"]}
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
            return {"error": "storage_unavailable", "note": "Nothing was confirmed saved; try again."}


TOOL_REGISTRY = {entry["definition"]["name"]: entry for entry in [
    function_tool(Tools.get_plant_status,
                  "Read live light, raw soil capacitance, and air temperature/humidity, plus saved "
                  "plant identity and a watering recommendation from this pot's calibration. "
                  "Call before answering current condition/watering questions. Soil is never a percentage."),
    function_tool(Tools.set_plant_profile,
                  "Save user-provided details for FLORA's one current plant. Update only supplied fields. "
                  "Set probe_in_soil only when the user says where the probe actually is. Changing species, "
                  "mix, or probe placement clears old calibration. For repotting/moving the probe, reset calibration.",
                  {**{name: {"type": "string", "minLength": 1, "maxLength": 1000}
                      for name in ("name", "species", "location", "potting_mix", "notes")},
                   "probe_in_soil": {"type": "boolean"}}),
    function_tool(Tools.calibrate_soil,
                  "Capture five LIVE soil readings for this pot's watering benchmark. water_now means "
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
