"""Tracking orchestrator: polls foreground window, checks idle, records time."""

import logging
import threading
import time
from typing import Optional, Tuple

from config import AppConfig
from tracker.idle_detector import is_idle
from tracker.time_recorder import TimeRecorder
from tracker.window_tracker import ForegroundInfo, get_foreground_info

logger = logging.getLogger(__name__)

# Processes whose window title doesn't contain project info; use Codex hook for indie/work classification
_CODEX_PROCESSES = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}


class TrackingEngine:
    """Runs a timer thread that samples the foreground window every *poll_interval* ms."""

    def __init__(self, config: AppConfig, recorder: TimeRecorder, codex_manager=None):
        self._config = config
        self._recorder = recorder
        self._codex_manager = codex_manager
        self._timer: Optional[threading.Timer] = None
        self._last_fg: Optional[ForegroundInfo] = None
        self._last_ts: float = 0.0
        self._running = False
        self._lock = threading.Lock()

        # Wire foreground checker to codex_manager for cross-validation
        if codex_manager:
            codex_manager.set_foreground_checker(self._is_codex_foreground)

    def _is_codex_foreground(self) -> bool:
        """Check if any Codex process window is visible and not minimized.

        Uses EnumWindows instead of GetForegroundWindow — the window doesn't
        need to have keyboard focus, just be visible (not minimized, not hidden).
        """
        try:
            import win32gui
            import win32process
            import psutil

            found = [False]

            def _enum_callback(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if win32gui.IsIconic(hwnd):
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid == 0:
                    return True
                try:
                    proc_name = psutil.Process(pid).name().lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return True
                if proc_name in _CODEX_PROCESSES:
                    found[0] = True
                    return False
                return True

            win32gui.EnumWindows(_enum_callback, None)
            return found[0]
        except Exception as e:
            logger.error("EnumWindows error: %s", e)
            return False

    def start(self):
        if self._running:
            return
        self._running = True
        self._last_ts = time.monotonic()
        self._schedule_poll()

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._flush()

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ #

    def _schedule_poll(self):
        if not self._running:
            return
        interval = self._config.poll_interval / 1000.0
        self._timer = threading.Timer(interval, self._poll)
        self._timer.daemon = True
        self._timer.start()

    def _poll(self):
        if not self._running:
            return
        try:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_ts
                self._last_ts = now

                # Idle check
                if is_idle(self._config.idle_threshold):
                    self._flush()
                    self._recorder.add_idle_time(elapsed)
                    self._last_fg = None
                    return

                fg = get_foreground_info()
                if fg is None:
                    self._flush()
                    self._last_fg = None
                    return

                display_name = self._config.get_display_name(fg.process_name, fg.window_title)

                # Check if any Codex window is visible (not minimized) via EnumWindows.
                # This is independent of which window has keyboard focus.
                if self._codex_manager and self._is_codex_foreground():
                    active_proj = self._codex_manager.get_current_active_project()
                    if active_proj:
                        proj_name = active_proj["project_name"]
                        proj_path = active_proj["project"]
                        tag = "Indie" if "(Indie)" in proj_name else "Work"
                        self._recorder.add_codex_time(proj_path, proj_name, elapsed, tag)
                        self._last_fg = fg
                        return

                # For Codex processes with focus but no active hook project — fallback
                if fg.process_name.lower() in _CODEX_PROCESSES and self._codex_manager:
                    display_name, tag = self._classify_codex_from_hook(display_name, fg.window_title)
                else:
                    tag = self._config.resolve_tag(fg.process_name, fg.window_title)

                if display_name is None:
                    # Unmonitored process — track as "Other" for stats visibility
                    other_name = f"Other ({fg.process_name})"
                    other_tag = "Other"
                    if self._last_fg and self._last_fg.process_name.lower() == fg.process_name.lower():
                        self._recorder.add_time(fg.process_name, other_name, elapsed, fg.project, other_tag)
                    else:
                        self._flush()
                        self._last_fg = fg
                        self._recorder.add_time(fg.process_name, other_name, elapsed, fg.project, other_tag)
                    return

                # Build display_name with project for multi-window apps (e.g. Devin [zs-cloud])
                display_name = self._build_display_name(display_name, fg.project)

                # Same monitored process AND same display_name (including project) → accumulate
                if self._last_fg and self._last_fg.process_name.lower() == fg.process_name.lower():
                    last_display = self._config.get_display_name(self._last_fg.process_name, self._last_fg.window_title)
                    last_display = self._build_display_name(last_display, self._last_fg.project)
                    if last_display == display_name:
                        self._recorder.add_time(fg.process_name, display_name, elapsed, fg.project, tag)
                    else:
                        # Same process but project or category changed
                        self._last_fg = fg
                        self._recorder.add_time(fg.process_name, display_name, elapsed, fg.project, tag)
                else:
                    # Context switch — flush old, start new
                    self._flush()
                    self._last_fg = fg
                    self._recorder.add_time(fg.process_name, display_name, elapsed, fg.project, tag)
        except Exception as e:
            logger.error("Poll error: %s", e, exc_info=True)
        finally:
            self._schedule_poll()

    def _flush(self):
        """Record any pending time for the last known foreground process."""
        # Time is already accumulated incrementally in _poll, so nothing to do
        # here.  This method exists for potential future batch-flush logic.
        pass

    @staticmethod
    def _build_display_name(display_name: str, project: str) -> str:
        """Insert project into display_name: 'Devin' → 'Devin [zs-cloud]'"""
        if not project or not display_name:
            return display_name
        return f"{display_name} [{project}]"

    def _classify_codex_from_hook(self, display_name: str, window_title: str = "") -> Tuple[str, str]:
        """Use Codex hook active project to classify tag for Codex foreground time.

        Returns (display_name, tag).
        """
        if not self._codex_manager:
            tag = self._config.resolve_tag("ChatGPT.exe", window_title)
            return display_name, tag
        active = self._codex_manager.get_active_projects()
        indie_active = any(p["active"] and "(Indie)" in p["project_name"] for p in active)
        work_active = any(p["active"] and "(Work)" in p["project_name"] for p in active)
        if indie_active:
            return display_name, "Indie"
        if work_active:
            return display_name, "Work"
        tag = self._config.resolve_tag("ChatGPT.exe", window_title)
        return display_name, tag
