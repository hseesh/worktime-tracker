"""Flask web server: serves the Web UI and HTTP API for WorkTime Tracker.

Runs on 127.0.0.1:17891. Replaces the PySide6 desktop UI.
"""

import csv
import io
import logging
import threading
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, send_file, request, Response

from config import AppConfig, DB_FILE
from tracker.chrome_url_cache import ChromeUrlCache
from tracker.codex_activity_manager import CodexActivityManager
from tracker.time_recorder import TimeRecorder
from tracker.tracking_engine import TrackingEngine
from web.icon_extractor import get_icon_png

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 17891

WEB_DIR = Path(__file__).resolve().parent
INDEX_FILE = WEB_DIR / "index.html"


def _fmt_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


class WebServer:
    """Manages the Flask server lifecycle in a daemon thread."""

    def __init__(
        self,
        config: AppConfig,
        recorder: TimeRecorder,
        engine: TrackingEngine,
        codex_manager: CodexActivityManager,
        chrome_url_cache: ChromeUrlCache = None,
    ):
        self._config = config
        self._recorder = recorder
        self._engine = engine
        self._codex_manager = codex_manager
        self._chrome_url_cache = chrome_url_cache
        self._thread: threading.Thread | None = None
        # NOTE: Do NOT bulk-update time_records tags on startup.
        # With keyword rules in place, a process-level tag sync would overwrite
        # correctly keyword-tagged records (e.g. ChatGPT→Indie for msedge.exe).
        # Tags are assigned correctly at recording time.
        self._app = self._create_app()

    def _create_app(self) -> Flask:
        app = Flask(__name__, static_folder=str(WEB_DIR))

        # ---- Serve index.html ----
        @app.route("/")
        def index():
            return send_file(str(INDEX_FILE))

        # ---- API: App Icon ----
        @app.route("/api/icon/<path:process_name>")
        def api_icon(process_name):
            png = get_icon_png(process_name)
            if png:
                return Response(png, mimetype="image/png")
            return Response(status=404)

        # ---- API: Dashboard ----
        @app.route("/api/dashboard")
        def api_dashboard():
            codex_summary = self._recorder.get_codex_today_summary()

            # Tag-based classification from time_segments (includes both foreground AND codex hook segments)
            seg_tag_dist = self._recorder.get_today_tag_distribution()
            tag_totals = {}
            for r in seg_tag_dist:
                tag_totals[r["tag"]] = tag_totals.get(r["tag"], 0) + r["seconds"]

            total = sum(tag_totals.values())
            total_indie = tag_totals.get("Indie", 0)
            total_work = tag_totals.get("Work", 0)

            # Pie chart from time_segments only (codex time already included)
            seg_apps = self._recorder.get_today_app_breakdown()
            merged = {}
            for r in seg_apps:
                merged[r["display_name"]] = merged.get(r["display_name"], 0) + r["seconds"]

            pie_data = [
                {"name": name, "value": round(s, 1)}
                for name, s in sorted(merged.items(), key=lambda kv: -kv[1])
            ]

            # App breakdown (from time_segments, consistent with total)
            app_breakdown = []
            for r in seg_apps:
                pct = (r["seconds"] / total * 100) if total > 0 else 0
                app_breakdown.append({
                    "name": r["display_name"],
                    "process_name": r["process_name"],
                    "project": r.get("project", ""),
                    "tag": r.get("tag", "Other"),
                    "duration": _fmt_duration(r["seconds"]),
                    "percent": round(pct, 1),
                })

            # Tag distribution
            tag_dist = [
                {"tag": tag, "seconds": s, "duration": _fmt_duration(s),
                 "pct": round((s / total * 100) if total > 0 else 0, 1)}
                for tag, s in sorted(tag_totals.items(), key=lambda kv: -kv[1])
            ]

            # Timeline (by hour + tag)
            timeline_raw = self._recorder.get_today_timeline()
            timeline = [
                {"hour": r["hour"], "tag": r["tag"], "seconds": r["seconds"]}
                for r in timeline_raw
                if r["tag"] != "Idle"
            ]

            # Indie details (from time_segments for consistency, includes codex)
            indie_rows = []
            for r in seg_apps:
                if r.get("tag") == "Indie":
                    indie_rows.append({"source": "App", "name": r["display_name"], "seconds": r["seconds"]})
            indie_rows.sort(key=lambda x: -x["seconds"])
            for r in indie_rows:
                r["duration"] = _fmt_duration(r["seconds"])
                r["percent"] = round((r["seconds"] / total_indie * 100) if total_indie > 0 else 0, 1)

            # Codex activity
            # Codex desktop is single-instance. Hook history may mention several
            # projects, but only the most recently active project is current.
            codex_current_active = (
                self._codex_manager.get_current_active_project()
                if self._codex_manager else None
            )
            codex_table = []
            for r in codex_summary:
                status = "Active" if (
                    codex_current_active
                    and codex_current_active["project"] == r["project"]
                ) else ""
                codex_table.append({
                    "project": r["project_name"],
                    "duration": _fmt_duration(r["seconds"]),
                    "status": status,
                })

            # Current tracking: show every selected non-Codex window, one per
            # monitor, so two Devin projects are visible independently.
            current_windows = []
            seen_windows = set()
            codex_processes = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}
            if self._engine.is_running():
                for fg in self._engine.get_current_windows():
                    if fg.process_name.lower() in codex_processes and codex_current_active:
                        continue
                    dn = self._config.get_display_name(fg.process_name, fg.window_title)
                    if dn:
                        name = TrackingEngine._build_display_name(dn, fg.project)
                        # For Chrome, show short title only
                        if fg.process_name.lower() == "chrome.exe":
                            title = fg.window_title
                            if title.endswith(" - Google Chrome"):
                                title = title[:-len(" - Google Chrome")]
                            if len(title) > 30:
                                title = title[:30] + "…"
                            name = f"Chrome [{title}]" if title else "Chrome"
                            tag = self._engine._resolve_tag_with_url(fg) or "—"
                        else:
                            tag = self._config.resolve_app_tag(
                                fg.process_name, fg.window_title, fg.project, name
                            )
                    else:
                        name = f"Other ({fg.process_name})"
                        tag = "Other"
                    key = (name, tag, fg.monitor_index)
                    if key in seen_windows:
                        continue
                    seen_windows.add(key)
                    current_windows.append({
                        "name": name,
                        "tag": tag,
                        "monitor": fg.monitor_index + 1 if fg.monitor_index >= 0 else None,
                    })

            # Backwards-compatible focused-window fields.
            current_fg = ""
            current_fg_tag = ""
            if self._engine.is_running():
                if self._engine._last_fg:
                    dn = self._config.get_display_name(
                        self._engine._last_fg.process_name,
                        self._engine._last_fg.window_title,
                    )
                    if dn:
                        current_fg = dn
                        if self._engine._last_fg.project:
                            current_fg = TrackingEngine._build_display_name(dn, self._engine._last_fg.project)
                        current_fg_tag = self._config.resolve_tag(
                            self._engine._last_fg.process_name,
                            self._engine._last_fg.window_title,
                        )
                    else:
                        current_fg = f"Other ({self._engine._last_fg.process_name})"
                        current_fg_tag = "Other"

            # Single Codex instance — show only the most recently active project.
            current_codex = []
            if self._codex_manager and self._engine:
                fg_is_codex = self._engine._is_codex_foreground()
                if fg_is_codex:
                    active = self._codex_manager.get_current_active_project()
                    if active:
                        current_codex = [active["project_name"]]

            # Compat: merged string
            parts = [w["name"] for w in current_windows]
            if current_codex:
                parts.append("Codex: " + ", ".join(current_codex))
            current = " + ".join(parts) if parts else ""

            return jsonify({
                "cards": {
                    "total": _fmt_duration(total),
                    "indie": _fmt_duration(total_indie),
                    "indie_pct": round((total_indie / total * 100) if total > 0 else 0, 0),
                    "work": _fmt_duration(total_work),
                    "work_pct": round((total_work / total * 100) if total > 0 else 0, 0),
                    "current": current or "--",
                    "current_fg": current_fg,
                    "current_fg_tag": current_fg_tag,
                    "current_windows": current_windows,
                    "current_codex": current_codex,
                },
                "pie": pie_data,
                "app_breakdown": app_breakdown,
                "tag_distribution": tag_dist,
                "timeline": timeline,
                "indie_details": indie_rows,
                "codex_table": codex_table,
                "codex_active_count": 1 if codex_current_active else 0,
                "idle_time": _fmt_duration(self._recorder.get_today_idle_time()),
            })

        # ---- API: History ----
        @app.route("/api/history")
        def api_history():
            days_param = request.args.get("days", 7)
            is_all = days_param == "all"
            if is_all:
                days = 0  # placeholder, actual count computed later
            else:
                days = min(max(int(days_param), 1), 90)

            end = date.today()

            # Query date range
            if is_all:
                first_date = self._recorder.get_first_record_date()
                start = date.fromisoformat(first_date) if first_date else end
            else:
                start = end - timedelta(days=days - 1)

            actual_days = max(1, (end - start).days + 1)

            daily_totals = self._recorder.get_daily_totals(start, end)
            tag_daily = self._recorder.get_daily_tag_breakdown(start, end)
            range_apps = self._recorder.get_range_app_breakdown(start, end)

            total_indie = sum(tags.get("Indie", 0) for tags in tag_daily.values())
            total_work = sum(tags.get("Work", 0) for tags in tag_daily.values())
            grand_total = sum(sum(tags.values()) for tags in tag_daily.values())

            # Total contains every non-idle tag. Work and Indie remain explicit
            # instead of silently folding Other/custom tags into Work.
            date_map_total = {d: s for d, s in daily_totals}
            trend = []
            d = start
            while d <= end:
                d_iso = d.isoformat()
                day_tags = tag_daily.get(d_iso, {})
                day_indie = day_tags.get("Indie", 0)
                day_work = day_tags.get("Work", 0)
                day_total = date_map_total.get(d_iso, 0)
                trend.append({
                    "date": d_iso[5:],
                    "hours": round(day_total / 3600, 2),
                    "work_hours": round(day_work / 3600, 2),
                    "indie_hours": round(day_indie / 3600, 2),
                })
                d += timedelta(days=1)

            days_with_data = len([1 for t in trend if t["hours"] > 0])
            avg_divisor = max(1, days_with_data) if is_all else actual_days

            per_app = []
            for r in range_apps:
                total_s = r["seconds"]
                per_app.append({
                    "name": r["display_name"],
                    "process_name": r["process_name"],
                    "project": r.get("project", ""),
                    "tag": r.get("tag", "Other"),
                    "total": _fmt_duration(total_s),
                    "avg": _fmt_duration(total_s / actual_days),
                    "days_active": r.get("days_active", 1),
                    "pct": round((total_s / grand_total * 100) if grand_total > 0 else 0, 1),
                })

            # Tag trend (per-tag daily breakdown for dynamic trend lines)
            tag_trend_raw = self._recorder.get_daily_tag_trend(start, end)
            tag_trend = [
                {"date": r["date"], "tag": r["tag"], "seconds": r["seconds"]}
                for r in tag_trend_raw
            ]

            # Period tag summary (weekly/monthly)
            period_summary = self._recorder.get_period_tag_summary(start, end, "week")

            return jsonify({
                "cards": {
                    "total": _fmt_duration(grand_total),
                    "indie": _fmt_duration(total_indie),
                    "indie_pct": round((total_indie / grand_total * 100) if grand_total > 0 else 0, 0),
                    "work": _fmt_duration(total_work),
                    "work_pct": round((total_work / grand_total * 100) if grand_total > 0 else 0, 0),
                    "avg": _fmt_duration(grand_total / avg_divisor),
                },
                "trend": trend,
                "per_app": per_app,
                "tag_trend": tag_trend,
                "period_summary": period_summary,
            })

        # ---- API: Events ----
        @app.route("/api/events")
        def api_events():
            filter_type = request.args.get("filter", "all")
            limit = request.args.get("limit", 200, type=int)
            rows = []

            import sqlite3
            conn = sqlite3.connect(str(DB_FILE))
            conn.row_factory = sqlite3.Row

            if filter_type in ("all", "codex"):
                codex_rows = conn.execute(
                    "SELECT event, session_id, project, observed_at, received_at FROM codex_events ORDER BY received_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                for r in codex_rows:
                    rows.append({
                        "time": r["received_at"],
                        "source": "Codex",
                        "action": r["event"],
                        "session": r["session_id"],
                        "target": r["project"],
                        "detail": f"observed: {r['observed_at']}",
                    })

            if filter_type in ("all", "foreground"):
                fg_rows = conn.execute(
                    "SELECT date, display_name, process_name, project, seconds, updated_at FROM time_records ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                for r in fg_rows:
                    rows.append({
                        "time": r["updated_at"],
                        "source": "Foreground",
                        "action": "time_added",
                        "session": r["project"] or "",
                        "target": f"{r['display_name']} ({r['process_name']})",
                        "detail": f"+{r['seconds']:.0f}s",
                    })

            if filter_type in ("all", "chrome"):
                chrome_rows = conn.execute(
                    "SELECT url, domain, received_at FROM chrome_url_events ORDER BY received_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                for r in chrome_rows:
                    rows.append({
                        "time": r["received_at"],
                        "source": "Chrome",
                        "action": "url_reported",
                        "session": "",
                        "target": r["domain"] or r["url"],
                        "detail": r["url"],
                    })

            conn.close()
            rows.sort(key=lambda x: x["time"], reverse=True)
            rows = rows[:limit]
            return jsonify(rows)

        # ---- API: Efficiency ----
        @app.route("/api/efficiency")
        def api_efficiency():
            days_param = request.args.get("days", 7, type=int)
            end = date.today()
            start = end - timedelta(days=days_param - 1)

            focus_sessions = self._recorder.get_focus_sessions()
            for s in focus_sessions:
                s["duration"] = _fmt_duration(s["seconds"])

            switch_freq = self._recorder.get_switch_frequency()

            peak_hours = self._recorder.get_peak_hours(start, end)

            idle_time = self._recorder.get_today_idle_time()

            return jsonify({
                "focus_sessions": focus_sessions,
                "switch_frequency": switch_freq,
                "peak_hours": peak_hours,
                "idle_time": _fmt_duration(idle_time),
                "idle_seconds": round(idle_time, 1),
            })

        # ---- API: Settings ----
        @app.route("/api/settings")
        def api_settings():
            return jsonify({
                "processes": self._config.processes,
                "idle_threshold": self._config.idle_threshold,
                "poll_interval": self._config.poll_interval,
                "auto_start_minimized": self._config.auto_start_minimized,
                "auto_start_with_windows": self._config.auto_start_with_windows,
                "indie_keywords": self._config.indie_keywords,
                "work_keywords": self._config._work_keywords,
                "process_tags": self._config.process_tags,
                "tag_keyword_rules": self._config.tag_keyword_rules,
                "app_tag_overrides": self._config.app_tag_overrides,
                "url_tag_rules": self._config.url_tag_rules,
                "tags": self._recorder.list_tags(),
            })

        @app.route("/api/settings", methods=["PUT"])
        def api_update_settings():
            data = request.get_json()
            if "idle_threshold" in data:
                self._config.idle_threshold = int(data["idle_threshold"])
            if "auto_start_minimized" in data:
                self._config.auto_start_minimized = bool(data["auto_start_minimized"])
            if "auto_start_with_windows" in data:
                self._config.auto_start_with_windows = bool(data["auto_start_with_windows"])
            if "indie_keywords" in data:
                for proc, kws in data["indie_keywords"].items():
                    self._config.set_indie_keywords(proc, kws)
            if "add_process" in data:
                p = data["add_process"]
                self._config.add_process(p["process_name"], p["display_name"])
            if "remove_process" in data:
                self._config.remove_process(data["remove_process"])
            if "process_tags" in data:
                for proc, tag in data["process_tags"].items():
                    self._config.set_process_tag(proc, tag)
            if "tag_keyword_rules" in data:
                for proc, rules in data["tag_keyword_rules"].items():
                    self._config.set_tag_keyword_rules(proc, rules)
            if "url_tag_rules" in data:
                for proc, rules in data["url_tag_rules"].items():
                    self._config.set_url_tag_rules(proc, rules)
            self._config.save()
            return jsonify({"status": "ok"})

        # ---- API: Tags CRUD ----
        @app.route("/api/tags")
        def api_list_tags():
            return jsonify(self._recorder.list_tags())

        @app.route("/api/tags", methods=["POST"])
        def api_add_tag():
            data = request.get_json()
            name = data.get("name", "").strip()
            color = data.get("color", "#565f89")
            if not name:
                return jsonify({"error": "name required"}), 400
            try:
                tag = self._recorder.add_tag(name, color)
                return jsonify(tag)
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route("/api/tags/<int:tag_id>", methods=["PUT"])
        def api_update_tag(tag_id):
            data = request.get_json()
            old_tag = next((t for t in self._recorder.list_tags() if t["id"] == tag_id), None)
            ok = self._recorder.update_tag(tag_id, data.get("name"), data.get("color"))
            if ok:
                new_name = data.get("name")
                if old_tag and new_name and new_name != old_tag["name"]:
                    self._config.replace_tag_references(old_tag["name"], new_name)
                return jsonify({"status": "ok"})
            return jsonify({"error": "not found or system tag"}), 400

        @app.route("/api/tags/<int:tag_id>", methods=["DELETE"])
        def api_delete_tag(tag_id):
            old_tag = next((t for t in self._recorder.list_tags() if t["id"] == tag_id), None)
            ok = self._recorder.delete_tag(tag_id)
            if ok:
                if old_tag:
                    self._config.replace_tag_references(old_tag["name"], "Other")
                return jsonify({"status": "ok"})
            return jsonify({"error": "not found or system tag"}), 400

        # ---- API: App Tag (quick assign from App Breakdown) ----
        @app.route("/api/app-tag", methods=["PUT"])
        def api_set_app_tag():
            data = request.get_json()
            proc = data.get("process_name", "").strip()
            display_name = data.get("display_name", "").strip()
            project = data.get("project", "").strip()
            tag = data.get("tag", "Other").strip()
            if not proc or not display_name:
                return jsonify({"error": "process_name and display_name required"}), 400
            valid_tags = {item["name"] for item in self._recorder.list_tags()}
            if tag not in valid_tags:
                return jsonify({"error": f"unknown tag: {tag}"}), 400
            # Add process to monitored list if not already there
            if not self._config.is_monitored(proc):
                self._config.add_process(proc, display_name or proc)
            # Persist an exact app/project override. It has higher priority than
            # keyword and process rules, so the next poll cannot revert it.
            self._config.set_app_tag_override(proc, project, display_name, tag)
            try:
                TimeRecorder.update_app_tag(proc, display_name, project, tag)
            except Exception as e:
                logger.warning("Failed to update app tag history: %s", e)
                return jsonify({"error": str(e)}), 500
            return jsonify({"status": "ok"})

        # ---- API: Tag Rules ----
        @app.route("/api/tag-rules")
        def api_get_tag_rules():
            return jsonify({
                "process_tags": self._config.process_tags,
                "tag_keyword_rules": self._config.tag_keyword_rules,
            })

        @app.route("/api/tag-rules", methods=["PUT"])
        def api_set_tag_rules():
            data = request.get_json()
            if "process_tags" in data:
                for proc, tag in data["process_tags"].items():
                    self._config.set_process_tag(proc, tag)
            if "tag_keyword_rules" in data:
                for proc, rules in data["tag_keyword_rules"].items():
                    self._config.set_tag_keyword_rules(proc, rules)
            return jsonify({"status": "ok"})

        # ---- API: Status ----
        @app.route("/api/status")
        def api_status():
            return jsonify({
                "running": self._engine.is_running(),
                "current": "",
                "idle": False,
            })

        # ---- API: Toggle tracking ----
        @app.route("/api/toggle", methods=["POST"])
        def api_toggle():
            if self._engine.is_running():
                self._engine.stop()
                return jsonify({"running": False})
            else:
                self._engine.start()
                return jsonify({"running": True})

        # ---- API: Export CSV ----
        @app.route("/api/export")
        def api_export():
            start_str = request.args.get("start")
            end_str = request.args.get("end")
            start = date.fromisoformat(start_str) if start_str else None
            end = date.fromisoformat(end_str) if end_str else None

            buf = io.StringIO()
            buf.write("\ufeff")  # BOM for Excel
            writer = csv.writer(buf)
            writer.writerow(["Date", "DisplayName", "ProcessName", "Project", "Tag", "Seconds", "UpdatedAt"])

            import sqlite3
            conn = sqlite3.connect(str(DB_FILE))
            conn.row_factory = sqlite3.Row
            if start and end:
                rows = conn.execute(
                    """
                    SELECT date, display_name, process_name, project, tag,
                           SUM(seconds) AS seconds, MAX(end_time) AS updated_at
                    FROM time_segments
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
                           SUM(seconds) AS seconds, MAX(end_time) AS updated_at
                    FROM time_segments
                    WHERE tag != 'Idle'
                    GROUP BY date, display_name, process_name, project, tag
                    ORDER BY date, seconds DESC
                    """
                ).fetchall()
            conn.close()

            for r in rows:
                writer.writerow([
                    r["date"], r["display_name"], r["process_name"], r["project"],
                    r["tag"], r["seconds"], r["updated_at"],
                ])

            buf.seek(0)
            return Response(
                buf.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename=worktime_{start_str or 'all'}_{end_str or 'all'}.csv"},
            )

        # ---- API: Chrome URL (from browser extension) ----
        @app.route("/api/chrome-url", methods=["POST"])
        def api_chrome_url():
            if self._chrome_url_cache is None:
                logger.warning("POST /api/chrome-url received but cache not enabled")
                return jsonify({"error": "Chrome URL cache not enabled"}), 503
            data = request.get_json(silent=True)
            if not data:
                logger.warning("POST /api/chrome-url invalid JSON from %s", request.remote_addr)
                return jsonify({"error": "Invalid JSON"}), 400
            url = data.get("url", "")
            if not url:
                logger.warning("POST /api/chrome-url missing url field")
                return jsonify({"error": "url required"}), 400
            self._chrome_url_cache.set_url(url)
            logger.info("POST /api/chrome-url url=%s from=%s", url, request.remote_addr)
            return jsonify({"status": "ok"})

        @app.route("/api/chrome-url", methods=["GET"])
        def api_chrome_url_get():
            if self._chrome_url_cache is None:
                return jsonify({"error": "Chrome URL cache not enabled"}), 503
            return jsonify({"url": self._chrome_url_cache.get_url()})

        return app

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=lambda: self._app.run(host=HOST, port=PORT, debug=False, use_reloader=False),
            name="WebServer",
            daemon=True,
        )
        self._thread.start()
        logger.info("Web server listening on http://%s:%d", HOST, PORT)

    def stop(self):
        # Flask dev server doesn't have a clean shutdown API;
        # it's a daemon thread so it'll die with the process.
        self._thread = None
        logger.info("Web server stopped.")

    def open_browser(self):
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}")
