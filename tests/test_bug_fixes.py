"""Regression tests for fixed timing and event-validation edge cases."""

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tracker.time_recorder as time_recorder_module
from tracker.chrome_url_cache import ChromeUrlCache
from tracker.codex_activity_manager import CodexActivityManager
from tracker.time_recorder import TimeRecorder


class TestBugFixes(unittest.TestCase):

    def test_chrome_urls_are_kept_per_window_title(self):
        cache = ChromeUrlCache()
        cache.set_url("https://chatgpt.com/c/one", "ChatGPT")
        cache.set_url("https://github.com/org/repo", "Repository")

        self.assertEqual(
            cache.get_url("ChatGPT - Google Chrome", allow_active_fallback=False),
            "https://chatgpt.com/c/one",
        )
        self.assertEqual(
            cache.get_url("Repository - Google Chrome", allow_active_fallback=False),
            "https://github.com/org/repo",
        )
        self.assertEqual(
            cache.get_url("Unreported tab - Google Chrome", allow_active_fallback=False),
            "",
        )

    def test_codex_event_requires_timezone(self):
        valid, _, parsed = CodexActivityManager.validate_event(
            "PreToolUse", "session", r"D:\Project", "2026-07-27T10:00:00"
        )
        self.assertFalse(valid)
        self.assertIsNone(parsed)

    def test_cross_midnight_interval_is_split(self):
        start = datetime(2026, 7, 27, 23, 59, 58)
        end = datetime(2026, 7, 28, 0, 0, 2)
        chunks = list(TimeRecorder._split_interval(start, end))

        self.assertEqual(chunks, [
            (datetime(2026, 7, 27, 23, 59, 58), datetime(2026, 7, 28, 0, 0)),
            (datetime(2026, 7, 28, 0, 0), datetime(2026, 7, 28, 0, 0, 2)),
        ])

    def test_recorder_persists_each_cross_midnight_piece_on_its_own_day(self):
        fixed_now = datetime(2026, 7, 28, 0, 0, 2)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.replace(tzinfo=tz)

        original_db = time_recorder_module.DB_FILE
        with TemporaryDirectory() as temp_dir:
            time_recorder_module.DB_FILE = Path(temp_dir) / "worktime.db"
            try:
                recorder = TimeRecorder()
                with patch("tracker.time_recorder.datetime", FixedDateTime):
                    recorder.add_time("app.exe", "App", 4, tag="Work")

                conn = recorder._conn()
                try:
                    rows = conn.execute(
                        "SELECT date, seconds FROM time_segments ORDER BY start_time"
                    ).fetchall()
                finally:
                    conn.close()
            finally:
                time_recorder_module.DB_FILE = original_db

        self.assertEqual(
            [(row["date"], row["seconds"]) for row in rows],
            [("2026-07-27", 2.0), ("2026-07-28", 2.0)],
        )


if __name__ == "__main__":
    unittest.main()
