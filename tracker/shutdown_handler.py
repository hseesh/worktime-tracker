"""Windows shutdown handler — intercepts WM_QUERYENDSESSION for a final cloud sync.

Windows sends WM_QUERYENDSESSION to all top-level windows before shutting down
or logging off. We create a hidden window to intercept this message, push
today's data to Supabase, then allow shutdown to proceed.
"""

import logging
import threading

logger = logging.getLogger(__name__)

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016


def install_shutdown_sync(cloud_sync, recorder):
    """Create a hidden window that performs a final cloud push on system shutdown.

    Returns the daemon thread running the message pump, or None if disabled.
    """
    if not cloud_sync.enabled:
        return None

    try:
        import win32gui
    except ImportError:
        logger.warning("pywin32 not available, shutdown sync disabled.")
        return None

    def _final_push():
        try:
            recorder.refresh_today_ai_token_cache()
            recorder.refresh_today_tool_call_cache()
            cloud_sync._push()
            logger.info("Pre-shutdown cloud push completed.")
        except Exception as e:
            logger.warning("Pre-shutdown cloud push failed: %s", e)

    def _wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_QUERYENDSESSION:
            logger.info("System shutdown detected, doing final cloud push...")
            _final_push()
            return 1  # TRUE — allow shutdown
        if msg == WM_ENDSESSION:
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _message_pump():
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = "WTTShutdownSync"
        wc.lpfnWndProc = _wnd_proc
        atom = win32gui.RegisterClass(wc)

        win32gui.CreateWindowEx(
            0, atom, "WTTShutdownSync",
            0, 0, 0, 0, 0, 0, 0, None
        )

        msg = win32gui.GetMessage(None, 0, 0)
        while msg[0] != 0:
            win32gui.TranslateMessage(msg)
            win32gui.DispatchMessage(msg)
            msg = win32gui.GetMessage(None, 0, 0)

    t = threading.Thread(target=_message_pump, name="shutdown-sync", daemon=True)
    t.start()
    return t
