"""WorkTime Tracker — entry point.

Run:  python main.py
"""

import logging
import sys
import threading
from datetime import date, datetime, timedelta

from config import AppConfig
from tracker.chrome_url_cache import ChromeUrlCache
from tracker.cloud_sync import CloudSync
from tracker.codex_activity_manager import CodexActivityManager
from tracker.codex_event_server import CodexEventServer
from tracker.single_instance import SingleInstanceGuard
from tracker.time_recorder import TimeRecorder
from tracker.tracking_engine import TrackingEngine
from web.server import WebServer, HOST as WEB_HOST, PORT as WEB_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worktime-tracker")


def _create_tray_icon():
    """Create a system tray icon using pystray."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("pystray/Pillow not installed, skipping tray icon.")
        return None

    img = Image.new("RGBA", (64, 64), (122, 162, 247, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse([14, 14, 50, 50], outline=(192, 202, 245, 255), width=3)
    draw.line([32, 32, 32, 20], fill=(26, 27, 38, 255), width=3)
    draw.line([32, 32, 42, 32], fill=(26, 27, 38, 255), width=3)

    def on_open(icon, item):
        import webbrowser
        webbrowser.open(f"http://{WEB_HOST}:{WEB_PORT}")

    def on_quit(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Web UI", on_open, default=True),
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("WorkTimeTracker", img, "WorkTime Tracker", menu)
    return icon


def main():
    instance_guard = SingleInstanceGuard()
    if not instance_guard.acquire():
        logger.info("WorkTime Tracker is already running; exiting duplicate startup.")
        instance_guard.release()
        return

    config = AppConfig()
    config._set_registry_autostart(config.auto_start_with_windows)

    recorder = TimeRecorder(device_id=config.device_id)

    # AI token cache sync: scan Devin sessions.db + Codex JSONL files and
    # populate the ai_token_daily cache table for history (including today).
    # Runs in a daemon thread so startup is not blocked by the slow JSONL scan.
    def _sync_ai_tokens():
        try:
            recorder.sync_ai_token_cache(days=400)
            recorder.sync_tool_call_cache(days=400)
            logger.info("AI token + tool call cache sync complete.")
        except Exception as e:
            logger.warning("AI token/tool call cache sync failed: %s", e)

    ai_sync_thread = threading.Thread(target=_sync_ai_tokens, name="ai-token-sync", daemon=True)
    ai_sync_thread.start()

    # Cloud sync: push/pull all daily aggregates (time + AI tokens + tool calls,
    # including today) to/from Supabase. Runs once at startup, then every 30
    # minutes so today's growing data stays in sync across devices.
    cloud_sync = CloudSync(config, recorder)
    if cloud_sync.enabled:
        def _initial_cloud_sync():
            # Avoid publishing an empty or partial cache while the startup
            # source scan is still running.
            ai_sync_thread.join()
            cloud_sync.run_if_needed()

        threading.Thread(
            target=_initial_cloud_sync, name="cloud-sync", daemon=True
        ).start()

        def _periodic_sync_loop():
            """Refresh today's caches then force cloud sync every 30 min."""
            while True:
                threading.Event().wait(timeout=1800)
                try:
                    recorder.refresh_today_ai_token_cache()
                    recorder.refresh_today_tool_call_cache()
                    cloud_sync.run_if_needed(force=True)
                    logger.info("Periodic cloud sync complete.")
                except Exception as e:
                    logger.warning("Periodic cloud sync failed: %s", e)

        threading.Thread(target=_periodic_sync_loop, name="cloud-sync-periodic", daemon=True).start()
    else:
        logger.info("Cloud sync disabled. Set supabase.enabled=true in config.json to enable.")

    # Midnight loop: archive the just-ended day's caches (fills any gaps from
    # days where the process ran continuously without restart). Force-refresh
    # yesterday's data because the 30-min timer may have cached a partial day.
    def _ai_token_daily_loop():
        while True:
            now = datetime.now()
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            wait_seconds = (tomorrow - now).total_seconds()
            threading.Event().wait(timeout=max(wait_seconds, 1))
            # Force-refresh the completed day even if it was already cached by
            # the periodic timer, then publish it immediately.
            try:
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                recorder.refresh_ai_token_cache_for_date(yesterday)
                recorder.refresh_tool_call_cache_for_date(yesterday)
                if cloud_sync.enabled:
                    cloud_sync.run_if_needed(force=True)
                logger.info("Yesterday cache refresh complete for %s.", yesterday)
            except Exception as e:
                logger.warning("Yesterday cache refresh failed: %s", e)

    ai_daily_thread = threading.Thread(target=_ai_token_daily_loop, name="ai-token-daily", daemon=True)
    ai_daily_thread.start()

    codex_proc_names = ("ChatGPT.exe", "codex.exe", "codex-code-mode-host.exe")
    config_indie_kws = []
    for pn in codex_proc_names:
        config_indie_kws.extend(config.get_indie_keywords(pn))
    codex_manager = CodexActivityManager(recorder, indie_keywords=config_indie_kws)

    codex_server = CodexEventServer(codex_manager)
    codex_server.start()

    chrome_url_cache = ChromeUrlCache(recorder)

    engine = TrackingEngine(config, recorder, codex_manager, chrome_url_cache)
    engine.start()

    web_server = WebServer(config, recorder, engine, codex_manager, chrome_url_cache)
    web_server.start()

    logger.info("WorkTime Tracker started. Tracking: %s", engine.is_running())
    logger.info("Web UI: http://%s:%d", WEB_HOST, WEB_PORT)

    if not config.auto_start_minimized:
        web_server.open_browser()

    icon = _create_tray_icon()
    if icon:
        icon.run()
    else:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass

    try:
        engine.stop()
        codex_server.stop()
        web_server.stop()
        logger.info("WorkTime Tracker stopped.")
    finally:
        instance_guard.release()


if __name__ == "__main__":
    main()
