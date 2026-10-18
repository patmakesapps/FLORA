"""Hourly plant readings, saved quietly whether or not anyone is talking.

The service takes one sensor reading at the top of every hour and stores a
compact copy in the observations table (kind "hourly_reading"). The
get_plant_trends tool turns those into per-day summaries the model can use
with the plant's species to answer care questions.
"""
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger(__name__)
KIND = "hourly_reading"
RETRY_SECONDS = 60  # Retry soon when a conversation's read holds the bus.
LIGHT_LEVELS = (100, 1000, 10000)  # lux: dim indoor, bright indoor, near-sun


def compact(status):
    """The numbers worth keeping from a full get_plant_status reading."""
    light, soil, env = status["light"], status["soil"], status["environment"]
    return {
        "lux": light["lux"] if light["status"] == "ok" else None,
        "temperature_c": env["temperature_c"] if env["status"] == "ok" else None,
        "humidity_percent": env["relative_humidity_percent"] if env["status"] == "ok" else None,
        "soil_raw": soil["raw_capacitance"] if soil["status"] == "ok" else None,
        "probe_in_soil": soil.get("probe_in_soil"),
    }


def seconds_to_next_hour(now):
    following = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (following - now).total_seconds()


class PlantLog:
    def __init__(self, sensors, memory, now=lambda: datetime.now().astimezone()):
        self.sensors = sensors
        self.memory = memory
        self.now = now

    async def record(self):
        """Save one reading. Returns False to retry soon, True when done for this hour."""
        status = await self.sensors.get_plant_status()
        if any(status[name].get("error") == "read_in_progress" for name in ("light", "soil", "environment")):
            return False
        reading = compact(status)
        if all(reading[key] is None for key in ("lux", "temperature_c", "soil_raw")):
            return True  # No sensors connected; nothing to track.
        try:
            saved = await asyncio.to_thread(self.memory.recall)
            if saved["plant"]["probe_in_soil"] is not None:
                reading["probe_in_soil"] = saved["plant"]["probe_in_soil"]
            await asyncio.to_thread(self.memory.record_observation, KIND, reading, saved)
        except (sqlite3.Error, OSError) as exc:
            log.warning("Hourly plant reading not saved (%s)", type(exc).__name__)
        return True

    async def run(self):
        while True:
            await asyncio.sleep(seconds_to_next_hour(self.now()))
            for _ in range(10):
                if await self.record():
                    break
                await asyncio.sleep(RETRY_SECONDS)


def _stats(values, digits=1):
    if not values:
        return None
    return {"min": round(min(values), digits), "avg": round(sum(values) / len(values), digits),
            "max": round(max(values), digits)}


def summarize(rows):
    """Per local day: readings, light exposure, temperature, humidity, soil counts."""
    days = {}
    for row in rows:
        day = datetime.fromisoformat(row["recorded_at"]).astimezone().date().isoformat()
        days.setdefault(day, []).append(row["data"])
    summary = []
    for day, readings in sorted(days.items()):
        lux = [r["lux"] for r in readings if r.get("lux") is not None]
        temps = [r["temperature_c"] for r in readings if r.get("temperature_c") is not None]
        humidity = [r["humidity_percent"] for r in readings if r.get("humidity_percent") is not None]
        soil = [r["soil_raw"] for r in readings if r.get("soil_raw") is not None and r.get("probe_in_soil")]
        entry = {"date": day, "readings": len(readings)}
        if lux:
            entry["light"] = {
                "max_lux": round(max(lux)),
                "hourly_readings_at_or_above_lux": {str(level): sum(v >= level for v in lux)
                                                    for level in LIGHT_LEVELS},
            }
        if temps:
            entry["temperature_c"] = _stats(temps)
            entry["temperature_f"] = _stats([t * 9 / 5 + 32 for t in temps])
        if humidity:
            entry["humidity_percent"] = _stats(humidity, 0)
        if soil:
            entry["soil_raw"] = {"first": soil[0], "last": soil[-1], "min": min(soil), "max": max(soil)}
        summary.append(entry)
    return summary
