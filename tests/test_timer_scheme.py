"""Regression tests for dual-Devin identity and the canonical segment source."""

import csv
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import config
import tracker.time_recorder as time_recorder_module
from config import AppConfig
from tracker.time_recorder import TimeRecorder
from tracker.codex_activity_manager import CodexActivityManager
from tracker.tracking_engine import TrackingEngine
from tracker.window_tracker import ForegroundInfo
from tracker.project_parser import parse_project


class TestTimerScheme(unittest.TestCase):

    def setUp(self):
        self._db_path = tempfile.mktemp(suffix=".db")
        self._csv_path = tempfile.mktemp(suffix=".csv")
        self._config_dir = tempfile.TemporaryDirectory()
        self._original_config_dir = config.CONFIG_DIR
        self._original_config_file = config.CONFIG_FILE
        config.CONFIG_DIR = Path(self._config_dir.name)
        config.CONFIG_FILE = config.CONFIG_DIR / "config.json"
        config.DB_FILE = Path(self._db_path)
        time_recorder_module.DB_FILE = config.DB_FILE
        self.recorder = TimeRecorder()

    def tearDown(self):
        for path in (self._db_path, self._csv_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        config.CONFIG_DIR = self._original_config_dir
        config.CONFIG_FILE = self._original_config_file
        self._config_dir.cleanup()

    def test_devin_titles_produce_distinct_projects(self):
        self.assertEqual(
            parse_project("Devin.exe", "Assets - Devin - Scene.unity"),
            "Assets",
        )
        self.assertEqual(
            parse_project("Devin.exe", "zs-cloud — Devin — README.md"),
            "zs-cloud",
        )
        self.assertEqual(
            parse_project("Devin.exe", "Assets - Devin"),
            "Assets",
        )

    def test_two_devin_projects_and_codex_share_one_canonical_source(self):
        self.recorder.add_time(
            "Devin.exe", "Devin [Assets]", 10, "Assets", "Indie"
        )
        self.recorder.add_time(
            "Devin.exe", "Devin [zs-cloud]", 20, "zs-cloud", "Work"
        )
        self.recorder.add_codex_time(
            r"D:\Data\unity\P1-c\Assets", "Assets (Indie)", 30, "Indie"
        )
        self.recorder.add_idle_time(5)

        today = date.today()
        self.assertAlmostEqual(self.recorder.get_today_total(), 60)

        tags = self.recorder.get_daily_tag_breakdown(today, today)[today.isoformat()]
        self.assertAlmostEqual(tags["Indie"], 40)
        self.assertAlmostEqual(tags["Work"], 20)
        self.assertNotIn("Idle", tags)

        apps = self.recorder.get_range_app_breakdown(today, today)
        identities = {(r["display_name"], r["project"], r["tag"]) for r in apps}
        self.assertIn(("Devin [Assets]", "Assets", "Indie"), identities)
        self.assertIn(("Devin [zs-cloud]", "zs-cloud", "Work"), identities)
        self.assertIn(
            ("Assets (Indie)", r"D:\Data\unity\P1-c\Assets", "Indie"),
            identities,
        )

    def test_export_contains_tag_project_and_codex(self):
        project = r"D:\Data\unity\P1-c\Assets"
        self.recorder.add_time(
            "Devin.exe", "Devin [Assets]", 10, "Assets", "Indie"
        )
        self.recorder.add_codex_time(project, "Assets (Indie)", 20, "Indie")

        self.recorder.export_csv(self._csv_path)

        with open(self._csv_path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(
            list(rows[0]),
            ["Date", "DisplayName", "ProcessName", "Project", "Tag", "Seconds", "UpdatedAt"],
        )
        self.assertEqual({r["DisplayName"] for r in rows}, {"Devin [Assets]", "Assets (Indie)"})
        codex = next(r for r in rows if r["DisplayName"] == "Assets (Indie)")
        self.assertEqual(codex["Project"], project)
        self.assertEqual(codex["Tag"], "Indie")

    def test_codex_requires_http_hook_before_recording(self):
        project = r"D:\Data\unity\P1-c\Assets"
        manager = CodexActivityManager(self.recorder, indie_keywords=["P1-c"])
        engine = TrackingEngine(AppConfig(), self.recorder, manager)
        codex_window = ForegroundInfo(
            process_name="ChatGPT.exe",
            window_title="Codex",
            pid=1,
        )

        # A visible Codex window alone must not create a generic Codex record.
        engine._record_window(codex_window, 10)
        self.assertEqual(self.recorder.get_today_total(), 0)
        self.assertEqual(self.recorder.get_codex_today_total(), 0)

        # A valid local HTTP event activates the project and unlocks recording.
        manager.handle_event(
            "SessionStart", "session-1", project, datetime.now(timezone.utc)
        )
        engine._record_window(codex_window, 10)

        summary = self.recorder.get_codex_today_summary()
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["project"], project)
        self.assertAlmostEqual(summary[0]["seconds"], 10)

    def test_multiple_codex_sessions_share_one_sample_interval(self):
        project = r"D:\Data\unity\P1-c\Assets"
        manager = CodexActivityManager(self.recorder, indie_keywords=["P1-c"])
        manager.handle_event(
            "SessionStart", "session-1", project, datetime.now(timezone.utc)
        )
        engine = TrackingEngine(AppConfig(), self.recorder, manager)
        windows = [
            ForegroundInfo("ChatGPT.exe", "Codex session 1", pid=1, hwnd=101),
            ForegroundInfo("ChatGPT.exe", "Codex session 2", pid=2, hwnd=202),
        ]

        engine._record_selected_windows(windows, elapsed=10, focused_hwnd=101)

        summary = self.recorder.get_codex_today_summary()
        self.assertEqual(len(summary), 1)
        self.assertAlmostEqual(summary[0]["seconds"], 10)

    def test_visible_codex_window_is_recorded_without_keyboard_focus(self):
        project = r"D:\Data\unity\P1-c\Assets"
        manager = CodexActivityManager(self.recorder, indie_keywords=["P1-c"])
        manager.handle_event(
            "SessionStart", "session-1", project, datetime.now(timezone.utc)
        )
        engine = TrackingEngine(AppConfig(), self.recorder, manager)
        codex_window = ForegroundInfo(
            "ChatGPT.exe", "Codex background", pid=1, hwnd=202
        )

        engine._record_selected_windows([codex_window], elapsed=10, focused_hwnd=101)

        self.assertAlmostEqual(self.recorder.get_codex_today_total(), 10)

    def test_focus_time_only_uses_the_keyboard_focused_window(self):
        project = r"D:\Data\unity\P1-c\Assets"
        manager = CodexActivityManager(self.recorder, indie_keywords=["P1-c"])
        manager.handle_event(
            "SessionStart", "session-1", project, datetime.now(timezone.utc)
        )
        engine = TrackingEngine(AppConfig(), self.recorder, manager)
        codex_window = ForegroundInfo("ChatGPT.exe", "Codex", pid=1, hwnd=101)

        engine._record_selected_windows([codex_window], elapsed=10, focused_hwnd=999)
        engine._record_selected_windows([codex_window], elapsed=10, focused_hwnd=101)

        totals = self.recorder.get_today_live_totals()
        self.assertAlmostEqual(totals["indie_focus"], 10)
        self.assertAlmostEqual(totals["work_focus"], 0)

    def test_live_totals_exclude_idle_time(self):
        self.recorder.add_time("Devin.exe", "Devin", 12, tag="Indie")
        self.recorder.add_time("IDE.exe", "IDE", 8, tag="Work")
        self.recorder.add_focus_time("Indie", 7)
        self.recorder.add_focus_time("Work", 3)
        self.recorder.add_focus_time("Other", 99)
        self.recorder.add_idle_time(5)

        totals = self.recorder.get_today_live_totals()

        self.assertAlmostEqual(totals["total"], 20)
        self.assertAlmostEqual(totals["indie"], 12)
        self.assertAlmostEqual(totals["work"], 8)
        self.assertAlmostEqual(totals["indie_focus"], 7)
        self.assertAlmostEqual(totals["work_focus"], 3)

    def test_same_tag_windows_count_once_in_tag_totals_but_keep_app_rows(self):
        app_config = AppConfig()
        app_config.set_process_tag("Devin.exe", "Indie")
        app_config.set_process_tag("Unity.exe", "Indie")
        engine = TrackingEngine(app_config, self.recorder)
        windows = [
            ForegroundInfo("Devin.exe", "Devin - Assets", pid=1, hwnd=101),
            ForegroundInfo("Unity.exe", "Unity Editor", pid=2, hwnd=202),
        ]

        engine._record_selected_windows(windows, elapsed=10, focused_hwnd=101)

        totals = self.recorder.get_today_live_totals()
        self.assertAlmostEqual(totals["indie"], 10)
        self.assertAlmostEqual(totals["total"], 10)
        apps = self.recorder.get_today_app_breakdown()
        self.assertEqual(len(apps), 2)
        self.assertAlmostEqual(sum(row["seconds"] for row in apps), 20)

    def test_timeline_merges_overlapping_windows_with_the_same_tag(self):
        today = date.today().isoformat()
        conn = self.recorder._conn()
        try:
            conn.executemany(
                """
                INSERT INTO time_segments
                    (date, start_time, end_time, process_name, display_name, project, tag, seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (today, f"{today}T15:00:00", f"{today}T15:30:00", "Devin.exe", "Devin", "", "Indie", 1800),
                    (today, f"{today}T15:20:00", f"{today}T15:50:00", "codex.exe", "Codex", "", "Indie", 1800),
                    (today, f"{today}T15:50:00", f"{today}T16:00:00", "idle", "Idle", "", "Idle", 600),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        timeline = self.recorder.get_today_timeline()
        indie_at_15 = next(
            row for row in timeline if row["hour"] == 15 and row["tag"] == "Indie"
        )

        self.assertAlmostEqual(indie_at_15["seconds"], 3000)
        self.assertNotIn("Idle", {row["tag"] for row in timeline})

    def test_existing_segment_table_migrates_project_column(self):
        conn = sqlite3.connect(self._db_path)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(time_segments)")]
        conn.close()
        self.assertIn("project", columns)

    def test_app_override_beats_keyword_rule_on_next_sample(self):
        app_config = AppConfig()
        app_config.set_tag_keyword_rules(
            "Devin.exe", [{"keyword": "Assets", "tag": "Indie"}]
        )
        app_config.set_app_tag_override(
            "Devin.exe", "Assets", "Devin [Assets]", "Work"
        )
        engine = TrackingEngine(app_config, self.recorder)

        engine._record_window(
            ForegroundInfo(
                process_name="Devin.exe",
                window_title="Assets - Devin - Scene.unity",
                pid=1,
                project="Assets",
            ),
            1.0,
        )

        rows = self.recorder.get_today_app_breakdown()
        assets = next(r for r in rows if r["display_name"] == "Devin [Assets]")
        self.assertEqual(assets["tag"], "Work")
        reloaded = AppConfig()
        self.assertEqual(
            reloaded.get_app_tag_override(
                "Devin.exe", "Assets", "Devin [Assets]"
            ),
            "Work",
        )


if __name__ == "__main__":
    unittest.main()
