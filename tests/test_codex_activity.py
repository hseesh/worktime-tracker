"""Tests for the current Codex model: hooks select a project; visibility adds time."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import tracker.time_recorder as time_recorder_module
from tracker.codex_activity_manager import CodexActivityManager
from tracker.time_recorder import TimeRecorder


class TestCodexActivity(unittest.TestCase):

    def setUp(self):
        self._db_path = tempfile.mktemp(suffix=".db")
        config.DB_FILE = Path(self._db_path)
        time_recorder_module.DB_FILE = config.DB_FILE
        self.recorder = TimeRecorder()
        self.manager = CodexActivityManager(
            self.recorder,
            indie_keywords=["P1-c", "Assets"],
        )

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    @staticmethod
    def _now(offset_seconds=0):
        return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)

    def test_hook_selects_project_but_does_not_add_time(self):
        project = r"D:\Data\unity\P1-c\Assets"
        result = self.manager.handle_event(
            "SessionStart", "s1", project, self._now(-2)
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["added_seconds"], 0)
        self.assertEqual(self.recorder.get_codex_today_total(), 0)
        self.assertEqual(
            self.manager.get_current_active_project()["project_name"],
            "Assets (Indie)",
        )

    def test_visible_codex_time_is_recorded_explicitly(self):
        project = r"D:\Data\unity\P1-c\Assets"
        self.manager.handle_event("PreToolUse", "s1", project, self._now(-1))
        active = self.manager.get_current_active_project()

        self.recorder.add_codex_time(
            active["project"], active["project_name"], 12.5, "Indie"
        )

        summary = self.recorder.get_codex_today_summary()
        self.assertEqual(len(summary), 1)
        self.assertAlmostEqual(summary[0]["seconds"], 12.5)
        tag_totals = self.recorder.get_today_tag_distribution()
        self.assertEqual(tag_totals[0]["tag"], "Indie")
        self.assertAlmostEqual(tag_totals[0]["seconds"], 12.5)

    def test_duplicate_event_is_idempotent(self):
        project = r"D:\Projects\WorkApp"
        observed_at = self._now(-1)
        first = self.manager.handle_event("PreToolUse", "s1", project, observed_at)
        duplicate = self.manager.handle_event("PreToolUse", "s1", project, observed_at)

        self.assertEqual(first["status"], "ok")
        self.assertEqual(duplicate["status"], "duplicate")

    def test_stop_deactivates_single_codex_project(self):
        project = r"D:\Projects\WorkApp"
        self.manager.handle_event("SessionStart", "s1", project, self._now(-2))
        result = self.manager.handle_event("Stop", "s1", project, self._now(-1))

        self.assertEqual(result["status"], "stopped")
        self.assertIsNone(self.manager.get_current_active_project())

    def test_most_recent_project_is_current(self):
        work = r"D:\Projects\WorkApp"
        indie = r"D:\Data\unity\P1-c\Assets"
        self.manager.handle_event("PreToolUse", "work", work, self._now(-20))
        self.manager.handle_event("PreToolUse", "indie", indie, self._now(-5))

        current = self.manager.get_current_active_project()
        self.assertEqual(current["project"], indie)
        self.assertEqual(current["project_name"], "Assets (Indie)")

    def test_validation(self):
        ok, _, parsed = CodexActivityManager.validate_event(
            "PostToolUse", "s1", r"D:\Projects\X", "2026-07-20T10:00:00Z"
        )
        self.assertTrue(ok)
        self.assertIsNotNone(parsed)

        bad_event, _, _ = CodexActivityManager.validate_event(
            "Unknown", "s1", r"D:\Projects\X", "2026-07-20T10:00:00Z"
        )
        bad_path, _, _ = CodexActivityManager.validate_event(
            "PreToolUse", "s1", "relative/path", "2026-07-20T10:00:00Z"
        )
        self.assertFalse(bad_event)
        self.assertFalse(bad_path)


if __name__ == "__main__":
    unittest.main()
