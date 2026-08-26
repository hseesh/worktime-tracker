"""Regression coverage for cloud, relabeling and AI usage integrity fixes."""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

import config
import tracker.ai_token_reader as ai_reader
import tracker.time_recorder as time_recorder_module
from tracker.time_recorder import TimeRecorder


class TestDataIntegrityRegressions(unittest.TestCase):

    def setUp(self):
        self._db_path = tempfile.mktemp(suffix=".db")
        config.DB_FILE = Path(self._db_path)
        time_recorder_module.DB_FILE = config.DB_FILE
        self.recorder = TimeRecorder(device_id="local")

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_app_relabel_rebuilds_tag_totals(self):
        self.recorder.add_time("Devin.exe", "Devin [A]", 10, "A", "Work")
        self.recorder.update_app_tag("Devin.exe", "Devin [A]", "A", "Other")

        apps = self.recorder.get_today_app_breakdown()
        tags = {row["tag"]: row["seconds"] for row in self.recorder.get_today_tag_distribution()}
        self.assertEqual(apps[0]["tag"], "Other")
        self.assertAlmostEqual(tags["Other"], 10)
        self.assertNotIn("Work", tags)

    def test_custom_tag_rename_and_delete_rebuild_totals(self):
        tag = self.recorder.add_tag("Focus", "#123456")
        self.recorder.add_time("x.exe", "X", 8, "", "Focus")

        self.assertTrue(self.recorder.update_tag(tag["id"], name="Deep Work"))
        renamed = {row["tag"]: row["seconds"] for row in self.recorder.get_today_tag_distribution()}
        self.assertAlmostEqual(renamed["Deep Work"], 8)
        self.assertNotIn("Focus", renamed)

        self.assertTrue(self.recorder.delete_tag(tag["id"]))
        deleted = {row["tag"]: row["seconds"] for row in self.recorder.get_today_tag_distribution()}
        self.assertAlmostEqual(deleted["Other"], 8)
        self.assertNotIn("Deep Work", deleted)

    def test_live_local_row_is_not_replaced_by_cloud_snapshot(self):
        self.recorder.add_time("x.exe", "X", 10, "", "Work")
        stale = self.recorder.get_local_time_records_for_sync(date.today().isoformat())[0]
        self.recorder.add_time("x.exe", "X", 1, "", "Work")

        self.recorder.upsert_cloud_time_record(stale)

        self.assertAlmostEqual(self.recorder.get_today_app_breakdown()[0]["seconds"], 11)

    def test_ai_and_tool_rows_sum_across_devices(self):
        today = date.today().isoformat()
        common = {
            "date": today, "source": "codex", "output_tokens": 0,
            "cached_tokens": 0, "sessions": 1, "messages": 1,
            "updated_at": "2026-08-21T00:00:00",
        }
        self.recorder.upsert_cloud_ai_token_daily(dict(common, device_id="A", input_tokens=100))
        self.recorder.upsert_cloud_ai_token_daily(dict(common, device_id="B", input_tokens=200))
        self.recorder.upsert_cloud_tool_call_daily({
            "device_id": "A", "date": today, "category": "mcp",
            "name": "server.tool", "count": 2,
        })
        self.recorder.upsert_cloud_tool_call_daily({
            "device_id": "B", "date": today, "category": "mcp",
            "name": "server.tool", "count": 3,
        })

        ai = self.recorder.get_ai_token_daily_range(date.today(), date.today())
        tools = self.recorder.get_tool_call_daily_range(date.today(), date.today())
        self.assertEqual(ai[today]["codex"]["input"], 300)
        self.assertEqual(ai[today]["codex"]["sessions"], 2)
        self.assertEqual(tools[today]["mcp"]["server.tool"], 5)

    def test_token_total_does_not_double_count_cached_input(self):
        summary = ai_reader._summarize_day({
            "codex": {
                "input": 100, "output": 20, "cached": 80,
                "sessions": 1, "messages": 0,
            }
        })
        self.assertEqual(summary["total_tokens"], 120)
        self.assertEqual(summary["cached_tokens"], 80)
        self.assertEqual(summary["by_source"][0]["tokens"], 120)

    def test_devin_target_date_uses_local_midnight(self):
        with tempfile.TemporaryDirectory() as td:
            source_db = Path(td) / "sessions.db"
            conn = sqlite3.connect(source_db)
            conn.execute("CREATE TABLE sessions (id TEXT, model TEXT, created_at INTEGER, metadata TEXT)")
            conn.execute(
                "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT, node_id INTEGER, parent_node_id INTEGER, "
                "chat_message TEXT, created_at INTEGER, metadata TEXT)"
            )
            local_one_am = datetime.combine(date.today(), time(1, 0))
            metadata = json.dumps({
                "response_dimensions": [
                    {"uid": "input_tokens", "kind": {"CumulativeMetric": {"value": 123}}}
                ]
            })
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                ("sess-1", "model", int(local_one_am.timestamp()), metadata),
            )
            conn.commit()
            conn.close()

            old_db = ai_reader._DEVIN_DB
            ai_reader._DEVIN_DB = source_db
            try:
                result = ai_reader.read_devin_daily_tokens(date.today().isoformat())
            finally:
                ai_reader._DEVIN_DB = old_db

        self.assertEqual(result[date.today().isoformat()]["model"]["input"], 123)

    def test_empty_cache_days_are_marked_and_not_rescanned(self):
        with patch("tracker.ai_token_reader.read_all_daily_tokens", return_value={}) as ai_scan:
            self.recorder.sync_ai_token_cache(days=2)
            self.recorder.sync_ai_token_cache(days=2)
        with patch("tracker.ai_token_reader.read_all_daily_tool_calls", return_value={}) as tool_scan:
            self.recorder.sync_tool_call_cache(days=2)
            self.recorder.sync_tool_call_cache(days=2)

        self.assertEqual(ai_scan.call_count, 1)
        self.assertEqual(tool_scan.call_count, 1)

    def test_legacy_ai_cache_tables_migrate_to_device_keys(self):
        os.unlink(self._db_path)
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "CREATE TABLE ai_token_daily (date TEXT, source TEXT, input_tokens INTEGER, "
            "output_tokens INTEGER, cached_tokens INTEGER, sessions INTEGER, messages INTEGER, "
            "updated_at TEXT, PRIMARY KEY(date, source))"
        )
        conn.execute(
            "CREATE TABLE tool_call_daily (date TEXT, category TEXT, name TEXT, count INTEGER, "
            "updated_at TEXT, PRIMARY KEY(date, category, name))"
        )
        conn.execute(
            "INSERT INTO ai_token_daily VALUES ('2026-08-20', 'codex', 10, 2, 8, 1, 0, 'now')"
        )
        conn.execute(
            "INSERT INTO tool_call_daily VALUES ('2026-08-20', 'mcp', 'server.tool', 3, 'now')"
        )
        conn.commit()
        conn.close()

        migrated = TimeRecorder(device_id="local")
        conn = migrated._conn()
        try:
            ai_row = conn.execute("SELECT device_id, input_tokens FROM ai_token_daily").fetchone()
            tool_row = conn.execute("SELECT device_id, count FROM tool_call_daily").fetchone()
        finally:
            conn.close()

        self.assertEqual((ai_row["device_id"], ai_row["input_tokens"]), ("local", 10))
        self.assertEqual((tool_row["device_id"], tool_row["count"]), ("local", 3))

    def test_codex_cumulative_tokens_are_split_at_local_midnight(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            archived = Path(td) / "archived"
            sessions.mkdir()
            archived.mkdir()
            first_day = date.today() - timedelta(days=1)
            second_day = date.today()
            local_tz = datetime.now().astimezone().tzinfo
            first_ts = datetime.combine(first_day, time(23, 59), tzinfo=local_tz).isoformat()
            second_ts = datetime.combine(second_day, time(0, 1), tzinfo=local_tz).isoformat()
            rollout = sessions / f"rollout-{first_day.isoformat()}T23-59-00-test.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"base_instructions": {"provenance": {"model": "codex"}}}},
                {"timestamp": first_ts, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 80}}}},
                {"timestamp": second_ts, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 150, "output_tokens": 20, "cached_input_tokens": 100}}}},
            ]
            rollout.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            old_sessions = ai_reader._CODEX_SESSIONS_DIR
            old_archived = ai_reader._CODEX_ARCHIVED_DIR
            ai_reader._CODEX_SESSIONS_DIR = sessions
            ai_reader._CODEX_ARCHIVED_DIR = archived
            try:
                result = ai_reader.read_codex_daily_tokens()
            finally:
                ai_reader._CODEX_SESSIONS_DIR = old_sessions
                ai_reader._CODEX_ARCHIVED_DIR = old_archived

        self.assertEqual(result[first_day.isoformat()]["codex"]["input"], 100)
        self.assertEqual(result[second_day.isoformat()]["codex"]["input"], 50)
        self.assertEqual(result[second_day.isoformat()]["codex"]["cached"], 20)

    def test_all_history_uses_cloud_only_dates(self):
        old_date = (date.today() - timedelta(days=100)).isoformat()
        self.recorder.upsert_cloud_time_record({
            "device_id": "remote", "date": old_date, "process_name": "x.exe",
            "display_name": "X", "project": "", "tag": "Work",
            "seconds": 60, "updated_at": "2026-01-01T00:00:00",
        })
        self.assertEqual(self.recorder.get_first_record_date(), old_date)


if __name__ == "__main__":
    unittest.main()
