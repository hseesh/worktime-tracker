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
    ):
        self._config = config
        self._recorder = recorder
        self._engine = engine
        self._codex_manager = codex_manager
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
            summary = self._recorder.get_today_summary()
            codex_summary = self._recorder.get_codex_today_summary()

            # Tag-based classification from time_segments (includes both foreground AND codex hook segments)
            seg_tag_dist = self._recorder.get_today_tag_distribution()
            tag_totals = {}
            for r in seg_tag_dist:
                tag_totals[r["tag"]] = tag_totals.get(r["tag"], 0) + r["seconds"]

            # Codex summary for display table only (not added to totals — already in time_segments)
            codex_by_tag = {}
            for r in codex_summary:
                pn = r["project_name"]
                if "(Indie)" in pn:
                    tag = "Indie"
                elif "(Work)" in pn:
                    tag = "Work"
                else:
                    tag = "Work"
                codex_by_tag[tag] = codex_by_tag.get(tag, 0) + r["seconds"]

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
            codex_active = self._codex_manager.get_active_projects() if self._codex_manager else []
            codex_table = []
            for r in codex_summary:
                status = ""
                for ap in codex_active:
                    if ap["project"] == r["project"]:
                        status = "Active" if ap["active"] else f"Idle {ap['idle_seconds'] // 60}m"
                        break
                codex_table.append({
                    "project": r["project_name"],
                    "duration": _fmt_duration(r["seconds"]),
                    "status": status,
                })

            # Current tracking (foreground + parallel Codex hook)
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

            # Parallel Codex hook activity — only show if Codex is in foreground
            current_codex = []
            if self._codex_manager and self._engine:
                fg_is_codex = self._engine._is_codex_foreground()
                if fg_is_codex:
                    active = self._codex_manager.get_active_projects()
                    codex_active = [p for p in active if p["active"]]
                    current_codex = list(dict.fromkeys(p["project_name"] for p in codex_active))

            # Compat: merged string
            parts = []
            if current_fg:
                parts.append(current_fg)
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
                    "current_codex": current_codex,
                },
                "pie": pie_data,
                "app_breakdown": app_breakdown,
                "tag_distribution": tag_dist,
                "timeline": timeline,
                "indie_details": indie_rows,
                "codex_table": codex_table,
                "codex_active_count": sum(1 for p in codex_active if p["active"]),
                "idle_time": _fmt_duration(self._recorder.get_today_idle_time()),
            })

        # ---- API: History ----
        @app.route("/api/history")
        def api_history():
            import sqlite3

            days_param = request.args.get("days", 7)
            is_all = days_param == "all"
            if is_all:
                days = 0  # placeholder, actual count computed later
            else:
                days = min(max(int(days_param), 1), 90)

            end = date.today()

            # Query date range
            if is_all:
                conn = sqlite3.connect(str(DB_FILE))
                conn.row_factory = sqlite3.Row
                min_row = conn.execute("SELECT MIN(date) AS d FROM time_records").fetchone()
                min_row2 = conn.execute("SELECT MIN(date) AS d FROM codex_time_records").fetchone()
                conn.close()
                dates_avail = [r["d"] for r in [min_row, min_row2] if r and r["d"]]
                if dates_avail:
                    start = date.fromisoformat(min(dates_avail))
                else:
                    start = end
            else:
                start = end - timedelta(days=days - 1)

            actual_days = max(1, (end - start).days + 1)

            daily_totals = self._recorder.get_daily_totals(start, end)
            range_data = self._recorder.get_range_summary(start, end)

            # Codex range breakdown + daily codex breakdown
            codex_rows = []
            codex_daily = []  # [(date_iso, indie_seconds, work_seconds)]
            try:
                conn = sqlite3.connect(str(DB_FILE))
                conn.row_factory = sqlite3.Row
                codex_rows = conn.execute(
                    "SELECT project_name, SUM(seconds) AS seconds FROM codex_time_records WHERE date >= ? AND date <= ? GROUP BY project_name",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                codex_daily = conn.execute(
                    """SELECT date,
                              SUM(CASE WHEN project_name LIKE '%(Indie)' THEN seconds ELSE 0 END) AS indie,
                              SUM(CASE WHEN project_name NOT LIKE '%(Indie)' THEN seconds ELSE 0 END) AS work
                       FROM codex_time_records WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date""",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                conn.close()
            except Exception:
                pass

            # Foreground daily tag breakdown (tag-based, not suffix-based)
            fg_tag_daily = self._recorder.get_daily_tag_breakdown(start, end)
            fg_daily = {}  # {date_iso: [indie_s, work_s]}
            for d_iso, tag_map in fg_tag_daily.items():
                indie_s = tag_map.get("Indie", 0)
                work_s = tag_map.get("Work", 0)
                other_s = sum(s for t, s in tag_map.items() if t not in ("Indie", "Work"))
                fg_daily[d_iso] = [indie_s, work_s + other_s]

            codex_daily_map = {r["date"]: (r["indie"] or 0, r["work"] or 0) for r in codex_daily}

            fg_indie = sum(v[0] for v in fg_daily.values())
            fg_work = sum(v[1] for v in fg_daily.values())
            codex_indie = sum(r["seconds"] for r in codex_rows if "(Indie)" in r["project_name"])
            codex_work = sum(r["seconds"] for r in codex_rows if "(Indie)" not in r["project_name"])

            total_indie = fg_indie + codex_indie
            total_work = fg_work + codex_work
            grand_total = total_indie + total_work

            # Daily trend with merged fg + codex, split by work/indie
            date_map_total = {d: s for d, s in daily_totals}
            trend = []
            d = start
            while d <= end:
                d_iso = d.isoformat()
                fg_i, fg_w = fg_daily.get(d_iso, (0.0, 0.0))
                cx_i, cx_w = codex_daily_map.get(d_iso, (0.0, 0.0))
                day_indie = fg_i + cx_i
                day_work = fg_w + cx_w
                day_total = day_indie + day_work
                trend.append({
                    "date": d_iso[5:],
                    "hours": round(day_total / 3600, 2),
                    "work_hours": round(day_work / 3600, 2),
                    "indie_hours": round(day_indie / 3600, 2),
                })
                d += timedelta(days=1)

            days_with_data = len([1 for t in trend if t["hours"] > 0])
            avg_divisor = max(1, days_with_data) if is_all else actual_days

            # Per-app summary
            all_apps = {}
            for name, recs in range_data.items():
                all_apps[name] = sum(s for _, s in recs)
            for r in codex_rows:
                pn = r["project_name"]
                all_apps[pn] = all_apps.get(pn, 0) + r["seconds"]

            # Build display_name -> (process_name, tag) map for icon lookup and tag display
            name_to_proc = {}
            name_to_tag = {}
            try:
                conn = sqlite3.connect(str(DB_FILE))
                conn.row_factory = sqlite3.Row
                proc_rows = conn.execute(
                    "SELECT DISTINCT display_name, process_name, tag FROM time_records WHERE date >= ? AND date <= ?",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
                conn.close()
                for pr in proc_rows:
                    name_to_proc[pr["display_name"]] = pr["process_name"]
                    name_to_tag[pr["display_name"]] = pr["tag"]
            except Exception:
                pass

            per_app = []
            for name, total_s in sorted(all_apps.items(), key=lambda kv: -kv[1]):
                per_app.append({
                    "name": name,
                    "process_name": name_to_proc.get(name, ""),
                    "tag": name_to_tag.get(name, "Other"),
                    "total": _fmt_duration(total_s),
                    "avg": _fmt_duration(total_s / actual_days),
                    "days_active": max(1, days_with_data),
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
            ok = self._recorder.update_tag(tag_id, data.get("name"), data.get("color"))
            if ok:
                return jsonify({"status": "ok"})
            return jsonify({"error": "not found or system tag"}), 400

        @app.route("/api/tags/<int:tag_id>", methods=["DELETE"])
        def api_delete_tag(tag_id):
            ok = self._recorder.delete_tag(tag_id)
            if ok:
                return jsonify({"status": "ok"})
            return jsonify({"error": "not found or system tag"}), 400

        # ---- API: App Tag (quick assign from App Breakdown) ----
        @app.route("/api/app-tag", methods=["PUT"])
        def api_set_app_tag():
            data = request.get_json()
            proc = data.get("process_name", "").strip()
            display_name = data.get("display_name", "").strip()
            tag = data.get("tag", "Other").strip()
            if not proc:
                return jsonify({"error": "process_name required"}), 400
            # Add process to monitored list if not already there
            if proc not in self._config.processes:
                self._config.add_process(proc, display_name or proc)
            # Set process tag
            old_tag = self._config.process_tags.get(proc, "Other")
            self._config.set_process_tag(proc, tag)
            self._config.save()
            # Update historical time_records: only change records with the OLD tag,
            # preserving records that were tagged differently via keyword rules
            if old_tag != tag:
                try:
                    TimeRecorder.update_process_tag_selective(proc, old_tag, tag)
                except Exception as e:
                    logger.warning(f"Failed to update historical tags: {e}")
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
            writer.writerow(["Date", "DisplayName", "ProcessName", "Project", "Seconds", "UpdatedAt"])

            import sqlite3
            conn = sqlite3.connect(str(DB_FILE))
            conn.row_factory = sqlite3.Row
            if start and end:
                rows = conn.execute(
                    "SELECT date, display_name, process_name, project, seconds, updated_at FROM time_records WHERE date >= ? AND date <= ? ORDER BY date, seconds DESC",
                    (start.isoformat(), end.isoformat()),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT date, display_name, process_name, project, seconds, updated_at FROM time_records ORDER BY date, seconds DESC"
                ).fetchall()
            conn.close()

            for r in rows:
                writer.writerow([r["date"], r["display_name"], r["process_name"], r["project"], r["seconds"], r["updated_at"]])

            buf.seek(0)
            return Response(
                buf.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename=worktime_{start_str or 'all'}_{end_str or 'all'}.csv"},
            )

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
