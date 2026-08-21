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
from werkzeug.serving import make_server

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
        self._http_server = None
        # NOTE: Do NOT bulk-update time_records tags on startup.
        # With keyword rules in place, a process-level tag sync would overwrite
        # correctly keyword-tagged records (e.g. ChatGPT→Indie for msedge.exe).
        # Tags are assigned correctly at recording time.
        self._app = self._create_app()

    def _get_live_dashboard_cards(self):
        """Build the fast-changing dashboard cards without loading chart data."""
        totals = self._recorder.get_today_live_totals()
        total = totals["total"]
        total_indie = totals["indie"]
        total_work = totals["work"]
        indie_focus = totals["indie_focus"]
        work_focus = totals["work_focus"]

        codex_current_active = (
            self._codex_manager.get_current_active_project()
            if self._codex_manager else None
        )
        current_windows = []
        seen_windows = set()
        codex_processes = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}
        selected_windows = []
        if self._engine.is_running():
            selected_windows = self._engine.get_current_windows()
            for fg in selected_windows:
                # A Codex window without an active HTTP hook is not tracked.
                if fg.process_name.lower() in codex_processes:
                    continue
                dn = self._config.get_display_name(fg.process_name, fg.window_title)
                if dn:
                    name = TrackingEngine._build_display_name(dn, fg.project)
                    if fg.process_name.lower() == "chrome.exe":
                        title = fg.window_title.removesuffix(" - Google Chrome")
                        if len(title) > 30:
                            title = title[:30] + "..."
                        name = f"Chrome [{title}]" if title else "Chrome"
                        tag = self._engine._resolve_tag_with_url(fg) or "Other"
                    else:
                        tag = self._config.resolve_app_tag(
                            fg.process_name, fg.window_title, fg.project, name
                        )
                else:
                    name = f"Other ({fg.process_name})"
                    tag = "Other"
                key = (name, tag, fg.monitor_index)
                if key not in seen_windows:
                    seen_windows.add(key)
                    current_windows.append({
                        "name": name,
                        "tag": tag,
                        "monitor": fg.monitor_index + 1 if fg.monitor_index >= 0 else None,
                    })

        current_codex = []
        if codex_current_active and self._engine._is_codex_foreground():
            current_codex = [codex_current_active["project_name"]]

        parts = [w["name"] for w in current_windows]
        if current_codex:
            parts.append("Codex: " + ", ".join(current_codex))
        current = " + ".join(parts) if parts else ""
        return {
            "total": _fmt_duration(total),
            "indie": _fmt_duration(total_indie),
            "indie_pct": round((total_indie / total * 100) if total > 0 else 0, 0),
            "indie_focus": _fmt_duration(indie_focus),
            "work": _fmt_duration(total_work),
            "work_pct": round((total_work / total * 100) if total > 0 else 0, 0),
            "work_focus": _fmt_duration(work_focus),
            "current": current or "--",
            "current_windows": current_windows,
            "current_codex": current_codex,
        }

    def _create_app(self) -> Flask:
        app = Flask(__name__, static_folder=str(WEB_DIR))

        # ---- Serve index.html ----
        @app.route("/")
        def index():
            response = send_file(str(INDEX_FILE), max_age=0)
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            return response

        # ---- API: App Icon ----
        @app.route("/api/icon/<path:process_name>")
        def api_icon(process_name):
            png = get_icon_png(process_name)
            if png:
                return Response(png, mimetype="image/png")
            return Response(status=404)

        # ---- API: Dashboard ----
        @app.route("/api/dashboard/live")
        def api_dashboard_live():
            response = jsonify({"cards": self._get_live_dashboard_cards()})
            response.headers["Cache-Control"] = "no-store"
            return response

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
                    if fg.process_name.lower() in codex_processes:
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
        @app.route("/api/history/summary")
        def api_history_summary():
            """Return the lightweight History cards/trend before full details."""
            days_param = request.args.get("days", 7)
            is_all = days_param == "all"
            if is_all:
                days = 0
            else:
                try:
                    days = min(max(int(days_param), 1), 90)
                except (TypeError, ValueError):
                    return jsonify({"error": "days must be an integer or 'all'"}), 400

            end = date.today()
            if is_all:
                first_date = self._recorder.get_first_record_date()
                start = date.fromisoformat(first_date) if first_date else end
            else:
                start = end - timedelta(days=days - 1)

            actual_days = max(1, (end - start).days + 1)
            tag_daily = self._recorder.get_daily_tag_breakdown(start, end)
            total_indie = sum(tags.get("Indie", 0) for tags in tag_daily.values())
            total_work = sum(tags.get("Work", 0) for tags in tag_daily.values())
            grand_total = sum(sum(tags.values()) for tags in tag_daily.values())
            trend = []
            cursor = start
            while cursor <= end:
                d_iso = cursor.isoformat()
                tags = tag_daily.get(d_iso, {})
                total = sum(tags.values())
                trend.append({
                    "date": d_iso[5:],
                    "hours": round(total / 3600, 2),
                    "work_hours": round(tags.get("Work", 0) / 3600, 2),
                    "indie_hours": round(tags.get("Indie", 0) / 3600, 2),
                })
                cursor += timedelta(days=1)
            days_with_data = sum(1 for row in trend if row["hours"] > 0)
            avg_divisor = max(1, days_with_data) if is_all else actual_days
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
            })

        @app.route("/api/history")
        def api_history():
            days_param = request.args.get("days", 7)
            is_all = days_param == "all"
            if is_all:
                days = 0  # placeholder, actual count computed later
            else:
                try:
                    days = min(max(int(days_param), 1), 90)
                except (TypeError, ValueError):
                    return jsonify({"error": "days must be an integer or 'all'"}), 400

            end = date.today()

            # Query date range
            if is_all:
                first_date = self._recorder.get_first_record_date()
                start = date.fromisoformat(first_date) if first_date else end
            else:
                start = end - timedelta(days=days - 1)

            actual_days = max(1, (end - start).days + 1)

            tag_daily = self._recorder.get_daily_tag_breakdown(start, end)
            range_apps = self._recorder.get_range_app_breakdown(start, end)

            total_indie = sum(tags.get("Indie", 0) for tags in tag_daily.values())
            total_work = sum(tags.get("Work", 0) for tags in tag_daily.values())
            grand_total = sum(sum(tags.values()) for tags in tag_daily.values())

            # Total contains every non-idle tag. Work and Indie remain explicit
            # instead of silently folding Other/custom tags into Work.
            # The tag aggregate already contains every non-idle segment. Reuse
            # it for totals and charts instead of scanning time_segments again.
            date_map_total = {
                d: sum(tags.values()) for d, tags in tag_daily.items()
            }
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

            # Tag trend and weekly summary both derive from the same daily
            # aggregate above; avoid two more identical database scans.
            tag_trend = [
                {"date": d, "tag": tag, "seconds": seconds}
                for d in sorted(tag_daily)
                for tag, seconds in sorted(tag_daily[d].items())
            ]
            period_tag = {}
            for d, tags in tag_daily.items():
                iso = date.fromisoformat(d).isocalendar()
                period = f"{iso[0]}-W{iso[1]:02d}"
                for tag, seconds in tags.items():
                    key = (period, tag)
                    period_tag[key] = period_tag.get(key, 0) + seconds
            period_summary = [
                {"period": period, "tag": tag, "seconds": round(seconds, 1)}
                for (period, tag), seconds in sorted(period_tag.items())
            ]

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

        # ---- API: History heatmap -------------------------------------
        @app.route("/api/history/heatmap")
        def api_history_heatmap():
            days = request.args.get("days", 365, type=int)
            days = min(max(days or 365, 1), 730)
            end = date.today()
            start = end - timedelta(days=days - 1)
            daily_tags = self._recorder.get_daily_tag_breakdown(start, end)

            # Pad to whole Monday-Sunday weeks so the frontend can render a
            # GitHub-style grid without special cases at either edge.
            grid_start = start - timedelta(days=start.weekday())
            grid_end = end + timedelta(days=6 - end.weekday())
            cells = []
            cursor = grid_start
            while cursor <= grid_end:
                iso = cursor.isoformat()
                in_range = start <= cursor <= end
                tags = daily_tags.get(iso, {}) if in_range else {}
                total = sum(tags.values())
                cells.append({
                    "date": iso if in_range else None,
                    "total_seconds": round(total, 1),
                    "work_seconds": round(tags.get("Work", 0), 1),
                    "indie_seconds": round(tags.get("Indie", 0), 1),
                    "other_seconds": round(
                        sum(v for k, v in tags.items() if k not in ("Work", "Indie")),
                        1,
                    ),
                })
                cursor += timedelta(days=1)

            active_cells = [c for c in cells if c["date"] and c["total_seconds"] > 0]
            return jsonify({
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": cells,
                "days_with_data": len(active_cells),
                "total_seconds": round(sum(c["total_seconds"] for c in active_cells), 1),
                "work_seconds": round(sum(c["work_seconds"] for c in active_cells), 1),
                "indie_seconds": round(sum(c["indie_seconds"] for c in active_cells), 1),
            })

        # ---- API: One history day -------------------------------------
        @app.route("/api/history/day")
        def api_history_day():
            date_param = request.args.get("date", "")
            try:
                selected = date.fromisoformat(date_param)
            except (TypeError, ValueError):
                return jsonify({"error": "date must be YYYY-MM-DD"}), 400
            if selected > date.today():
                return jsonify({"error": "date cannot be in the future"}), 400

            selected_iso = selected.isoformat()
            tags = self._recorder.get_today_tag_distribution(selected_iso)
            apps = self._recorder.get_today_app_breakdown(selected_iso)
            timeline = self._recorder.get_today_timeline(selected_iso)
            total_seconds = sum(r["seconds"] for r in tags)
            idle_seconds = self._recorder.get_today_idle_time(selected_iso)

            for row in tags:
                row["seconds"] = round(row["seconds"], 1)
                row["duration"] = _fmt_duration(row["seconds"])
                row["percent"] = round(
                    row["seconds"] / total_seconds * 100 if total_seconds else 0, 1
                )
            for row in apps:
                row["seconds"] = round(row["seconds"], 1)
                row["duration"] = _fmt_duration(row["seconds"])
                row["percent"] = round(
                    row["seconds"] / total_seconds * 100 if total_seconds else 0, 1
                )
            for row in timeline:
                row["duration"] = _fmt_duration(row["seconds"])

            return jsonify({
                "date": selected_iso,
                "total_seconds": round(total_seconds, 1),
                "total": _fmt_duration(total_seconds),
                "idle_seconds": round(idle_seconds, 1),
                "idle": _fmt_duration(idle_seconds),
                "tags": tags,
                "apps": apps,
                "timeline": timeline,
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
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "JSON object required"}), 400
            if "idle_threshold" in data:
                try:
                    self._config.idle_threshold = int(data["idle_threshold"])
                except (TypeError, ValueError):
                    return jsonify({"error": "idle_threshold must be an integer"}), 400
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
            refresh = getattr(self._engine, "refresh_monitored_processes", None)
            if refresh:
                refresh()
            return jsonify({"status": "ok"})

        # ---- API: Tags CRUD ----
        @app.route("/api/tags")
        def api_list_tags():
            return jsonify(self._recorder.list_tags())

        @app.route("/api/tags", methods=["POST"])
        def api_add_tag():
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "JSON object required"}), 400
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
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "JSON object required"}), 400
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
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "JSON object required"}), 400
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
                refresh = getattr(self._engine, "refresh_monitored_processes", None)
                if refresh:
                    refresh()
            # Persist an exact app/project override. It has higher priority than
            # keyword and process rules, so the next poll cannot revert it.
            self._config.set_app_tag_override(proc, project, display_name, tag)
            try:
                self._recorder.update_app_tag(proc, display_name, project, tag)
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
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "JSON object required"}), 400
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
            try:
                start = date.fromisoformat(start_str) if start_str else None
                end = date.fromisoformat(end_str) if end_str else None
            except ValueError:
                return jsonify({"error": "start and end must be ISO dates"}), 400
            if start and end and start > end:
                return jsonify({"error": "start must not be after end"}), 400

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
                           SUM(seconds) AS seconds, MAX(updated_at) AS updated_at
                    FROM time_records
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
                           SUM(seconds) AS seconds, MAX(updated_at) AS updated_at
                    FROM time_records
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
            title = data.get("title", "")
            if not url:
                logger.warning("POST /api/chrome-url missing url field")
                return jsonify({"error": "url required"}), 400
            self._chrome_url_cache.set_url(url, title)
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
        self._http_server = make_server(HOST, PORT, self._app, threaded=True)
        self._thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="WebServer",
            daemon=True,
        )
        self._thread.start()
        logger.info("Web server listening on http://%s:%d", HOST, PORT)

    def stop(self):
        if self._http_server:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        logger.info("Web server stopped.")

    def open_browser(self):
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}")
