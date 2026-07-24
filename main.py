"""WorkTime Tracker — entry point.

Run:  python main.py
"""

import logging
import os
import sys
import threading

from config import AppConfig
from tracker.chrome_url_cache import ChromeUrlCache
from tracker.codex_activity_manager import CodexActivityManager
from tracker.codex_event_server import CodexEventServer
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
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Web UI", on_open, default=True),
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("WorkTimeTracker", img, "WorkTime Tracker", menu)
    return icon


def main():
    config = AppConfig()
    config._set_registry_autostart(config.auto_start_with_windows)

    recorder = TimeRecorder()

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

    engine.stop()
    codex_server.stop()
    web_server.stop()
    logger.info("WorkTime Tracker stopped.")


if __name__ == "__main__":
    main()
