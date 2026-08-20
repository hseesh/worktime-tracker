"""API regression tests: dashboard, history and CSV use the same segments."""

import csv
import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

import config
import tracker.time_recorder as time_recorder_module
import web.server as web_server_module
from tracker.codex_activity_manager import CodexActivityManager
from tracker.time_recorder import TimeRecorder
from web.server import WebServer


class _FakeConfig:
    def __init__(self):
        self.processes = {}
        self.app_tag_overrides = {}

    def is_monitored(self, process_name):
        return process_name.lower() in {p.lower() for p in self.processes}

    def add_process(self, process_name, display_name):
        self.processes[process_name] = display_name

    def set_app_tag_override(self, process_name, project, display_name, tag):
        self.app_tag_overrides[(process_name.lower(), project, display_name)] = tag


class _StoppedEngine:
    _last_fg = None

    @staticmethod
    def is_running():
        return False

    @staticmethod
    def get_current_windows():
        return []

    @staticmethod
    def _is_codex_foreground():
        return False


class TestWebConsistency(unittest.TestCase):

    def setUp(self):
        self._db_path = tempfile.mktemp(suffix=".db")
        config.DB_FILE = Path(self._db_path)
        time_recorder_module.DB_FILE = config.DB_FILE
        web_server_module.DB_FILE = config.DB_FILE
        self.recorder = TimeRecorder()
        self.manager = CodexActivityManager(self.recorder)
        self.config = _FakeConfig()
        self.server = WebServer(
            self.config, self.recorder, _StoppedEngine(), self.manager
        )
        self.client = self.server._app.test_client()

    def tearDown(self):
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_dashboard_history_and_csv_totals_match(self):
        self.recorder.add_time(
            "Devin.exe", "Devin [zs-cloud]", 20, "zs-cloud", "Work"
        )
        self.recorder.add_time(
            "Devin.exe", "Devin [Assets]", 10, "Assets", "Indie"
        )
        self.recorder.add_time("cloudmusic.exe", "cloudmusic.exe", 5, "", "Fun")
        self.recorder.add_codex_time(
            r"D:\Data\unity\P1-c\Assets", "Assets (Indie)", 30, "Indie"
        )

        dashboard = self.client.get("/api/dashboard").get_json()
        history = self.client.get("/api/history?days=1").get_json()
        export_response = self.client.get("/api/export")

        self.assertEqual(dashboard["cards"]["total"], "1m 5s")
        self.assertEqual(history["cards"]["total"], "1m 5s")
        self.assertEqual(history["cards"]["work"], "20s")
        self.assertEqual(history["cards"]["indie"], "40s")

        rows = list(csv.DictReader(io.StringIO(export_response.data.decode("utf-8-sig"))))
        self.assertEqual(sum(float(r["Seconds"]) for r in rows), 65)
        self.assertEqual({r["Tag"] for r in rows}, {"Work", "Indie", "Fun"})
        self.assertIn("Assets (Indie)", {r["DisplayName"] for r in rows})

    def test_live_dashboard_cards_use_current_segment_totals(self):
        self.recorder.add_time("Devin.exe", "Devin", 12, tag="Indie")
        self.recorder.add_time("IDE.exe", "IDE", 8, tag="Work")
        self.recorder.add_focus_time("Indie", 7)
        self.recorder.add_focus_time("Work", 3)

        response = self.client.get("/api/dashboard/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        cards = response.get_json()["cards"]
        self.assertEqual(cards["total"], "20s")
        self.assertEqual(cards["indie"], "12s")
        self.assertEqual(cards["work"], "8s")
        self.assertEqual(cards["indie_pct"], 60)
        self.assertEqual(cards["indie_focus"], "7s")
        self.assertEqual(cards["work_focus"], "3s")

    def test_app_tag_quick_assign_updates_exact_project_immediately(self):
        self.recorder.add_time(
            "Devin.exe", "Devin [Assets]", 10, "Assets", "Indie"
        )
        self.recorder.add_time(
            "Devin.exe", "Devin [zs-cloud]", 20, "zs-cloud", "Work"
        )

        response = self.client.put(
            "/api/app-tag",
            json={
                "process_name": "Devin.exe",
                "display_name": "Devin [Assets]",
                "project": "Assets",
                "tag": "Other",
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = self.client.get("/api/dashboard").get_json()["app_breakdown"]
        by_name = {r["name"]: r["tag"] for r in rows}
        self.assertEqual(by_name["Devin [Assets]"], "Other")
        self.assertEqual(by_name["Devin [zs-cloud]"], "Work")
        self.assertEqual(
            self.config.app_tag_overrides[
                ("devin.exe", "Assets", "Devin [Assets]")
            ],
            "Other",
        )

    def test_heatmap_and_day_detail_use_segment_totals(self):
        self.recorder.add_time(
            "Devin.exe", "Devin [zs-cloud]", 20, "zs-cloud", "Work"
        )
        self.recorder.add_time(
            "Devin.exe", "Devin [Assets]", 10, "Assets", "Indie"
        )
        self.recorder.add_codex_time(
            r"D:\Data\unity\P1-c\Assets", "Assets (Indie)", 30, "Indie"
        )
        self.recorder.add_idle_time(5)

        today = date.today().isoformat()
        heatmap = self.client.get("/api/history/heatmap?days=365").get_json()
        day_cell = next(cell for cell in heatmap["days"] if cell["date"] == today)
        self.assertEqual(len(heatmap["days"]), 371)
        self.assertAlmostEqual(day_cell["total_seconds"], 60)
        self.assertAlmostEqual(day_cell["work_seconds"], 20)
        self.assertAlmostEqual(day_cell["indie_seconds"], 40)

        detail = self.client.get(f"/api/history/day?date={today}").get_json()
        self.assertEqual(detail["date"], today)
        self.assertAlmostEqual(detail["total_seconds"], 60)
        self.assertAlmostEqual(detail["idle_seconds"], 5)
        self.assertEqual({r["tag"] for r in detail["tags"]}, {"Work", "Indie"})
        self.assertIn("Assets (Indie)", {r["display_name"] for r in detail["apps"]})


if __name__ == "__main__":
    unittest.main()
