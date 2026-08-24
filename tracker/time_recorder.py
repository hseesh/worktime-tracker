"""Core timing logic: accumulate per-process foreground seconds and persist to SQLite."""

import csv
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from config import DB_FILE

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS time_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT    NOT NULL DEFAULT '',
    date        TEXT    NOT NULL,
    process_name TEXT   NOT NULL,
    display_name TEXT   NOT NULL,
    project     TEXT    NOT NULL DEFAULT '',
    tag         TEXT    NOT NULL DEFAULT 'Other',
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(device_id, date, process_name, project)
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
    device_id   TEXT    NOT NULL DEFAULT '',
    date        TEXT    NOT NULL,
    tag         TEXT    NOT NULL,
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (device_id, date, tag)
);
"""

_MIGRATE_ADD_PROJECT_SQL = """
CREATE TABLE IF NOT EXISTS time_records_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT    NOT NULL DEFAULT '',
    date        TEXT    NOT NULL,
    process_name TEXT   NOT NULL,
    display_name TEXT   NOT NULL,
    project     TEXT    NOT NULL DEFAULT '',
    seconds     REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(device_id, date, process_name, project)
);
INSERT INTO time_records_new (device_id, date, process_name, display_name, project, seconds, updated_at)
SELECT '', date, process_name, display_name, '', seconds, updated_at FROM time_records;
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

_CREATE_AI_TOKEN_SQL = """
CREATE TABLE IF NOT EXISTS ai_token_daily (
    device_id     TEXT    NOT NULL DEFAULT '',
    date           TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens  INTEGER NOT NULL DEFAULT 0,
    sessions       INTEGER NOT NULL DEFAULT 0,
    messages       INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (device_id, date, source)
);
"""

_CREATE_TOOL_CALL_SQL = """
CREATE TABLE IF NOT EXISTS tool_call_daily (
    device_id     TEXT    NOT NULL DEFAULT '',
    date           TEXT    NOT NULL,
    category       TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    count          INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT    NOT NULL,
    PRIMARY KEY (device_id, date, category, name)
);
"""

_CREATE_DEVIN_ACTIVITY_SQL = """
CREATE TABLE IF NOT EXISTS devin_activity_daily (
    date           TEXT    NOT NULL PRIMARY KEY,
    data_json      TEXT    NOT NULL DEFAULT '{}',
    updated_at     TEXT    NOT NULL
);
"""

_CREATE_CACHE_SCAN_SQL = """
CREATE TABLE IF NOT EXISTS cache_scan_state (
    device_id     TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL,
    date          TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (device_id, kind, date)
);
"""


class TimeRecorder:
    """Thread-safe-ish SQLite recorder.

    All public methods open a short-lived connection so that calls from the
    tracker thread and the UI thread do not share a single connection object.
    """

    def __init__(self, device_id: str = ""):
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._device_id = device_id
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
            conn.execute(_CREATE_AI_TOKEN_SQL)
            conn.execute(_CREATE_TOOL_CALL_SQL)
            conn.execute(_CREATE_DEVIN_ACTIVITY_SQL)
            conn.execute(_CREATE_CACHE_SCAN_SQL)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_time_segments_date_tag "
                "ON time_segments (date, tag)"
            )
            self._migrate_add_project(conn)
            self._migrate_add_tag(conn)
            self._migrate_add_segment_project(conn)
            self._migrate_add_device_id(conn)
            self._migrate_ai_cache_device_id(conn)
            self._migrate_tool_cache_device_id(conn)
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

    def _migrate_current_tag_totals(self, conn):
        """Backfill today's de-duplicated tag totals from legacy segments once."""
        today = date.today().isoformat()
        existing = conn.execute(
            "SELECT 1 FROM tag_time_records WHERE device_id = ? AND date = ? LIMIT 1",
            (self._device_id, today),
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
                "INSERT OR REPLACE INTO tag_time_records (device_id, date, tag, seconds, updated_at) VALUES (?, ?, ?, ?, ?)",
                (self._device_id, today, tag, seconds, now),
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

    def _migrate_add_device_id(self, conn):
        """Add device_id column to time_records and tag_time_records.

        Recreates both tables with device_id in the unique/primary key.
        Existing rows are backfilled with this device's device_id so that
        local recording continues to accumulate on the same rows.
        """
        # --- time_records ---
        cols = conn.execute("PRAGMA table_info(time_records)").fetchall()
        col_names = [c["name"] for c in cols]
        if "device_id" not in col_names:
            conn.execute(
                """
                CREATE TABLE time_records_new (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id   TEXT    NOT NULL DEFAULT '',
                    date        TEXT    NOT NULL,
                    process_name TEXT   NOT NULL,
                    display_name TEXT   NOT NULL,
                    project     TEXT    NOT NULL DEFAULT '',
                    tag         TEXT    NOT NULL DEFAULT 'Other',
                    seconds     REAL    NOT NULL DEFAULT 0,
                    updated_at  TEXT    NOT NULL,
                    UNIQUE(device_id, date, process_name, project)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO time_records_new
                    (device_id, date, process_name, display_name, project, tag, seconds, updated_at)
                SELECT ?, date, process_name, display_name, project, tag, seconds, updated_at
                FROM time_records
                """,
                (self._device_id,),
            )
            conn.execute("DROP TABLE time_records")
            conn.execute("ALTER TABLE time_records_new RENAME TO time_records")

        # --- tag_time_records ---
        cols = conn.execute("PRAGMA table_info(tag_time_records)").fetchall()
        col_names = [c["name"] for c in cols]
        if "device_id" not in col_names:
            conn.execute(
                """
                CREATE TABLE tag_time_records_new (
                    device_id   TEXT    NOT NULL DEFAULT '',
                    date        TEXT    NOT NULL,
                    tag         TEXT    NOT NULL,
                    seconds     REAL    NOT NULL DEFAULT 0,
                    updated_at  TEXT    NOT NULL,
                    PRIMARY KEY (device_id, date, tag)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO tag_time_records_new (device_id, date, tag, seconds, updated_at)
                SELECT ?, date, tag, seconds, updated_at FROM tag_time_records
                """,
                (self._device_id,),
            )
            conn.execute("DROP TABLE tag_time_records")
            conn.execute("ALTER TABLE tag_time_records_new RENAME TO tag_time_records")

    def _migrate_ai_cache_device_id(self, conn):
        """Preserve legacy device-local AI rows while adding device identity."""
        cols = conn.execute("PRAGMA table_info(ai_token_daily)").fetchall()
        if "device_id" in {c["name"] for c in cols}:
            return
        conn.execute(
            """
            CREATE TABLE ai_token_daily_new (
                device_id TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,
                source TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                sessions INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (device_id, date, source)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO ai_token_daily_new
                (device_id, date, source, input_tokens, output_tokens,
                 cached_tokens, sessions, messages, updated_at)
            SELECT ?, date, source, input_tokens, output_tokens,
                   cached_tokens, sessions, messages, updated_at
            FROM ai_token_daily
            """,
            (self._device_id,),
        )
        conn.execute("DROP TABLE ai_token_daily")
        conn.execute("ALTER TABLE ai_token_daily_new RENAME TO ai_token_daily")

    def _migrate_tool_cache_device_id(self, conn):
        """Preserve legacy device-local tool rows while adding device identity."""
        cols = conn.execute("PRAGMA table_info(tool_call_daily)").fetchall()
        if "device_id" in {c["name"] for c in cols}:
            return
        conn.execute(
            """
            CREATE TABLE tool_call_daily_new (
                device_id TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (device_id, date, category, name)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tool_call_daily_new
                (device_id, date, category, name, count, updated_at)
            SELECT ?, date, category, name, count, updated_at
            FROM tool_call_daily
            """,
            (self._device_id,),
        )
        conn.execute("DROP TABLE tool_call_daily")
        conn.execute("ALTER TABLE tool_call_daily_new RENAME TO tool_call_daily")

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

    def _rebuild_local_tag_totals_conn(self, conn, dates):
        """Recompute de-duplicated local tag totals after historical relabeling."""
        dates = sorted(set(dates))
        if not dates:
            return
        placeholders = ",".join("?" for _ in dates)
        rows = conn.execute(
            f"SELECT date, start_time, end_time, tag FROM time_segments "
            f"WHERE date IN ({placeholders}) AND tag != 'Idle' "
            "ORDER BY date, tag, start_time, end_time",
            dates,
        ).fetchall()
        conn.execute(
            f"DELETE FROM tag_time_records WHERE device_id = ? "
            f"AND date IN ({placeholders})",
            (self._device_id, *dates),
        )
        grouped = {}
        for row in rows:
            grouped.setdefault((row["date"], row["tag"]), []).append(
                (datetime.fromisoformat(row["start_time"]), datetime.fromisoformat(row["end_time"]))
            )
        now = datetime.now().isoformat(timespec="seconds")
        for (d_iso, tag), intervals in grouped.items():
            merged = []
            for start, end in intervals:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            seconds = sum((end - start).total_seconds() for start, end in merged)
            conn.execute(
                "INSERT INTO tag_time_records "
                "(device_id, date, tag, seconds, updated_at) VALUES (?, ?, ?, ?, ?)",
                (self._device_id, d_iso, tag, seconds, now),
            )

    def update_tags_from_process_tags(self, process_tags: dict):
        """Update time_records.tag for processes whose tag has changed in config.

        Only affects this device's rows; other devices' data is managed by
        cloud sync.
        """
        conn = self._conn()
        try:
            for proc, tag in process_tags.items():
                conn.execute(
                    "UPDATE time_records SET tag = ? WHERE device_id = ? AND process_name = ? AND tag != ?",
                    (tag, self._device_id, proc, tag),
                )
            conn.commit()
        finally:
            conn.close()

    def update_process_tag_selective(self, process_name: str, old_tag: str, new_tag: str):
        """Update time_records.tag only for records with the old tag, preserving
        records that were tagged differently via keyword rules.

        Only affects this device's rows.
        """
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE time_records SET tag = ? WHERE device_id = ? AND process_name = ? AND tag = ?",
                (new_tag, self._device_id, process_name, old_tag),
            )
            conn.commit()
        finally:
            conn.close()

    def update_app_tag(
        self,
        process_name: str,
        display_name: str,
        project: str,
        new_tag: str,
    ):
        """Reclassify one App Breakdown identity in canonical and legacy data.

        Project is preferred when available. The display-name fallback also
        catches segments recorded before the project column was introduced.

        Only updates this device's rows in time_records (other devices' data
        is managed by cloud sync). time_segments is always local-only so it
        is updated unconditionally.
        """
        conn = self._conn()
        try:
            if project:
                segment_where = (
                    "process_name = ? COLLATE NOCASE "
                    "AND (project = ? COLLATE NOCASE OR display_name = ?)"
                )
                segment_args = (process_name, project, display_name)
                record_where = (
                    "device_id = ? AND process_name = ? COLLATE NOCASE "
                    "AND (project = ? COLLATE NOCASE OR display_name = ?)"
                )
                record_args = (self._device_id, process_name, project, display_name)
            else:
                segment_where = "process_name = ? COLLATE NOCASE AND display_name = ?"
                segment_args = (process_name, display_name)
                record_where = "device_id = ? AND process_name = ? COLLATE NOCASE AND display_name = ?"
                record_args = (self._device_id, process_name, display_name)

            affected_dates = [
                row["date"] for row in conn.execute(
                    f"SELECT DISTINCT date FROM time_segments WHERE {segment_where}",
                    segment_args,
                ).fetchall()
            ]
            conn.execute(
                f"UPDATE time_segments SET tag = ? WHERE {segment_where}",
                (new_tag, *segment_args),
            )
            conn.execute(
                f"UPDATE time_records SET tag = ? WHERE {record_where}",
                (new_tag, *record_args),
            )
            self._rebuild_local_tag_totals_conn(conn, affected_dates)
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

    def _upsert_tag_total_conn(self, conn, tag: str, start: datetime, end: datetime):
        if not tag or tag == "Idle" or end <= start:
            return
        updated_at = end.isoformat(timespec="seconds")
        for chunk_start, chunk_end in TimeRecorder._split_interval(start, end):
            conn.execute(
                """
                INSERT INTO tag_time_records (device_id, date, tag, seconds, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id, date, tag) DO UPDATE SET
                    seconds = seconds + excluded.seconds,
                    updated_at = excluded.updated_at
                """,
                (
                    self._device_id,
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
                    INSERT INTO time_records (device_id, date, process_name, display_name, project, tag, seconds, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, date, process_name, project)
                    DO UPDATE SET
                        seconds    = seconds + excluded.seconds,
                        display_name = excluded.display_name,
                        tag         = excluded.tag,
                        updated_at = excluded.updated_at
                    """,
                    (self._device_id, chunk_date, process_name, display_name, project, tag, chunk_seconds, end_iso),
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
        """Return per-app/project/tag totals.

        Aggregates from time_records (which includes synced data from all
        devices) instead of time_segments (local-only per-second data).
        Falls back to time_segments for dates that have no time_records
        rows (e.g. today, which hasn't been synced yet).
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT display_name, process_name, project, tag,
                       SUM(seconds) AS seconds,
                       COUNT(DISTINCT date) AS days_active
                FROM time_records
                WHERE date >= ? AND date <= ? AND tag != 'Idle'
                GROUP BY display_name, process_name, project, tag
                ORDER BY seconds DESC
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            # Fallback: for dates with no time_records rows (e.g. today),
            # supplement with time_segments data.
            fallback = conn.execute(
                """
                SELECT display_name, process_name, project, tag,
                       SUM(seconds) AS seconds,
                       COUNT(DISTINCT date) AS days_active
                FROM time_segments
                WHERE date >= ? AND date <= ? AND tag != 'Idle'
                  AND date NOT IN (SELECT DISTINCT date FROM time_records WHERE date >= ? AND date <= ?)
                GROUP BY display_name, process_name, project, tag
                ORDER BY seconds DESC
                """,
                (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in [*rows, *fallback]]

    def get_first_record_date(self) -> Optional[str]:
        """Return the first local or cloud-synced work date."""
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT MIN(date) AS date FROM (
                    SELECT date FROM time_segments WHERE tag != 'Idle'
                    UNION ALL
                    SELECT date FROM time_records WHERE tag != 'Idle'
                    UNION ALL
                    SELECT date FROM tag_time_records
                )
                """
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
        """Return de-duplicated {date_iso: {tag: seconds}} for [start, end].

        Aggregates across all device_ids (SUM) so multi-device sync data
        is merged into per-day totals.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date, tag, SUM(seconds) AS seconds FROM tag_time_records "
                "WHERE date >= ? AND date <= ? GROUP BY date, tag ORDER BY date",
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
    #  Cloud sync helpers                                                 #
    # ------------------------------------------------------------------ #

    def get_local_time_records_for_sync(self, include_date: str) -> List[Dict]:
        """Return this device's time_records rows for dates <= include_date (ISO)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT device_id, date, process_name, display_name, project, tag, seconds, updated_at "
                "FROM time_records WHERE device_id = ? AND date <= ?",
                (self._device_id, include_date),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_local_tag_time_records_for_sync(self, include_date: str) -> List[Dict]:
        """Return this device's tag_time_records rows for dates <= include_date (ISO)."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT device_id, date, tag, seconds, updated_at "
                "FROM tag_time_records WHERE device_id = ? AND date <= ?",
                (self._device_id, include_date),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def upsert_cloud_time_record(self, row: Dict):
        """Insert or replace a time_records row from cloud sync.

        Conflict policy: cloud-priority (REPLACE). The caller should only
        pass rows with date < today so today's local recording is untouched.
        """
        if row.get("device_id") == self._device_id and row.get("date") == date.today().isoformat():
            # The tracker may have added more seconds after this row was pushed.
            # Never replace the live local row with its older cloud snapshot.
            return
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO time_records
                    (device_id, date, process_name, display_name, project, tag, seconds, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["device_id"],
                    row["date"],
                    row["process_name"],
                    row["display_name"],
                    row.get("project", ""),
                    row.get("tag", "Other"),
                    row["seconds"],
                    row.get("updated_at", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def upsert_cloud_tag_time_record(self, row: Dict):
        """Insert or replace a tag_time_records row from cloud sync."""
        if row.get("device_id") == self._device_id and row.get("date") == date.today().isoformat():
            return
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO tag_time_records
                    (device_id, date, tag, seconds, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["device_id"],
                    row["date"],
                    row["tag"],
                    row["seconds"],
                    row.get("updated_at", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_local_ai_token_daily_for_sync(self, include_date: str) -> List[Dict]:
        """Return this device's ai_token_daily rows for dates <= include_date.

        Only this device's rows are uploaded; pulled devices remain local cache
        rows and must never be re-published under this device's identity.
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT device_id, date, source, input_tokens, output_tokens, cached_tokens, "
                "sessions, messages, updated_at FROM ai_token_daily "
                "WHERE device_id = ? AND date <= ?",
                (self._device_id, include_date),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def upsert_cloud_ai_token_daily(self, row: Dict):
        """Insert or replace an ai_token_daily row from cloud sync."""
        if row.get("device_id") == self._device_id and row.get("date") == date.today().isoformat():
            return
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO ai_token_daily "
                "(device_id, date, source, input_tokens, output_tokens, cached_tokens, "
                "sessions, messages, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["device_id"],
                    row["date"],
                    row["source"],
                    row.get("input_tokens", 0),
                    row.get("output_tokens", 0),
                    row.get("cached_tokens", 0),
                    row.get("sessions", 0),
                    row.get("messages", 0),
                    row.get("updated_at", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()

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
                    INSERT INTO time_records (device_id, date, process_name, display_name, project, tag, seconds, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, date, process_name, project)
                    DO UPDATE SET
                        seconds    = seconds + excluded.seconds,
                        display_name = excluded.display_name,
                        tag         = excluded.tag,
                        updated_at = excluded.updated_at
                    """,
                    (self._device_id, chunk_date, "codex.exe", project_name, project, tag, chunk_seconds, end_iso),
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

    # ---- AI token daily cache ---------------------------------------

    def upsert_ai_token_daily(self, d_iso: str, source: str, input_tokens: int,
                              output_tokens: int, cached_tokens: int,
                              sessions: int, messages: int):
        """Insert or update one row in ai_token_daily cache."""
        conn = self._conn()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO ai_token_daily (device_id, date, source, input_tokens, output_tokens, "
                "cached_tokens, sessions, messages, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id, date, source) DO UPDATE SET "
                "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, "
                "cached_tokens=excluded.cached_tokens, sessions=excluded.sessions, "
                "messages=excluded.messages, updated_at=excluded.updated_at",
                (self._device_id, d_iso, source, input_tokens, output_tokens, cached_tokens,
                 sessions, messages, now),
            )
            conn.commit()
        finally:
            conn.close()

    def get_ai_token_daily_range(self, start: date, end: date) -> Dict[str, Dict[str, Dict]]:
        """Return cached {date_iso: {source: {input, output, cached, sessions, messages}}}."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date, source, SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(cached_tokens) AS cached_tokens, "
                "SUM(sessions) AS sessions, SUM(messages) AS messages "
                "FROM ai_token_daily WHERE date >= ? AND date <= ? "
                "GROUP BY date, source ORDER BY date, source",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        result: Dict[str, Dict[str, Dict]] = {}
        for r in rows:
            result.setdefault(r["date"], {})[r["source"]] = {
                "input": r["input_tokens"],
                "output": r["output_tokens"],
                "cached": r["cached_tokens"],
                "sessions": r["sessions"],
                "messages": r["messages"],
            }
        return result

    def get_ai_token_today_cached(self) -> Dict[str, Dict]:
        """Return cached {source: {input, output, cached, sessions, messages}} for today."""
        today = date.today().isoformat()
        rng = self.get_ai_token_daily_range(date.today(), date.today())
        return rng.get(today, {})

    def get_cached_token_dates(self) -> set:
        """Return dates whose local source scan completed, including empty days."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date FROM cache_scan_state WHERE device_id = ? AND kind = 'ai_token'",
                (self._device_id,),
            ).fetchall()
        finally:
            conn.close()
        return {r["date"] for r in rows}

    def _mark_cache_dates_scanned(self, kind: str, dates):
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._conn()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO cache_scan_state "
                "(device_id, kind, date, updated_at) VALUES (?, ?, ?, ?)",
                [(self._device_id, kind, d_iso, now) for d_iso in dates],
            )
            conn.commit()
        finally:
            conn.close()

    def _replace_local_ai_token_date(self, d_iso: str, day_data: Dict[str, Dict]):
        """Atomically replace one device-local day so removed sources do not linger."""
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM ai_token_daily WHERE device_id = ? AND date = ?",
                (self._device_id, d_iso),
            )
            now = datetime.now().isoformat(timespec="seconds")
            for source, entry in day_data.items():
                conn.execute(
                    "INSERT INTO ai_token_daily "
                    "(device_id, date, source, input_tokens, output_tokens, cached_tokens, "
                    "sessions, messages, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._device_id, d_iso, source,
                        entry.get("input", 0), entry.get("output", 0),
                        entry.get("cached", 0), entry.get("sessions", 0),
                        entry.get("messages", 0), now,
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO cache_scan_state "
                "(device_id, kind, date, updated_at) VALUES (?, 'ai_token', ?, ?)",
                (self._device_id, d_iso, now),
            )
            conn.commit()
        finally:
            conn.close()

    def sync_ai_token_cache(self, days: int = 400):
        """Scan source data and populate cache for the last *days* days.

        Fills gaps for dates not yet cached (including today). Call once at
        startup or periodically.
        """
        from tracker.ai_token_reader import read_all_daily_tokens
        cached_dates = self.get_cached_token_dates()
        end = date.today()
        start = end - timedelta(days=days - 1)
        requested_dates = {
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        }
        missing_dates = requested_dates - cached_dates
        if not missing_dates:
            return
        # One full scan fills every missing day. Empty days are marked too, so
        # the next startup does not repeat the expensive filesystem traversal.
        all_data = read_all_daily_tokens()
        for d_iso in sorted(missing_dates):
            self._replace_local_ai_token_date(d_iso, all_data.get(d_iso, {}))

    def refresh_ai_token_cache_for_date(self, d_iso: str):
        """Force-refresh a date, including a previously cached completed day."""
        from tracker.ai_token_reader import read_all_daily_tokens
        all_data = read_all_daily_tokens(target_date=d_iso)
        self._replace_local_ai_token_date(d_iso, all_data.get(d_iso, {}))

    def refresh_today_ai_token_cache(self):
        """Refresh today's AI token cache from live source data.

        Called periodically (e.g. every 30 min) so today's cached values
        stay fresh for cloud sync. The dashboard/heatmap still read live
        for display; this is only for the cloud sync push.
        """
        self.refresh_ai_token_cache_for_date(date.today().isoformat())

    # ------------------------------------------------------------------ #
    #  Tool call daily cache (MCP / Skill aggregated counts)             #
    # ------------------------------------------------------------------ #

    def upsert_tool_call_daily(self, d_iso: str, category: str, name: str, count: int):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tool_call_daily "
                "(device_id, date, category, name, count, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._device_id, d_iso, category, name, count,
                 datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()

    def get_tool_call_daily_range(self, start: date, end: date) -> Dict[str, Dict[str, Dict[str, int]]]:
        """Return {date_iso: {category: {name: count}}}."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date, category, name, SUM(count) AS count FROM tool_call_daily "
                "WHERE date >= ? AND date <= ? GROUP BY date, category, name "
                "ORDER BY date, category, name",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        finally:
            conn.close()
        result: Dict[str, Dict[str, Dict[str, int]]] = {}
        for r in rows:
            result.setdefault(r["date"], {}).setdefault(r["category"], {})[r["name"]] = r["count"]
        return result

    def get_cached_tool_call_dates(self) -> set:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date FROM cache_scan_state WHERE device_id = ? AND kind = 'tool_call'",
                (self._device_id,),
            ).fetchall()
        finally:
            conn.close()
        return {r["date"] for r in rows}

    def _replace_local_tool_call_date(self, d_iso: str, day_data: Dict[str, Dict[str, int]]):
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM tool_call_daily WHERE device_id = ? AND date = ?",
                (self._device_id, d_iso),
            )
            now = datetime.now().isoformat(timespec="seconds")
            for category, items in day_data.items():
                for name, count in items.items():
                    conn.execute(
                        "INSERT INTO tool_call_daily "
                        "(device_id, date, category, name, count, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (self._device_id, d_iso, category, name, count, now),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO cache_scan_state "
                "(device_id, kind, date, updated_at) VALUES (?, 'tool_call', ?, ?)",
                (self._device_id, d_iso, now),
            )
            conn.commit()
        finally:
            conn.close()

    def sync_tool_call_cache(self, days: int = 400):
        """Scan source data and populate tool_call_daily cache for uncached dates."""
        from tracker.ai_token_reader import read_all_daily_tool_calls
        cached_dates = self.get_cached_tool_call_dates()
        end = date.today()
        start = end - timedelta(days=days - 1)
        requested_dates = {
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        }
        missing_dates = requested_dates - cached_dates
        if not missing_dates:
            return
        all_data = read_all_daily_tool_calls(start.isoformat(), end.isoformat())
        for d_iso in sorted(missing_dates):
            self._replace_local_tool_call_date(d_iso, all_data.get(d_iso, {}))

    def refresh_tool_call_cache_for_date(self, d_iso: str):
        from tracker.ai_token_reader import read_all_daily_tool_calls
        data = read_all_daily_tool_calls(d_iso, d_iso)
        self._replace_local_tool_call_date(d_iso, data.get(d_iso, {}))

    def refresh_today_tool_call_cache(self):
        """Refresh today's tool call cache from live source data."""
        self.refresh_tool_call_cache_for_date(date.today().isoformat())

    def get_local_tool_call_daily_for_sync(self, include_date: str) -> List[Dict]:
        """Return this device's tool_call_daily rows for dates <= include_date."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT device_id, date, category, name, count, updated_at "
                "FROM tool_call_daily WHERE device_id = ? AND date <= ?",
                (self._device_id, include_date),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def upsert_cloud_tool_call_daily(self, row: Dict):
        if row.get("device_id") == self._device_id and row.get("date") == date.today().isoformat():
            return
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tool_call_daily "
                "(device_id, date, category, name, count, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row["device_id"], row["date"], row["category"], row["name"],
                 row["count"], row.get("updated_at", "")),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  Devin activity daily cache (projects, tool kinds, titles, etc.)   #
    # ------------------------------------------------------------------ #

    def upsert_devin_activity_daily(self, d_iso: str, data_json: str):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO devin_activity_daily (date, data_json, updated_at) "
                "VALUES (?, ?, ?)",
                (d_iso, data_json, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()

    def get_devin_activity_daily(self, d_iso: str) -> Optional[Dict]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT data_json FROM devin_activity_daily WHERE date = ?",
                (d_iso,),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["data_json"]:
            return None
        try:
            return json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def get_cached_devin_activity_dates(self) -> set:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT DISTINCT date FROM devin_activity_daily").fetchall()
        finally:
            conn.close()
        return {r["date"] for r in rows}

    def sync_devin_activity_cache(self, days: int = 400):
        """Scan Devin sessions.db and populate devin_activity_daily for uncached dates."""
        from tracker.ai_token_reader import read_daily_devin_activity
        cached_dates = self.get_cached_devin_activity_dates()
        end = date.today()
        start = end - timedelta(days=days - 1)
        cursor = start
        while cursor <= end:
            d_iso = cursor.isoformat()
            if d_iso not in cached_dates:
                data = read_daily_devin_activity(d_iso)
                if data.get("projects") or data.get("titles") or data.get("msg_dist"):
                    self.upsert_devin_activity_daily(d_iso, json.dumps(data))
            cursor += timedelta(days=1)

    def refresh_today_devin_activity_cache(self):
        """Refresh today's devin activity cache from live source data."""
        from tracker.ai_token_reader import read_daily_devin_activity
        today = date.today().isoformat()
        data = read_daily_devin_activity(today)
        self.upsert_devin_activity_daily(today, json.dumps(data))

    def get_local_devin_activity_for_sync(self, include_date: str) -> List[Dict]:
        """Return this device's devin_activity_daily rows for dates <= include_date."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT date, data_json, updated_at FROM devin_activity_daily WHERE date <= ?",
                (include_date,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r, device_id=self._device_id) for r in rows]

    def upsert_cloud_devin_activity_daily(self, row: Dict):
        if row.get("device_id") == self._device_id and row.get("date") == date.today().isoformat():
            return
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO devin_activity_daily (date, data_json, updated_at) "
                "VALUES (?, ?, ?)",
                (row["date"], row["data_json"], row.get("updated_at", "")),
            )
            conn.commit()
        finally:
            conn.close()

    def cleanup_old_time_segments(self, keep_days: int = 30) -> int:
        """Delete time_segments older than keep_days. Returns deleted row count."""
        cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
        conn = self._conn()
        try:
            cur = conn.execute(
                "DELETE FROM time_segments WHERE date < ?", (cutoff,)
            )
            deleted = cur.rowcount
            conn.commit()
            # Reclaim disk space
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
        if deleted > 0:
            logger.info("Cleaned up %d time_segments rows older than %s", deleted, cutoff)
        return deleted

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
                affected_dates = [
                    row["date"] for row in conn.execute(
                        "SELECT DISTINCT date FROM time_segments WHERE tag = ?",
                        (tag["name"],),
                    ).fetchall()
                ]
                conn.execute("UPDATE time_records SET tag = ? WHERE device_id = ? AND tag = ?", (name, self._device_id, tag["name"]))
                conn.execute("UPDATE time_segments SET tag = ? WHERE tag = ?", (name, tag["name"]))
                conn.execute("UPDATE focus_time_records SET tag = ? WHERE tag = ?", (name, tag["name"]))
                self._rebuild_local_tag_totals_conn(conn, affected_dates)
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
            # Reassign records to Other (only this device's rows in time_records)
            affected_dates = [
                row["date"] for row in conn.execute(
                    "SELECT DISTINCT date FROM time_segments WHERE tag = ?",
                    (tag["name"],),
                ).fetchall()
            ]
            conn.execute("UPDATE time_records SET tag = 'Other' WHERE device_id = ? AND tag = ?", (self._device_id, tag["name"]))
            conn.execute("UPDATE time_segments SET tag = 'Other' WHERE tag = ?", (tag["name"],))
            conn.execute("UPDATE focus_time_records SET tag = 'Other' WHERE tag = ?", (tag["name"],))
            self._rebuild_local_tag_totals_conn(conn, affected_dates)
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
                "SELECT tag, SUM(seconds) AS seconds FROM tag_time_records WHERE date = ? GROUP BY tag ORDER BY seconds DESC",
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
        """Return app rows with a stable project identity for a date.

        Uses time_records (multi-device) with time_segments fallback for
        today's live data that hasn't been synced yet.
        """
        today = target_date or date.today().isoformat()
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT display_name, process_name, MAX(project) AS project,
                       tag, SUM(seconds) AS seconds
                FROM time_records
                WHERE date = ? AND tag != 'Idle'
                GROUP BY process_name, display_name, tag
                ORDER BY seconds DESC
                """,
                (today,),
            ).fetchall()
            if not rows:
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
                           SUM(seconds) AS seconds, MAX(updated_at) AS updated_at
                    FROM time_records
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
                           SUM(seconds) AS seconds, MAX(updated_at) AS updated_at
                    FROM time_records
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
