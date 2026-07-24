"""Regression tests for dual-Devin identity and the canonical segment source."""

import csv
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import config
import tracker.time_recorder as time_recorder_module
from config import AppConfig
from tracker.time_recorder import TimeRecorder
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
