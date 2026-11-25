import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tracker.execution_stats import read_execution_stats, merge_execution


class ExecutionStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "devin.db"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.day = "2026-11-24"
        self.start = datetime(2026, 11, 24).timestamp()

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        return read_execution_stats(self.day, self.db, (self.logs,))

    def log(self, name, turns):
        rows = [{"type": "session_meta", "payload": {"id": "session", "cwd": "project"}}]
        rows += [{"type": "event_msg", "payload": {"type": "task_complete", **p}} for p in turns]
        (self.logs / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken", encoding="utf-8")

    def test_codex_no_devin_and_idle_gap_and_dedup(self):
        turns = [{"turn_id": "a", "started_at": self.start+10, "completed_at": self.start+20, "duration_ms": 10000},
                 {"turn_id": "b", "started_at": self.start+3600, "completed_at": self.start+3620, "duration_ms": 20000}]
        self.log("one.jsonl", turns)
        self.log("archived-copy.jsonl", turns)
        r = self.read()["sources"]["codex"]
        self.assertEqual(r["seconds"], 30)
        self.assertEqual(r["count"], 2)

    def test_codex_midnight_and_missing(self):
        self.log("old-session.jsonl", [{"turn_id": "a", "completed_at": self.start+10, "duration_ms": 20000},
                                      {"turn_id": "b", "completed_at": self.start+20}])
        r = self.read()["sources"]["codex"]
        self.assertEqual(r["seconds"], 10)
        self.assertEqual(r["missing"], 1)

    def test_devin_dedup_cross_day_and_missing(self):
        c = sqlite3.connect(self.db)
        c.executescript("CREATE TABLE sessions(id TEXT,title TEXT,working_directory TEXT,created_at REAL,last_activity_at REAL); CREATE TABLE message_nodes(session_id TEXT,created_at REAL,chat_message TEXT);")
        c.execute("INSERT INTO sessions VALUES ('s','task','project',?,?)", (self.start-100,self.start+100))
        for request, ms, begin in [("a", 20000, self.start-10), ("a",20000,self.start-10), ("b",None,self.start+10)]:
            meta = {"request_id":request, "started_generation_at":begin, "metrics":{"total_time_ms":ms}}
            c.execute("INSERT INTO message_nodes VALUES (?,?,?)", ('s',self.start+10,json.dumps({"role":"assistant","metadata":meta})))
        c.commit();c.close()
        r = self.read()["sources"]["devin"]
        self.assertEqual(r["seconds"], 10)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["missing"], 1)

    def test_merge_preserves_metrics_and_old_cache(self):
        self.log("one.jsonl", [{"completed_at":self.start+1,"duration_ms":1000}])
        p = self.read()
        r = merge_execution([p,p])
        self.assertEqual(r["sources"]["codex"]["seconds"],2)
        self.assertEqual(len(r["tasks"]),2)
        self.assertIsNone(merge_execution([]))

    def test_activity_wrapper_and_device_merge(self):
        from unittest.mock import patch
        import tracker.ai_token_reader as reader
        from tracker.time_recorder import _merge_devin_activity
        self.log("one.jsonl", [{"turn_id":"one", "completed_at":self.start+20,"duration_ms":10000}])
        with patch.object(reader, "_DEVIN_DB", self.db), patch.object(reader, "_CODEX_SESSIONS_DIR", self.logs), patch.object(reader, "_CODEX_ARCHIVED_DIR", self.root / "absent"):
            activity = reader.read_daily_devin_activity(self.day)
        self.assertEqual(activity["execution"]["sources"]["codex"]["seconds"],10)
        merged = _merge_devin_activity([activity, {"projects":[],"titles":[]}])
        self.assertEqual(merged["execution"]["sources"]["codex"]["seconds"],10)

    def test_codex_uses_current_task_name_and_follows_rename(self):
        self.log("one.jsonl", [{"turn_id":"one", "completed_at":self.start+20,"duration_ms":10000}])
        state = sqlite3.connect(self.root / "state_5.sqlite")
        state.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,name TEXT,title TEXT)")
        state.execute("INSERT INTO threads VALUES ('session','Build a laser turret','original prompt')")
        state.commit()
        before = self.read()
        self.assertEqual(before["tasks"][0]["title"], "Build a laser turret")
        state.execute("UPDATE threads SET name='New task title'")
        state.commit();state.close()
        after = self.read()
        self.assertEqual(after["tasks"][0]["title"], "New task title")
        self.assertEqual(before["sources"], after["sources"])

    def test_codex_title_index_fallback_latest_wins(self):
        self.log("one.jsonl", [{"completed_at":self.start+20,"duration_ms":10000}])
        (self.root / "session_index.jsonl").write_text("\n".join(json.dumps(row) for row in [
            {"id":"session", "thread_name":"Old name"}, {"id":"session", "thread_name":"Actual task title"}]) + "\n{broken", encoding="utf-8")
        self.assertEqual(self.read()["tasks"][0]["title"], "Actual task title")

    def test_codex_legacy_state_title_and_missing_title(self):
        self.log("one.jsonl", [{"completed_at":self.start+20,"duration_ms":10000}])
        self.assertEqual(self.read()["tasks"][0]["title"], "Untitled task")
        state=sqlite3.connect(self.root / "state_4.sqlite")
        state.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT)")
        state.execute("INSERT INTO threads VALUES ('session',?)", ("# Files mentioned by the user:\nattachment.png\n## My request:\nCreate **3D model**",))
        state.commit();state.close()
        self.assertEqual(self.read()["tasks"][0]["title"], "Create 3D model")

    def test_incomplete_turn_excluded(self):
        (self.logs/'running.jsonl').write_text(json.dumps({"type":"event_msg","payload":{"type":"task_started","started_at":self.start+1}}))
        self.assertEqual(self.read()["sources"]["codex"]["count"],0)


if __name__ == "__main__":
    unittest.main()
