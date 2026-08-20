"""Core timing logic: accumulate per-process foreground seconds and persist to SQLite."""

import csv
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from config import DB_FILE

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS time_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    process_name TEXT   NOT NULL,
    display_name TEXT   NOT NULL,
    project     TEXT    NOT NULL DEFAULT '',
    tag         TEXT    NOT NULL DEFAULT 'Other',
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(date, process_name, project)
);
"""

_CREATE_TAGS_SQL = """
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    color       TEXT    NOT NULL,
    is_system   INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_SEGMENTS_SQL = """
CREATE TABLE IF NOT EXISTS time_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    start_time  TEXT    NOT NULL,
    end_time    TEXT    NOT NULL,
    process_name TEXT   NOT NULL,
    display_name TEXT   NOT NULL,
    project     TEXT    NOT NULL DEFAULT '',
    tag         TEXT    NOT NULL DEFAULT 'Other',
    seconds     REAL    NOT NULL DEFAULT 0
);
"""

_CREATE_FOCUS_SQL = """
CREATE TABLE IF NOT EXISTS focus_time_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    tag         TEXT    NOT NULL,
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(date, tag)
);
"""

_CREATE_TAG_TOTALS_SQL = """
CREATE TABLE IF NOT EXISTS tag_time_records (
    date        TEXT    NOT NULL,
    tag         TEXT    NOT NULL,
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (date, tag)
);
"""

_MIGRATE_ADD_PROJECT_SQL = """
CREATE TABLE IF NOT EXISTS time_records_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    process_name TEXT   NOT NULL,
    display_name TEXT   NOT NULL,
    project     TEXT    NOT NULL DEFAULT '',
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(date, process_name, project)
);
INSERT INTO time_records_new (date, process_name, display_name, project, seconds, updated_at)
SELECT date, process_name, display_name, '', seconds, updated_at FROM time_records;
DROP TABLE time_records;
ALTER TABLE time_records_new RENAME TO time_records;
"""

_CREATE_CODEX_SQL = """
CREATE TABLE IF NOT EXISTS codex_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT    NOT NULL,
    session_id  TEXT    NOT NULL,
    project     TEXT    NOT NULL,
    observed_at TEXT    NOT NULL,
    received_at TEXT    NOT NULL,
    UNIQUE(session_id, event, observed_at)
);

CREATE TABLE IF NOT EXISTS codex_time_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    project     TEXT    NOT NULL,
    project_name TEXT   NOT NULL,
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(date, project)
);

CREATE TABLE IF NOT EXISTS chrome_url_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT    NOT NULL,
    domain      TEXT    NOT NULL DEFAULT '',
    received_at TEXT    NOT NULL
);
"""


class TimeRecorder:
    """Thread-safe-ish SQLite recorder.

    All public methods open a short-lived connection so that calls from the
    tracker thread and the UI thread do not share a single connection object.
    """

    def __init__(self):
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_SQL)
            conn.executescript(_CREATE_CODEX_SQL)
            conn.execute(_CREATE_TAGS_SQL)
            conn.execute(_CREATE_SEGMENTS_SQL)
            conn.execute(_CREATE_FOCUS_SQL)
            conn.execute(_CREATE_TAG_TOTALS_SQL)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_time_segments_date_tag "
                "ON time_segments (date, tag)"
            )
            self._migrate_add_project(conn)
            self._migrate_add_tag(conn)
            self._migrate_add_segment_project(conn)
            # History range app breakdowns read these fields together. Keep a
            # covering index so the aggregation does not fetch every segment
            # row from the table for each History request.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_time_segments_history_app "
                "ON time_segments (date, tag, display_name, process_name, project, seconds)"
            )
            self._migrate_current_tag_totals(conn)
            self._seed_default_tags(conn)
            self._migrate_tag_from_display_name(conn)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate_current_tag_totals(conn):
        """Backfill today's de-duplicated tag totals from legacy segments once."""
        today = date.today().isoformat()
        existing = conn.execute(
            "SELECT 1 FROM tag_time_records WHERE date = ? LIMIT 1", (today,)
        ).fetchone()
        if existing:
            return
        rows = conn.execute(
            """
            SELECT start_time, end_time, tag
            FROM time_segments
            WHERE date = ? AND tag != 'Idle'
            ORDER BY tag, start_time, end_time
            """,
            (today,),
        ).fetchall()
        by_tag = {}
        for row in rows:
            by_tag.setdefault(row["tag"], []).append(
                (datetime.fromisoformat(row["start_time"]), datetime.fromisoformat(row["end_time"]))
            )
        now = datetime.now().isoformat(timespec="seconds")
        for tag, intervals in by_tag.items():
            merged = []
            for start, end in intervals:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            seconds = sum((end - start).total_seconds() for start, end in merged)
            conn.execute(
                "INSERT OR REPLACE INTO tag_time_records (date, tag, seconds, updated_at) VALUES (?, ?, ?, ?)",
                (today, tag, seconds, now),
            )

    @staticmethod
    def _migrate_add_project(conn):
        """Add project column to existing time_records table if missing."""
        cols = conn.execute("PRAGMA table_info(time_records)").fetchall()
        col_names = [c["name"] for c in cols]
        if "project" not in col_names:
            conn.executescript(_MIGRATE_ADD_PROJECT_SQL)

    @staticmethod
    def _migrate_add_tag(conn):
        """Add tag column to time_records if missing."""
        cols = conn.execute("PRAGMA table_info(time_records)").fetchall()
        col_names = [c["name"] for c in cols]
        if "tag" not in col_names:
            conn.execute("ALTER TABLE time_records ADD COLUMN tag TEXT NOT NULL DEFAULT 'Other'")

    @staticmethod
    def _migrate_add_segment_project(conn):
        """Add project identity to the canonical segment table."""
        cols = conn.execute("PRAGMA table_info(time_segments)").fetchall()
        col_names = [c["name"] for c in cols]
        if "project" not in col_names:
            conn.execute("ALTER TABLE time_segments ADD COLUMN project TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _seed_default_tags(conn):
        """Insert built-in tags if not present."""
        defaults = [
            ("Work", "#7aa2f7", 1),
            ("Indie", "#9ece6a", 1),
            ("Other", "#565f89", 1),
        ]
        for name, color, is_system in defaults:
            conn.execute(
                "INSERT OR IGNORE INTO tags (name, color, is_system) VALUES (?, ?, ?)",
                (name, color, is_system),
            )

    @staticmethod
    def _migrate_tag_from_display_name(conn):
        """Parse (Indie)/(Work) suffix from display_name into tag column, then strip suffix."""
        # Only migrate rows where tag is still 'Other' but display_name has a suffix
        conn.execute(
            "UPDATE time_records SET tag = 'Indie' WHERE display_name LIKE '%(Indie)' AND tag = 'Other'"
        )
        conn.execute(
            "UPDATE time_records SET tag = 'Work' WHERE display_name LIKE '%(Work)' AND tag = 'Other'"
        )
        conn.execute(
            "UPDATE time_records SET tag = 'Other' WHERE display_name LIKE '%(Other)' AND tag = 'Other'"
        )
        # Strip suffixes from display_name
        conn.execute(
            "UPDATE time_records SET display_name = REPLACE(display_name, ' (Indie)', '') WHERE display_name LIKE '%(Indie)'"
        )
        conn.execute(
            "UPDATE time_records SET display_name = REPLACE(display_name, ' (Work)', '') WHERE display_name LIKE '%(Work)'"
        )
        conn.execute(
            "UPDATE time_records SET display_name = REPLACE(display_name, ' (Other)', '') WHERE display_name LIKE '%(Other)'"
        )
        # Strip 'Other (xxx.exe)' prefix format -> just 'xxx.exe'
        rows = conn.execute(
            "SELECT DISTINCT display_name FROM time_records WHERE display_name LIKE 'Other (%'"
        ).fetchall()
        for row in rows:
            old_name = row["display_name"]
            if old_name.startswith("Other (") and old_name.endswith(")"):
                new_name = old_name[7:-1]  # Remove 'Other (' prefix and ')' suffix
                conn.execute(
                    "UPDATE time_records SET display_name = ? WHERE display_name = ?",
                    (new_name, old_name),
                )

    @staticmethod
    def _conn() -> sqlite3.Connection:
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @classmethod
    def update_tags_from_process_tags(cls, process_tags: dict):
        """Update time_records.tag for processes whose tag has changed in config."""
        conn = cls._conn()
        try:
            for proc, tag in process_tags.items():
                conn.execute(
                    "UPDATE time_records SET tag = ? WHERE process_name = ? AND tag != ?",
                    (tag, proc, tag),
                )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def update_process_tag_selective(cls, process_name: str, old_tag: str, new_tag: str):
        """Update time_records.tag only for records with the old tag, preserving
        records that were tagged differently via keyword rules."""
        conn = cls._conn()
        try:
            conn.execute(
                "UPDATE time_records SET tag = ? WHERE process_name = ? AND tag = ?",
                (new_tag, process_name, old_tag),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def update_app_tag(
        cls,
        process_name: str,
        display_name: str,
        project: str,
        new_tag: str,
    ):
        """Reclassify one App Breakdown identity in canonical and legacy data.

        Project is preferred when available. The display-name fallback also
        catches segments recorded before the project column was introduced.
        """
        conn = cls._conn()
        try:
            if project:
                segment_where = (
                    "process_name = ? COLLATE NOCASE "
                    "AND (project = ? COLLATE NOCASE OR display_name = ?)"
                )
                segment_args = (process_name, project, display_name)
                record_where = (
                    "process_name = ? COLLATE NOCASE "
                    "AND (project = ? COLLATE NOCASE OR display_name = ?)"
                )
                record_args = segment_args
            else:
                segment_where = "process_name = ? COLLATE NOCASE AND display_name = ?"
                segment_args = (process_name, display_name)
                record_where = segment_where
                record_args = segment_args

            conn.execute(
                f"UPDATE time_segments SET tag = ? WHERE {segment_where}",
                (new_tag, *segment_args),
            )
            conn.execute(
                f"UPDATE time_records SET tag = ? WHERE {record_where}",
                (new_tag, *record_args),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  Write                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _split_interval(start: datetime, end: datetime):
        """Yield pieces that do not cross a local calendar-day boundary."""
        cursor = start
        while cursor < end:
            next_midnight = (
                cursor.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1)
            )
            chunk_end = min(next_midnight, end)
            yield cursor, chunk_end
            cursor = chunk_end

    @staticmethod
    def _upsert_tag_total_conn(conn, tag: str, start: datetime, end: datetime):
        if not tag or tag == "Idle" or end <= start:
            return
        updated_at = end.isoformat(timespec="seconds")
        for chunk_start, chunk_end in TimeRecorder._split_interval(start, end):
            conn.execute(
                """
                INSERT INTO tag_time_records (date, tag, seconds, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date, tag) DO UPDATE SET
                    seconds = seconds + excluded.seconds,
                    updated_at = excluded.updated_at
                """,
                (
                    chunk_start.date().isoformat(),
                    tag,
                    (chunk_end - chunk_start).total_seconds(),
                    updated_at,
                ),
            )

    def add_tag_time(self, tag: str, seconds: float):
        """Add one de-duplicated sample for a tag (once per monitor poll)."""
        if seconds <= 0 or not tag or tag == "Idle":
            return
        now = datetime.now()
        conn = self._conn()
        try:
            self._upsert_tag_total_conn(conn, tag, now - timedelta(seconds=seconds), now)
            conn.commit()
        finally:
            conn.close()

    def add_time(
        self,
        process_name: str,
        display_name: str,
        seconds: float,
        project: str = "",
        tag: str = "Other",
        record_tag_total: bool = True,
    ):
        """Accumulate *seconds* for *process_name* on today's date."""
        if seconds <= 0:
            return
        now = datetime.now()
        start = now - timedelta(seconds=seconds)
        conn = self._conn()
        try:
            for chunk_start, chunk_end in self._split_interval(start, now):
                chunk_seconds = (chunk_end - chunk_start).total_seconds()
                chunk_date = chunk_start.date().isoformat()
                end_iso = chunk_end.isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO time_records (date, process_name, display_name, project, tag, seconds, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, process_name, project)
                    DO UPDATE SET
                        seconds    = seconds + excluded.seconds,
                        display_name = excluded.display_name,
                        tag         = excluded.tag,
                        updated_at = excluded.updated_at
                    """,
                    (chunk_date, process_name, display_name, project, tag, chunk_seconds, end_iso),
                )
                conn.execute(
                    """
                    INSERT INTO time_segments
                        (date, start_time, end_time, process_name, display_name, project, tag, seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_date,
                        chunk_start.isoformat(timespec="seconds"),
                        end_iso,
                        process_name,
                        display_name,
                        project,
                        tag,
                        chunk_seconds,
                    ),
                )
            if record_tag_total:
                self._upsert_tag_total_conn(conn, tag, start, now)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  Read                                                               #
    # ------------------------------------------------------------------ #

    def get_today_summary(self) -> List[Dict]:
        """Return [{display_name, process_name, project, tag, seconds}] for today, sorted desc."""
        today = date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT display_name, process_name, project, tag, SUM(seconds) AS seconds
                FROM time_segments
                WHERE date = ? AND tag != 'Idle'
                GROUP BY display_name, process_name, project, tag
                ORDER BY seconds DESC
                """,
                (today,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_today_total(self) -> float:
        return sum(row["seconds"] for row in self.get_today_tag_distribution())

    def get_today_live_totals(self) -> Dict[str, float]:
        """Return dashboard totals with same-tag overlap removed."""
        tag_totals = {
            row["tag"]: row["seconds"] for row in self.get_today_tag_distribution()
        }
        conn = self._conn()
        try:
            today = date.today().isoformat()
            focus_rows = conn.execute(
                """
                SELECT tag, COALESCE(SUM(seconds), 0) AS seconds
                FROM focus_time_records
                WHERE date = ? AND tag IN ('Indie', 'Work')
                GROUP BY tag
                """,
                (today,),
            ).fetchall()
        finally:
            conn.close()
        totals = {
            "total": sum(tag_totals.values()),
            "indie": tag_totals.get("Indie", 0.0),
            "work": tag_totals.get("Work", 0.0),
        }
        totals["indie_focus"] = 0.0
        totals["work_focus"] = 0.0
        for focus in focus_rows:
            if focus["tag"] == "Indie":
                totals["indie_focus"] = focus["seconds"]
            elif focus["tag"] == "Work":
                totals["work_focus"] = focus["seconds"]
        return totals

    def get_range_summary(
        self, start: date, end: date
    ) -> Dict[str, List[Tuple[str, float]]]:
        """Return canonical segment totals grouped by app and date."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT date, display_name, SUM(seconds) AS seconds
                FROM time_segments
                WHERE date >= ? AND date <= ? AND tag != 'Idle'
                GROUP BY date, display_name
                ORDER BY date, display_name
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()

        result: Dict[str, List[Tuple[str, float]]] = {}
        for r in rows:
            name = r["display_name"]
            result.setdefault(name, []).append((r["date"], r["seconds"]))
        return result

    def get_range_app_breakdown(self, start: date, end: date) -> List[Dict]:
        """Return per-app/project/tag totals from canonical segments."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT display_name, process_name, project, tag,
                       SUM(seconds) AS seconds,
                       COUNT(DISTINCT date) AS days_active
                FROM time_segments
                WHERE date >= ? AND date <= ? AND tag != 'Idle'
                GROUP BY display_name, process_name, project, tag
                ORDER BY seconds DESC
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_first_record_date(self) -> Optional[str]:
        """Return the first date available in the canonical segment table."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT MIN(date) AS date FROM time_segments WHERE tag != 'Idle'"
            ).fetchone()
        finally:
            conn.close()
        return row["date"] if row and row["date"] else None

    def get_daily_totals(self, start: date, end: date) -> List[Tuple[str, float]]:
        """Return [(date_iso, total_seconds)] for each day in range."""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT date, SUM(seconds) AS total
                FROM time_segments
                WHERE date >= ? AND date <= ? AND tag != 'Idle'
                GROUP BY date
                ORDER BY date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        return [(r["date"], r["total"]) for r in rows]

    def get_daily_tag_breakdown(self, start: date, end: date) -> Dict[str, Dict[str, float]]:
        """Return de-duplicated {date_iso: {tag: seconds}} for [start, end]."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date, tag, seconds FROM tag_time_records WHERE date >= ? AND date <= ? ORDER BY date",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            fallback = conn.execute(
                """
                SELECT date, tag, SUM(seconds) AS seconds
                FROM time_segments
                WHERE date >= ? AND date <= ? AND tag != 'Idle'
                  AND date NOT IN (SELECT DISTINCT date FROM tag_time_records WHERE date >= ? AND date <= ?)
                GROUP BY date, tag ORDER BY date
                """,
                (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        result: Dict[str, Dict[str, float]] = {}
        for r in [*rows, *fallback]:
            result.setdefault(r["date"], {})[r["tag"]] = r["seconds"]
        return result

    # ------------------------------------------------------------------ #
    #  Codex event + time persistence                                    #
    # ------------------------------------------------------------------ #

    def add_codex_event(self, event: str, session_id: str, project: str, observed_at: datetime):
        """Persist a raw Codex hook event (idempotent via UNIQUE constraint)."""
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO codex_events
                    (event, session_id, project, observed_at, received_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event, session_id, project, observed_at.isoformat(), now),
            )
            conn.commit()
        finally:
            conn.close()

    def record_chrome_url_event(self, url: str, domain: str = ""):
        """Persist a Chrome URL report event for the Event Log."""
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO chrome_url_events (url, domain, received_at)
                VALUES (?, ?, ?)
                """,
                (url, domain, now),
            )
            conn.commit()
        finally:
            conn.close()

    def add_codex_time(
        self,
        project: str,
        project_name: str,
        seconds: float,
        tag: str = "Work",
        record_tag_total: bool = True,
    ):
        """Accumulate Codex project time for today."""
        if seconds <= 0:
            return
        now = datetime.now()
        start = now - timedelta(seconds=seconds)
        conn = self._conn()
        try:
            for chunk_start, chunk_end in self._split_interval(start, now):
                chunk_seconds = (chunk_end - chunk_start).total_seconds()
                chunk_date = chunk_start.date().isoformat()
                end_iso = chunk_end.isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO codex_time_records (date, project, project_name, seconds, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(date, project)
                    DO UPDATE SET
                        seconds      = seconds + excluded.seconds,
                        project_name = excluded.project_name,
                        updated_at   = excluded.updated_at
                    """,
                    (chunk_date, project, project_name, chunk_seconds, end_iso),
                )
                conn.execute(
                    """
                    INSERT INTO time_segments
                        (date, start_time, end_time, process_name, display_name, project, tag, seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_date,
                        chunk_start.isoformat(timespec="seconds"),
                        end_iso,
                        "codex.exe",
                        project_name,
                        project,
                        tag,
                        chunk_seconds,
                    ),
                )
            if record_tag_total:
                self._upsert_tag_total_conn(conn, tag, start, now)
            conn.commit()
        finally:
            conn.close()

    def add_idle_time(self, seconds: float):
        """Record idle time as a segment with tag 'Idle'."""
        if seconds <= 0:
            return
        now = datetime.now()
        start = now - timedelta(seconds=seconds)
        conn = self._conn()
        try:
            for chunk_start, chunk_end in self._split_interval(start, now):
                conn.execute(
                    """
                    INSERT INTO time_segments
                        (date, start_time, end_time, process_name, display_name, project, tag, seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_start.date().isoformat(),
                        chunk_start.isoformat(timespec="seconds"),
                        chunk_end.isoformat(timespec="seconds"),
                        "idle",
                        "Idle",
                        "",
                        "Idle",
                        (chunk_end - chunk_start).total_seconds(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def add_focus_time(self, tag: str, seconds: float):
        """Accumulate keyboard-focus time for the two dashboard focus tags."""
        if seconds <= 0 or tag not in {"Indie", "Work"}:
            return
        now = datetime.now()
        start = now - timedelta(seconds=seconds)
        conn = self._conn()
        try:
            for chunk_start, chunk_end in self._split_interval(start, now):
                chunk_seconds = (chunk_end - chunk_start).total_seconds()
                chunk_date = chunk_start.date().isoformat()
                updated_at = chunk_end.isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO focus_time_records (date, tag, seconds, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(date, tag)
                    DO UPDATE SET
                        seconds = seconds + excluded.seconds,
                        updated_at = excluded.updated_at
                    """,
                    (chunk_date, tag, chunk_seconds, updated_at),
                )
            conn.commit()
        finally:
            conn.close()

    def get_codex_today_summary(self) -> List[Dict]:
        """Return [{project_name, project, seconds}] for today, sorted desc."""
        today = date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT project_name, project, SUM(seconds) AS seconds
                FROM codex_time_records
                WHERE date = ?
                GROUP BY project
                ORDER BY seconds DESC
                """,
                (today,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_codex_today_total(self) -> float:
        return sum(r["seconds"] for r in self.get_codex_today_summary())

    def get_codex_range_total(self, start: date, end: date) -> float:
        """Return total Codex seconds in [start, end]."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) AS total FROM codex_time_records WHERE date >= ? AND date <= ?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        finally:
            conn.close()
        return row["total"] if row else 0.0

    # ------------------------------------------------------------------ #
    #  Tag management                                                     #
    # ------------------------------------------------------------------ #

    def list_tags(self) -> List[Dict]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT id, name, color, is_system FROM tags ORDER BY is_system DESC, name").fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def add_tag(self, name: str, color: str) -> Dict:
        conn = self._conn()
        try:
            cur = conn.execute("INSERT INTO tags (name, color, is_system) VALUES (?, ?, 0)", (name, color))
            conn.commit()
            tag_id = cur.lastrowid
        finally:
            conn.close()
        return {"id": tag_id, "name": name, "color": color, "is_system": 0}

    def update_tag(self, tag_id: int, name: str = None, color: str = None) -> bool:
        conn = self._conn()
        try:
            tag = conn.execute("SELECT name, is_system FROM tags WHERE id = ?", (tag_id,)).fetchone()
            if not tag:
                return False
            is_system = tag["is_system"]
            sets = []
            params = []
            if name is not None and not is_system:
                sets.append("name = ?")
                params.append(name)
            if color is not None and not is_system:
                sets.append("color = ?")
                params.append(color)
            if not sets:
                return False
            params.append(tag_id)
            conn.execute(f"UPDATE tags SET {', '.join(sets)} WHERE id = ?", params)
            if name is not None and not is_system and name != tag["name"]:
                conn.execute("UPDATE time_records SET tag = ? WHERE tag = ?", (name, tag["name"]))
                conn.execute("UPDATE time_segments SET tag = ? WHERE tag = ?", (name, tag["name"]))
            conn.commit()
        finally:
            conn.close()
        return True

    def delete_tag(self, tag_id: int) -> bool:
        conn = self._conn()
        try:
            tag = conn.execute("SELECT name, is_system FROM tags WHERE id = ?", (tag_id,)).fetchone()
            if not tag or tag["is_system"]:
                return False
            # Reassign records to Other
            conn.execute("UPDATE time_records SET tag = 'Other' WHERE tag = ?", (tag["name"],))
            conn.execute("UPDATE time_segments SET tag = 'Other' WHERE tag = ?", (tag["name"],))
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            conn.commit()
        finally:
            conn.close()
        return True

    def get_today_timeline(self, target_date: str = None) -> List[Dict]:
        """Return active [{hour, tag, seconds}] using wall-clock time per hour.

        Idle is reported separately by ``get_today_idle_time`` and is therefore
        intentionally omitted here. Overlapping windows in the same tag are
        merged, so a Timeline bar never exceeds one hour merely because multiple
        Indie or Work apps are visible.
        """
        today = target_date or date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT start_time, end_time, tag, seconds FROM time_segments WHERE date = ? AND tag != 'Idle'",
                (today,),
            ).fetchall()
        finally:
            conn.close()

        # Split each segment into per-hour buckets, then merge overlap within
        # each (hour, tag) bucket. Other dashboard views intentionally retain
        # their multi-window cumulative totals.
        hour_tag_intervals = {}  # {(hour, tag): [(start, end), ...]}
        for r in rows:
            start = datetime.fromisoformat(r["start_time"])
            end = datetime.fromisoformat(r["end_time"])
            tag = r["tag"]
            cursor = start
            while cursor < end:
                # Next hour boundary
                next_boundary = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                chunk_end = min(next_boundary, end)
                hour = cursor.hour
                key = (hour, tag)
                hour_tag_intervals.setdefault(key, []).append((cursor, chunk_end))
                cursor = chunk_end

        result = []
        for (hour, tag), intervals in sorted(hour_tag_intervals.items()):
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            seconds = sum((end - start).total_seconds() for start, end in merged)
            result.append({"hour": hour, "tag": tag, "seconds": round(seconds, 1)})
        return result

    def get_today_tag_distribution(self, target_date: str = None) -> List[Dict]:
        """Return active tag totals with overlap removed within each tag."""
        today = target_date or date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT tag, seconds FROM tag_time_records WHERE date = ? ORDER BY seconds DESC",
                (today,),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT tag, SUM(seconds) AS seconds FROM time_segments WHERE date = ? AND tag != 'Idle' GROUP BY tag ORDER BY seconds DESC",
                    (today,),
                ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_today_app_breakdown(self, target_date: str = None) -> List[Dict]:
        """Return app rows with a stable project identity for a date."""
        today = target_date or date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT display_name, process_name, MAX(project) AS project,
                       tag, SUM(seconds) AS seconds
                FROM time_segments
                WHERE date = ? AND tag != 'Idle'
                GROUP BY process_name, display_name, tag
                ORDER BY seconds DESC
                """,
                (today,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  Efficiency analysis                                               #
    # ------------------------------------------------------------------ #

    def get_today_idle_time(self, target_date: str = None) -> float:
        """Return total idle seconds for a date from time_segments."""
        today = target_date or date.today().isoformat()
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(seconds), 0) AS total FROM time_segments WHERE date = ? AND tag = 'Idle'",
                (today,),
            ).fetchone()
        finally:
            conn.close()
        return row["total"] if row else 0

    def get_focus_sessions(self, target_date: str = None) -> List[Dict]:
        """Return longest continuous sessions (same process_name) for a date.
        Returns [{process_name, display_name, tag, start_time, end_time, seconds}] sorted by seconds desc.
        """
        d = target_date or date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT start_time, end_time, process_name, display_name, tag, seconds FROM time_segments WHERE date = ? AND tag != 'Idle' ORDER BY start_time",
                (d,),
            ).fetchall()
        finally:
            conn.close()

        # Merge consecutive segments with same process_name
        sessions = []
        for r in rows:
            if sessions and sessions[-1]["process_name"] == r["process_name"] and sessions[-1]["end_time"] == r["start_time"]:
                sessions[-1]["end_time"] = r["end_time"]
                sessions[-1]["seconds"] += r["seconds"]
            else:
                sessions.append(dict(r))
        sessions.sort(key=lambda x: -x["seconds"])
        return sessions[:10]  # Top 10

    def get_switch_frequency(self, target_date: str = None) -> List[Dict]:
        """Return [{hour, switches}] — number of process_name changes per hour."""
        d = target_date or date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT start_time, process_name FROM time_segments WHERE date = ? AND tag != 'Idle' ORDER BY start_time",
                (d,),
            ).fetchall()
        finally:
            conn.close()

        hour_switches = {}
        prev_proc = None
        for r in rows:
            start = datetime.fromisoformat(r["start_time"])
            hour = start.hour
            if prev_proc is not None and prev_proc != r["process_name"]:
                hour_switches[hour] = hour_switches.get(hour, 0) + 1
            prev_proc = r["process_name"]

        return [{"hour": h, "switches": s} for h, s in sorted(hour_switches.items())]

    def get_peak_hours(self, start: date, end: date) -> List[Dict]:
        """Return [{hour, avg_seconds}] — average active seconds per hour across date range."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT start_time, end_time, seconds FROM time_segments WHERE date >= ? AND date <= ? AND tag != 'Idle'",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()

        hour_totals = {}
        for r in rows:
            s = datetime.fromisoformat(r["start_time"])
            e = datetime.fromisoformat(r["end_time"])
            cursor = s
            while cursor < e:
                next_boundary = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                chunk_end = min(next_boundary, e)
                chunk_seconds = (chunk_end - cursor).total_seconds()
                hour_totals[cursor.hour] = hour_totals.get(cursor.hour, 0) + chunk_seconds
                cursor = chunk_end

        num_days = max(1, (end - start).days + 1)
        return [{"hour": h, "avg_seconds": round(hour_totals.get(h, 0) / num_days, 1)} for h in range(24)]

    def get_period_tag_summary(self, start: date, end: date, group_by: str = "week") -> List[Dict]:
        """Return [{period, tag, seconds}] grouped by week or month.
        group_by: 'week' (ISO week) or 'month'.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date, tag, SUM(seconds) AS seconds FROM time_segments WHERE date >= ? AND date <= ? AND tag != 'Idle' GROUP BY date, tag ORDER BY date",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()

        period_tag = {}
        for r in rows:
            d = date.fromisoformat(r["date"])
            if group_by == "week":
                iso = d.isocalendar()
                period_key = f"{iso[0]}-W{iso[1]:02d}"
            else:
                period_key = d.strftime("%Y-%m")
            key = (period_key, r["tag"])
            period_tag[key] = period_tag.get(key, 0) + r["seconds"]

        return [
            {"period": p, "tag": t, "seconds": round(s, 1)}
            for (p, t), s in sorted(period_tag.items())
        ]

    def get_daily_tag_trend(self, start: date, end: date) -> List[Dict]:
        """Return [{date, tag, seconds}] for each day and tag in range."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date, tag, SUM(seconds) AS seconds FROM time_segments WHERE date >= ? AND date <= ? AND tag != 'Idle' GROUP BY date, tag ORDER BY date",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  Export                                                             #
    # ------------------------------------------------------------------ #

    def export_csv(self, file_path: str, start: Optional[date] = None, end: Optional[date] = None):
        """Export canonical active-time segments aggregated by app/project/tag."""
        conn = self._conn()
        try:
            if start and end:
                rows = conn.execute(
                    """
                    SELECT date, display_name, process_name, project, tag,
                           SUM(seconds) AS seconds, MAX(end_time) AS updated_at
                    FROM time_segments
                    WHERE date >= ? AND date <= ? AND tag != 'Idle'
                    GROUP BY date, display_name, process_name, project, tag
                    ORDER BY date, seconds DESC
                    """,
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT date, display_name, process_name, project, tag,
                           SUM(seconds) AS seconds, MAX(end_time) AS updated_at
                    FROM time_segments
                    WHERE tag != 'Idle'
                    GROUP BY date, display_name, process_name, project, tag
                    ORDER BY date, seconds DESC
                    """
                ).fetchall()
        finally:
            conn.close()

        with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "DisplayName", "ProcessName", "Project", "Tag", "Seconds", "UpdatedAt"])
            for r in rows:
                writer.writerow([r["date"], r["display_name"], r["process_name"], r["project"], r["tag"], r["seconds"], r["updated_at"]])
