"""FLORA's local SQLite memory. No LumaKit imports or shared database."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import ROOT
from .sensors import timestamp

DB_PATH = ROOT / "data" / "flora.sqlite3"
PROFILE_FIELDS = {"name", "species", "location", "potting_mix", "notes", "probe_in_soil"}


class Memory:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.path, timeout=2, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS plant (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT, species TEXT, location TEXT, potting_mix TEXT, notes TEXT,
                    probe_in_soil INTEGER CHECK (probe_in_soil IN (0, 1)), updated_at TEXT
                );
                INSERT OR IGNORE INTO plant (id) VALUES (1);
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    subject TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    content TEXT NOT NULL, kind TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS soil_calibration (
                    point TEXT PRIMARY KEY CHECK (point IN ('water_now', 'watered')),
                    raw INTEGER NOT NULL, spread INTEGER NOT NULL,
                    measured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation_setup (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    setup_id INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO observation_setup VALUES (1, 1);
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY,
                    setup_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    plant_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS observations_setup_id ON observations(setup_id, id);
                CREATE TABLE IF NOT EXISTS soil_reference_context (
                    point TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY);
            """)
            self._migrate_references(conn)
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _snapshot(conn):
        plant = dict(conn.execute("SELECT * FROM plant WHERE id = 1").fetchone())
        plant.pop("id")
        if plant["probe_in_soil"] is not None:
            plant["probe_in_soil"] = bool(plant["probe_in_soil"])
        calibration = {row["point"]: dict(row) for row in conn.execute("SELECT * FROM soil_calibration")}
        for row in conn.execute("SELECT * FROM soil_reference_context"):
            if row["point"] in calibration:
                calibration[row["point"]].update(json.loads(row["metadata_json"]))
        setup_id = conn.execute("SELECT setup_id FROM observation_setup WHERE id = 1").fetchone()[0]
        return {"plant": plant, "soil_calibration": calibration, "setup_id": setup_id}

    def _migrate_references(self, conn):
        """Archive existing anchors once, preserving their original measurement dates."""
        if conn.execute("SELECT 1 FROM schema_migrations WHERE name = 'observation_history'").fetchone():
            return
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE name = 'observation_history'").fetchone():
            context = self._snapshot(conn)
            for point, sample in context["soil_calibration"].items():
                payload = {"sample": sample, "point": point, "confirmed": True,
                           "probe_in_soil": True, "warnings": [], "source": "legacy_calibration"}
                observation_id = self._insert_observation(conn, "soil_capture", payload, context)
                metadata = {"observation_id": observation_id, "confirmed": True,
                            "probe_in_soil": True, "warnings": [], "source": "legacy_calibration"}
                conn.execute("INSERT INTO soil_reference_context VALUES (?, ?)",
                             (point, json.dumps(metadata)))
            conn.execute("INSERT INTO schema_migrations VALUES ('observation_history')")
        conn.commit()

    def snapshot(self):
        with self._connect() as conn:
            conn.execute("BEGIN")
            return self._snapshot(conn)

    def set_plant_profile(self, new_setup=False, **fields):
        if type(new_setup) is not bool or (not fields and not new_setup) or set(fields) - PROFILE_FIELDS:
            raise ValueError("Supply plant profile fields only")
        for name, value in fields.items():
            if name == "probe_in_soil":
                if type(value) is not bool:
                    raise ValueError("probe_in_soil must be a boolean")
            elif not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
                raise ValueError("Plant details must be nonempty text up to 1000 characters")
            else:
                fields[name] = value.strip()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if new_setup:
                self._new_setup(conn)
            if fields:
                assignments = ", ".join(f"{name} = ?" for name in fields)
                conn.execute(f"UPDATE plant SET {assignments}, updated_at = ? WHERE id = 1",
                             (*fields.values(), timestamp()))
            return {"saved": True, "calibration_reset": new_setup, **self._snapshot(conn)}

    def remember(self, subject, content, kind="fact"):
        if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 120:
            raise ValueError("subject must be 1-120 characters")
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
            raise ValueError("content must be 1-2000 characters")
        if kind not in {"person", "fact", "preference"}:
            raise ValueError("Unsupported memory kind")
        with self._connect() as conn:
            now = timestamp()
            conn.execute("""
                INSERT INTO memories (subject, content, kind, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subject) DO UPDATE SET
                    content=excluded.content, kind=excluded.kind, updated_at=excluded.updated_at
            """, (subject.strip(), content.strip(), kind, now, now))
            return {"saved": True, "subject": subject.strip()}

    def recall(self, query=""):
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be text up to 200 characters")
        # Literal keyword search; SQL wildcard characters are not instructions.
        terms = query.split()[:8]
        params, clauses = [], []
        for term in terms:
            term = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(subject LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')")
            params.extend([f"%{term}%", f"%{term}%"])
        where = " WHERE " + " OR ".join(clauses) if clauses else ""
        with self._connect() as conn:
            conn.execute("BEGIN")
            memories = [dict(row) for row in conn.execute(
                "SELECT * FROM memories" + where + " ORDER BY updated_at DESC, id DESC LIMIT 20", params)]
            # Preferences remain available even when recent facts fill the
            # recall window or a keyword query matches a different subject.
            preferences = [dict(row) for row in conn.execute(
                "SELECT * FROM memories WHERE kind = 'preference' "
                "ORDER BY updated_at DESC, id DESC LIMIT 20")]
            return {**self._snapshot(conn), "memories": memories, "preferences": preferences}

    def forget(self, subject):
        if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 120:
            raise ValueError("Supply the exact memory subject from recall")
        with self._connect() as conn:
            deleted = conn.execute("DELETE FROM memories WHERE subject = ?", (subject.strip(),)).rowcount
            return {"deleted": bool(deleted), "subject": subject.strip()}

    @staticmethod
    def _insert_observation(conn, kind, payload, context):
        cursor = conn.execute("""
            INSERT INTO observations (setup_id, kind, recorded_at, plant_json, payload_json)
            VALUES (?, ?, ?, ?, ?)
        """, (context["setup_id"], kind, timestamp(), json.dumps(context["plant"], allow_nan=False),
              json.dumps(payload, allow_nan=False)))
        return cursor.lastrowid

    def record_observation(self, kind, payload, context):
        """Keep measurements even when noisy or captured during a setup change."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._snapshot(conn)
            changed = (current["setup_id"] != context["setup_id"] or current["plant"] != context["plant"])
            payload = {**payload, "context_changed_during_capture": changed}
            observation_id = self._insert_observation(conn, kind, payload, context)
            point = payload.get("point")
            sample = payload.get("sample", {})
            reference_saved = (kind == "soil_capture" and point in {"water_now", "watered"}
                               and "error" not in sample and not changed)
            if reference_saved:
                metadata = {key: payload[key] for key in ("confirmed", "probe_in_soil", "warnings")}
                metadata["observation_id"] = observation_id
                conn.execute("""
                    INSERT INTO soil_calibration VALUES (?, ?, ?, ?)
                    ON CONFLICT(point) DO UPDATE SET raw=excluded.raw,
                        spread=excluded.spread, measured_at=excluded.measured_at
                """, (point, sample["raw"], sample["spread"], sample["measured_at"]))
                conn.execute("""
                    INSERT INTO soil_reference_context VALUES (?, ?)
                    ON CONFLICT(point) DO UPDATE SET metadata_json=excluded.metadata_json
                """, (point, json.dumps(metadata, allow_nan=False)))
            return {"saved": True, "observation_id": observation_id, "reference_saved": reference_saved,
                    "context_changed_during_capture": changed, **self._snapshot(conn)}

    def observation_history(self, limit=20, before_id=None, include_previous_setups=False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100")
        if before_id is not None and (type(before_id) is not int or before_id < 1):
            raise ValueError("before_id must be a positive observation ID")
        if type(include_previous_setups) is not bool:
            raise ValueError("include_previous_setups must be a boolean")
        with self._connect() as conn:
            conn.execute("BEGIN")
            setup_id = self._snapshot(conn)["setup_id"]
            clauses, params = [], []
            if not include_previous_setups:
                clauses.append("setup_id = ?")
                params.append(setup_id)
            if before_id is not None:
                clauses.append("id < ?")
                params.append(before_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = list(conn.execute("SELECT * FROM observations" + where + " ORDER BY id DESC LIMIT ?",
                                     (*params, limit + 1)))
            observations = [{"id": row["id"], "setup_id": row["setup_id"], "kind": row["kind"],
                             "recorded_at": row["recorded_at"], "plant": json.loads(row["plant_json"]),
                             "data": json.loads(row["payload_json"])} for row in rows[:limit]]
            return {"setup_id": setup_id, "observations": observations,
                    "next_before_id": observations[-1]["id"] if len(rows) > limit else None}

    def save_calibration(self, point, raw, spread, measured_at, expected_profile=None):
        """Compatibility entry point; reference quality no longer vetoes storage."""
        if point not in {"water_now", "watered"} or type(raw) is not int or not 0 <= raw <= 4095:
            raise ValueError("Invalid calibration point")
        if type(spread) is not int or not 0 <= spread <= 4095:
            raise ValueError("Invalid calibration spread")
        context = self.snapshot()
        if expected_profile is not None:
            context["plant"] = expected_profile
        return self.record_observation("soil_capture", {
            "point": point, "confirmed": True, "probe_in_soil": True, "warnings": [],
            "sample": {"raw": raw, "spread": spread, "measured_at": measured_at},
        }, context)

    @staticmethod
    def _new_setup(conn):
        # Old evidence stays in history; future comparisons use the new setup only.
        conn.execute("UPDATE observation_setup SET setup_id = setup_id + 1 WHERE id = 1")
        conn.execute("DELETE FROM soil_calibration")
        conn.execute("DELETE FROM soil_reference_context")

    def reset_calibration(self):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._new_setup(conn)
            return {"reset": True, "history_preserved": True, **self._snapshot(conn)}
