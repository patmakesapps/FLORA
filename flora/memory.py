"""FLORA's local SQLite memory. No LumaKit imports or shared database."""
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
            """)
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
        return {"plant": plant, "soil_calibration": calibration}

    def snapshot(self):
        with self._connect() as conn:
            conn.execute("BEGIN")
            return self._snapshot(conn)

    def set_plant_profile(self, **fields):
        if not fields or set(fields) - PROFILE_FIELDS:
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
            before = self._snapshot(conn)["plant"]
            # Identity, pot/mix, or placement changes make old anchors unsafe.
            changed = any(before[name] != value for name, value in fields.items()
                          if name in {"species", "potting_mix", "probe_in_soil"})
            if changed:
                conn.execute("DELETE FROM soil_calibration")
            assignments = ", ".join(f"{name} = ?" for name in fields)
            conn.execute(f"UPDATE plant SET {assignments}, updated_at = ? WHERE id = 1",
                         (*fields.values(), timestamp()))
            return {"saved": True, "calibration_reset": changed, **self._snapshot(conn)}

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
            return {**self._snapshot(conn), "memories": memories}

    def forget(self, subject):
        if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 120:
            raise ValueError("Supply the exact memory subject from recall")
        with self._connect() as conn:
            deleted = conn.execute("DELETE FROM memories WHERE subject = ?", (subject.strip(),)).rowcount
            return {"deleted": bool(deleted), "subject": subject.strip()}

    def save_calibration(self, point, raw, spread, measured_at, expected_profile=None):
        if point not in {"water_now", "watered"} or type(raw) is not int or not 0 <= raw <= 4095:
            raise ValueError("Invalid calibration point")
        if type(spread) is not int or not 0 <= spread <= 4095:
            raise ValueError("Invalid calibration spread")
        with self._connect() as conn:
            # Keep placement/profile checks and the final save in one write
            # transaction, including when a CLI command runs beside voice.
            conn.execute("BEGIN IMMEDIATE")
            if expected_profile is not None and self._snapshot(conn)["plant"] != expected_profile:
                return {"error": "plant_changed_during_capture"}
            other = conn.execute("SELECT * FROM soil_calibration WHERE point != ?", (point,)).fetchone()
            if other is not None:
                span = raw - other["raw"] if point == "watered" else other["raw"] - raw
                if span <= max(5, 3 * max(spread, other["spread"])):
                    return {"error": "indistinct_calibration_points"}
            conn.execute("""
                INSERT INTO soil_calibration (point, raw, spread, measured_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(point) DO UPDATE SET raw=excluded.raw,
                    spread=excluded.spread, measured_at=excluded.measured_at
            """, (point, raw, spread, measured_at))
            return self._snapshot(conn)

    def reset_calibration(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM soil_calibration")
            return {"reset": True}
