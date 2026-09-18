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

    def test_devin_tokens_fall_back_to_message_metrics(self):
        """Newer Devin builds drop response_dimensions; usage is per message."""
        with tempfile.TemporaryDirectory() as td:
            source_db = Path(td) / "sessions.db"
            conn = sqlite3.connect(source_db)
            conn.execute("CREATE TABLE sessions (id TEXT, model TEXT, created_at INTEGER, metadata TEXT)")
            conn.execute(
                "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT, node_id INTEGER, parent_node_id INTEGER, "
                "chat_message TEXT, created_at INTEGER, metadata TEXT)"
            )
            created = int(datetime.combine(date.today(), time(10, 0)).timestamp())
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                ("sess-1", "glm-5-2", created, json.dumps({"total_credit_cost": 0})),
            )

            def node(node_id, role, metrics=None, message_id=None):
                chat = {"message_id": message_id, "role": role, "metadata": {"metrics": metrics}}
                conn.execute(
                    "INSERT INTO message_nodes (session_id, node_id, parent_node_id, "
                    "chat_message, created_at, metadata) VALUES (?, ?, ?, ?, ?, NULL)",
                    ("sess-1", node_id, node_id - 1, json.dumps(chat), created),
                )

            node(0, "user", message_id="u-1")
            # cache_creation_tokens are new input tokens for Anthropic-style models.
            first = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 20,
                     "cache_creation_tokens": 50}
            node(1, "assistant", first, "a-1")
            # Branching/compaction stores the same message again: must not double count.
            node(2, "assistant", first, "a-1")
            node(3, "assistant",
                 {"input_tokens": 5, "output_tokens": 7, "cache_read_tokens": 30,
                  "cache_creation_tokens": None}, "a-2")
            conn.commit()
            conn.close()

            old_db = ai_reader._DEVIN_DB
            ai_reader._DEVIN_DB = source_db
            try:
                result = ai_reader.read_devin_daily_tokens(date.today().isoformat())
            finally:
                ai_reader._DEVIN_DB = old_db

        entry = result[date.today().isoformat()]["glm-5-2"]
        self.assertEqual(entry["input"], 155)
        self.assertEqual(entry["output"], 17)
        self.assertEqual(entry["cached"], 50)
        self.assertEqual(entry["sessions"], 1)

    def test_workbuddy_tokens_split_by_entry_date(self):
        """WorkBuddy usage is per request and dated by the entry timestamp."""
        with tempfile.TemporaryDirectory() as td:
            projects = Path(td) / "projects" / "some-project"
            projects.mkdir(parents=True)
            transcript = projects / "session-1.jsonl"

            def entry(day, hour, message_id, usage, raw=None):
                ts = int(datetime.combine(day, time(hour, 0)).timestamp() * 1000)
                return json.dumps({
                    "id": message_id, "timestamp": ts, "type": "function_call",
                    "providerData": {
                        "messageId": message_id, "model": "wb-model",
                        "usage": usage, "rawUsage": raw or {},
                    },
                })

            def user_msg(day, hour, message_id):
                ts = int(datetime.combine(day, time(hour, 0)).timestamp() * 1000)
                return json.dumps({
                    "id": message_id, "timestamp": ts, "type": "message",
                    "role": "user", "content": [{"type": "input_text", "text": "hi"}],
                })

            today = date.today()
            yesterday = today - timedelta(days=1)
            rows = [
                user_msg(today, 9, "u-1"),
                # inputTokens is the whole prompt: 1000 - 400 cached = 600 new.
                entry(today, 10, "m-1", {
                    "inputTokens": 1000, "outputTokens": 100,
                    "inputTokensDetails": [{"cached_tokens": 400}],
                }),
                # The same request echoed again must not be counted twice.
                entry(today, 10, "m-1", {
                    "inputTokens": 1000, "outputTokens": 100,
                    "inputTokensDetails": [{"cached_tokens": 400}],
                }),
                user_msg(today, 10, "u-2"),
                # The same user message echoed again must not be counted twice.
                user_msg(today, 10, "u-2"),
                # Cache writes are billed as input but reported separately.
                entry(today, 11, "m-2", {"inputTokens": 500, "outputTokens": 50},
                      {"cache_creation_input_tokens": 200}),
                # A further tool round in the same turn is not another message.
                entry(today, 11, "m-3", {"inputTokens": 100, "outputTokens": 10}),
                user_msg(yesterday, 8, "u-0"),
                entry(yesterday, 9, "m-0", {
                    "inputTokens": 300, "outputTokens": 30,
                    "inputTokensDetails": [{"cached_tokens": 0}],
                }),
            ]
            transcript.write_text("\n".join(rows), encoding="utf-8")

            old_dir = ai_reader._WORKBUDDY_PROJECTS_DIR
            ai_reader._WORKBUDDY_PROJECTS_DIR = Path(td) / "projects"
            try:
                result = ai_reader.read_workbuddy_daily_tokens()
            finally:
                ai_reader._WORKBUDDY_PROJECTS_DIR = old_dir

        today_entry = result[today.isoformat()]["wb-model"]
        self.assertEqual(today_entry["input"], 1400)
        self.assertEqual(today_entry["output"], 160)
        self.assertEqual(today_entry["cached"], 400)
        # Two user messages, three API requests.
        self.assertEqual(today_entry["messages"], 2)
        self.assertEqual(today_entry["sessions"], 1)
        yesterday_entry = result[yesterday.isoformat()]["wb-model"]
        self.assertEqual(yesterday_entry["input"], 300)
        self.assertEqual(yesterday_entry["messages"], 1)

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

    def test_codex_user_messages_are_counted_per_local_day(self):
        """Only user-typed messages count, not the injected per-turn context."""
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

            def user_msg(ts, text):
                return {
                    "timestamp": ts, "type": "response_item",
                    "payload": {
                        "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }

            rows = [
                {"type": "session_meta", "payload": {"base_instructions": {"provenance": {"model": "codex"}}}},
                # Framework context injected at the start of the turn.
                user_msg(first_ts, "<environment_context>\n  <cwd>D:\\Work</cwd>\n</environment_context>"),
                user_msg(first_ts, "first prompt"),
                {"timestamp": first_ts, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 80}}}},
                # A message with no plain text part is context too.
                user_msg(second_ts, "<recommended_plugins>\n- Airtable\n</recommended_plugins>"),
                user_msg(second_ts, "second prompt"),
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

        self.assertEqual(result[first_day.isoformat()]["codex"]["messages"], 1)
        self.assertEqual(result[second_day.isoformat()]["codex"]["messages"], 1)
        self.assertEqual(result[first_day.isoformat()]["codex"]["sessions"], 1)
        self.assertEqual(result[second_day.isoformat()]["codex"]["sessions"], 1)

    def test_recorder_requires_a_device_id(self):
        """An empty device_id must not be able to duplicate the day's totals.

        ``_init_db`` backfills today's tag totals under whatever id the recorder
        holds, so allowing the default "" let a caller who forgot the argument
        write a second copy of the day under an empty device key.
        """
        with self.assertRaises(ValueError):
            TimeRecorder(device_id="")

        self.recorder.add_time("x.exe", "X", 10, "", "Work")
        blank = self.recorder._conn().execute(
            "SELECT COUNT(*) FROM tag_time_records WHERE device_id = ''"
        ).fetchone()[0]
        self.assertEqual(blank, 0)

    def test_all_history_uses_cloud_only_dates(self):
        old_date = (date.today() - timedelta(days=100)).isoformat()
        self.recorder.upsert_cloud_time_record({
            "device_id": "remote", "date": old_date, "process_name": "x.exe",
            "display_name": "X", "project": "", "tag": "Work",
            "seconds": 60, "updated_at": "2026-01-01T00:00:00",
        })
        self.assertEqual(self.recorder.get_first_record_date(), old_date)

    def test_tag_totals_are_scoped_to_this_device(self):
        """A cloud-pulled snapshot of the same day must not be added to the live row.

        ``tag_time_records`` is keyed by (device_id, date, tag), so the same day
        holds one row per device. Summing across devices doubled every headline
        number on a synced setup.
        """
        self.recorder.add_time("x.exe", "X", 10, "", "Work")
        self.recorder.upsert_cloud_tag_time_record({
            "device_id": "remote", "date": date.today().isoformat(), "tag": "Work",
            "seconds": 3600, "updated_at": "2026-01-01T00:00:00",
        })

        tags = {row["tag"]: row["seconds"] for row in self.recorder.get_today_tag_distribution()}
        self.assertAlmostEqual(tags["Work"], 10)
        self.assertAlmostEqual(self.recorder.get_today_live_totals()["total"], 10)

    def test_repeated_add_time_keeps_first_tag(self):
        """Adding a second sample under a different tag keeps the recorded total.

        The conflict key omits ``tag``, so ``DO UPDATE SET tag = excluded.tag``
        used to re-attribute every accumulated second to the newest tag.
        """
        self.recorder.add_time("chrome.exe", "Chrome", 10, "", "Work")
        self.recorder.add_time("chrome.exe", "Chrome", 5, "", "Indie")

        apps = self.recorder.get_today_app_breakdown()
        chrome = next(r for r in apps if r["process_name"] == "chrome.exe")
        tags = {row["tag"]: row["seconds"] for row in self.recorder.get_today_tag_distribution()}
        self.assertAlmostEqual(chrome["seconds"], 15)
        self.assertAlmostEqual(tags["Work"], 10)
        self.assertAlmostEqual(tags["Indie"], 5)

    def test_devin_tool_calls_filter_sessions_before_reading_blobs(self):
        """Only the requested day's tool calls are read, without a full table scan.

        The joined query planned ``SCAN tool_call_state`` and re-read ~340 MB of
        JSON blobs per refresh; the day's sessions must drive the lookup.
        """
        statements = []
        real_connect = sqlite3.connect

        class _TracingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                statements.append(" ".join(sql.split()))
                return super().execute(sql, *args)

        def tracing_connect(*args, **kwargs):
            kwargs["factory"] = _TracingConnection
            return real_connect(*args, **kwargs)

        devin_db = Path(tempfile.mktemp(suffix=".db"))
        con = real_connect(str(devin_db))
        con.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at REAL);"
            "CREATE TABLE tool_call_state (session_id TEXT, tool_call_id TEXT,"
            " tool_call_json TEXT, PRIMARY KEY (session_id, tool_call_id));"
        )
        today_ts = datetime.combine(date.today(), time(12, 0)).timestamp()
        yesterday_ts = today_ts - 86400
        con.execute("INSERT INTO sessions VALUES ('today', ?)", (today_ts,))
        con.execute("INSERT INTO sessions VALUES ('old', ?)", (yesterday_ts,))
        con.execute(
            "INSERT INTO tool_call_state VALUES ('today', 'c1', ?)",
            (json.dumps({
                "_meta": repr({"cognition.ai/inferenceToolName": "mcp_call_tool"}),
                "title": "Calling mysql_query from mysql",
            }),),
        )
        con.execute(
            "INSERT INTO tool_call_state VALUES ('old', 'c2', ?)",
            (json.dumps({
                "_meta": repr({"cognition.ai/inferenceToolName": "mcp_call_tool"}),
                "title": "Calling old_tool from oldserver",
            }),),
        )
        con.commit()
        con.close()

        old_db = ai_reader._DEVIN_DB
        ai_reader._DEVIN_DB = devin_db
        try:
            with patch.object(sqlite3, "connect", tracing_connect):
                result = ai_reader.read_all_daily_tool_calls(
                    date.today().isoformat(), date.today().isoformat()
                )
        finally:
            ai_reader._DEVIN_DB = old_db
            try:
                os.unlink(devin_db)
            except OSError:
                pass

        self.assertEqual(result[date.today().isoformat()]["mcp"], {"mysql.mysql_query": 1})
        # The blob fetch must be constrained to the day's sessions...
        self.assertTrue(
            any("tool_call_state" in s and "session_id IN" in s for s in statements),
            statements,
        )
        # ...and must not join sessions so that the plan degrades to a full scan.
        self.assertFalse(
            any("JOIN sessions" in s and "tool_call_state" in s for s in statements),
            statements,
        )


if __name__ == "__main__":
    unittest.main()
