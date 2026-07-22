"""Tests for Codex activity tracking: heartbeat, dedup, project switch,
idle timeout, gap cap, concurrent sessions, invalid input."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# We need to set DB_FILE before importing TimeRecorder
import config
import tracker.time_recorder as _tr_module
config.DB_FILE = Path(tempfile.mktemp(suffix=".db"))
_tr_module.DB_FILE = config.DB_FILE

from tracker.time_recorder import TimeRecorder
from tracker.codex_activity_manager import (
    CodexActivityManager,
    IDLE_TIMEOUT_SECONDS,
    MAX_GAP_SECONDS,
)


def ts(seconds_from_base: float) -> datetime:
    """Helper: create UTC datetime from base + offset."""
    base = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds_from_base)


class TestCodexActivity(unittest.TestCase):

    def setUp(self):
        # Fresh DB and manager for each test
        self._db_path = tempfile.mktemp(suffix=".db")
        config.DB_FILE = Path(self._db_path)
        _tr_module.DB_FILE = config.DB_FILE
        self.recorder = TimeRecorder()
        self.manager = CodexActivityManager(self.recorder)

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def _send(self, event, session_id, project, observed_at):
        return self.manager.handle_event(event, session_id, project, observed_at)

    # ------------------------------------------------------------------ #
    #  1. Normal heartbeat accumulates time                              #
    # ------------------------------------------------------------------ #

    def test_normal_heartbeat(self):
        proj = "D:\\Projects\\MyGame"
        r1 = self._send("SessionStart", "s1", proj, ts(0))
        self.assertEqual(r1["status"], "ok")
        self.assertAlmostEqual(r1["added_seconds"], 0.0, places=1)

        r2 = self._send("PreToolUse", "s1", proj, ts(30))
        self.assertEqual(r2["status"], "ok")
        self.assertAlmostEqual(r2["added_seconds"], 30.0, places=1)

        r3 = self._send("UserPromptSubmit", "s1", proj, ts(60))
        self.assertEqual(r3["status"], "ok")
        self.assertAlmostEqual(r3["added_seconds"], 30.0, places=1)

        summary = self.recorder.get_codex_today_summary()
        self.assertEqual(len(summary), 1)
        self.assertAlmostEqual(summary[0]["seconds"], 60.0, places=1)

    # ------------------------------------------------------------------ #
    #  2. Duplicate events are idempotent                                #
    # ------------------------------------------------------------------ #

    def test_duplicate_event(self):
        proj = "D:\\Projects\\MyGame"
        r1 = self._send("PreToolUse", "s1", proj, ts(0))
        self.assertEqual(r1["status"], "ok")

        r2 = self._send("PreToolUse", "s1", proj, ts(0))
        self.assertEqual(r2["status"], "duplicate")

        # No time should have been added by the duplicate
        summary = self.recorder.get_codex_today_summary()
        # First heartbeat with no prior heartbeat adds 0s, so summary may be empty
        total = sum(s["seconds"] for s in summary)
        self.assertAlmostEqual(total, 0.0, places=1)

    # ------------------------------------------------------------------ #
    #  3. Project switch within same session                             #
    # ------------------------------------------------------------------ #

    def test_project_switch(self):
        proj_a = "D:\\Projects\\ProjectA"
        proj_b = "D:\\Projects\\ProjectB"

        self._send("SessionStart", "s1", proj_a, ts(0))
        self._send("PreToolUse", "s1", proj_a, ts(30))  # +30s for A

        # Switch to project B
        r = self._send("PreToolUse", "s1", proj_b, ts(60))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["project_name"], "ProjectB")
        # Gap from project A's last heartbeat (30s ago) but for project B
        # there's no previous heartbeat, so added_seconds should be 0
        self.assertAlmostEqual(r["added_seconds"], 0.0, places=1)

        self._send("PreToolUse", "s1", proj_b, ts(90))  # +30s for B

        summary = self.recorder.get_codex_today_summary()
        by_name = {s["project_name"]: s["seconds"] for s in summary}
        self.assertAlmostEqual(by_name["ProjectA"], 30.0, places=1)
        self.assertAlmostEqual(by_name["ProjectB"], 30.0, places=1)

    # ------------------------------------------------------------------ #
    #  4. Idle timeout: gap > 5 min → no time added                      #
    # ------------------------------------------------------------------ #

    def test_idle_timeout(self):
        proj = "D:\\Projects\\MyGame"
        self._send("SessionStart", "s1", proj, ts(0))
        self._send("PreToolUse", "s1", proj, ts(30))  # +30s

        # Gap of 6 minutes (> IDLE_TIMEOUT_SECONDS=300)
        r = self._send("PreToolUse", "s1", proj, ts(30 + 360))
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["added_seconds"], 0.0, places=1)

        summary = self.recorder.get_codex_today_summary()
        self.assertAlmostEqual(summary[0]["seconds"], 30.0, places=1)

    # ------------------------------------------------------------------ #
    #  5. Gap cap: gap between heartbeats capped at 2 min                #
    # ------------------------------------------------------------------ #

    def test_gap_cap(self):
        proj = "D:\\Projects\\MyGame"
        self._send("SessionStart", "s1", proj, ts(0))
        self._send("PreToolUse", "s1", proj, ts(30))  # +30s

        # Gap of 4 minutes (240s) — within idle timeout but > MAX_GAP
        r = self._send("PreToolUse", "s1", proj, ts(30 + 240))
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(r["added_seconds"], MAX_GAP_SECONDS, places=1)

        summary = self.recorder.get_codex_today_summary()
        # 30 + 120 = 150
        self.assertAlmostEqual(summary[0]["seconds"], 30 + MAX_GAP_SECONDS, places=1)

    # ------------------------------------------------------------------ #
    #  6. Concurrent sessions on same project don't double-count         #
    # ------------------------------------------------------------------ #

    def test_concurrent_sessions_same_project(self):
        proj = "D:\\Projects\\MyGame"

        # Session 1 starts
        self._send("SessionStart", "s1", proj, ts(0))

        # Session 2 starts 10s later (same project)
        r2 = self._send("SessionStart", "s2", proj, ts(10))
        # Gap from project's last heartbeat (s1 at t=0) is 10s
        # But s2 is a new session — it should still use project-level last_ts
        self.assertAlmostEqual(r2["added_seconds"], 10.0, places=1)

        # Both sessions send heartbeats at t=30
        r3 = self._send("PreToolUse", "s1", proj, ts(30))
        self.assertAlmostEqual(r3["added_seconds"], 20.0, places=1)  # gap from s2's t=10

        r4 = self._send("PreToolUse", "s2", proj, ts(35))
        # Gap from project's last heartbeat (s1 at t=30) is 5s
        self.assertAlmostEqual(r4["added_seconds"], 5.0, places=1)

        # Total should be 10 + 20 + 5 = 35, not doubled
        summary = self.recorder.get_codex_today_summary()
        self.assertAlmostEqual(summary[0]["seconds"], 35.0, places=1)

    # ------------------------------------------------------------------ #
    #  7. Stop event does not add extra time                             #
    # ------------------------------------------------------------------ #

    def test_stop_no_extra_time(self):
        proj = "D:\\Projects\\MyGame"
        self._send("SessionStart", "s1", proj, ts(0))
        self._send("PreToolUse", "s1", proj, ts(30))  # +30s

        r = self._send("Stop", "s1", proj, ts(45))
        self.assertEqual(r["status"], "stopped")

        summary = self.recorder.get_codex_today_summary()
        self.assertAlmostEqual(summary[0]["seconds"], 30.0, places=1)

    # ------------------------------------------------------------------ #
    #  8. Invalid input: bad event, bad path, bad timestamp              #
    # ------------------------------------------------------------------ #

    def test_invalid_event_type(self):
        is_valid, msg, dt = CodexActivityManager.validate_event(
            "InvalidEvent", "s1", "D:\\Projects\\X", "2026-07-20T10:00:00.000Z"
        )
        self.assertFalse(is_valid)

    def test_invalid_relative_path(self):
        is_valid, msg, dt = CodexActivityManager.validate_event(
            "PreToolUse", "s1", "relative/path", "2026-07-20T10:00:00.000Z"
        )
        self.assertFalse(is_valid)
        self.assertIn("absolute", msg.lower())

    def test_invalid_timestamp(self):
        is_valid, msg, dt = CodexActivityManager.validate_event(
            "PreToolUse", "s1", "D:\\Projects\\X", "not-a-date"
        )
        self.assertFalse(is_valid)

    def test_empty_session_id(self):
        is_valid, msg, dt = CodexActivityManager.validate_event(
            "PreToolUse", "", "D:\\Projects\\X", "2026-07-20T10:00:00.000Z"
        )
        self.assertFalse(is_valid)

    # ------------------------------------------------------------------ #
    #  9. Get active projects                                            #
    # ------------------------------------------------------------------ #

    def test_get_active_projects(self):
        proj = "D:\\Projects\\MyGame"
        self._send("SessionStart", "s1", proj, ts(0))

        # Use a recent timestamp so it's still active
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        self._send("PreToolUse", "s1", proj, recent)

        active = self.manager.get_active_projects()
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["active"])
        self.assertEqual(active[0]["project_name"], "MyGame")


if __name__ == "__main__":
    unittest.main()
