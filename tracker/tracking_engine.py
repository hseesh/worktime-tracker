"""Tracking orchestrator: polls foreground window, checks idle, records time."""

import logging
import threading
import time
from typing import List, Optional

from config import AppConfig
from tracker.chrome_url_cache import ChromeUrlCache
from tracker.idle_detector import is_idle
from tracker.time_recorder import TimeRecorder
from tracker.window_tracker import ForegroundInfo, get_foreground_info, get_visible_windows, set_monitored_processes

logger = logging.getLogger(__name__)

# Processes whose window title doesn't contain project info; use Codex hook for indie/work classification
_CODEX_PROCESSES = {"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"}


class TrackingEngine:
    """Runs a timer thread that samples the foreground window every *poll_interval* ms."""

    def __init__(self, config: AppConfig, recorder: TimeRecorder, codex_manager=None, chrome_url_cache: ChromeUrlCache = None):
        self._config = config
        self._recorder = recorder
        self._codex_manager = codex_manager
        self._chrome_url_cache = chrome_url_cache
        self._timer: Optional[threading.Timer] = None
        self._last_fg: Optional[ForegroundInfo] = None
        self._current_windows: List[ForegroundInfo] = []
        self._last_ts: float = 0.0
        self._running = False
        self._lock = threading.Lock()

        # Wire foreground checker to codex_manager for cross-validation
        if codex_manager:
            codex_manager.set_foreground_checker(self._is_codex_foreground)

        # Populate monitored process names for dual-monitor window selection
        set_monitored_processes(set(config.processes.keys()))

    def _is_codex_foreground(self) -> bool:
        """Return True when Codex is selected as an active visible window."""
        return any(
            fg.process_name.lower() in _CODEX_PROCESSES
            for fg in self.get_current_windows()
        )

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._last_ts = time.monotonic()
        self._schedule_poll()

    def stop(self):
        with self._lock:
            self._running = False
            timer = self._timer
            self._timer = None
        if timer:
            timer.cancel()
        with self._lock:
            self._current_windows = []
            self._last_fg = None
        self._flush()

    def is_running(self) -> bool:
        return self._running

    def get_current_windows(self) -> List[ForegroundInfo]:
        """Return the windows selected for the most recent per-monitor sample."""
        with self._lock:
            return list(self._current_windows)

    def refresh_monitored_processes(self):
        """Apply settings changes to multi-monitor window selection immediately."""
        set_monitored_processes(set(self._config.processes.keys()))

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
                    self._current_windows = []
                    return

                # Get all visible topmost windows (one per monitor)
                windows = get_visible_windows()
                if not windows:
                    self._flush()
                    self._last_fg = None
                    self._current_windows = []
                    return

                self._current_windows = list(windows)

                # Identify the focused window. Chrome's extension can only
                # report the active Chrome window, so URL-based rules must not
                # be applied to a visible but unfocused Chrome window.
                focused = get_foreground_info()
                focused_hwnd = 0
                if focused:
                    focused_hwnd = focused.hwnd

                self._record_selected_windows(windows, elapsed, focused_hwnd)

                # Keep the focused window for display
                if focused:
                    self._last_fg = focused
                elif windows:
                    self._last_fg = windows[0]
        except Exception as e:
            logger.error("Poll error: %s", e, exc_info=True)
        finally:
            self._schedule_poll()

    def _record_selected_windows(
        self,
        windows: List[ForegroundInfo],
        elapsed: float,
        focused_hwnd: int,
    ):
        """Record selected windows, counting all Codex sessions as one activity."""
        codex_recorded = False
        tags_seen = set()
        focused_codex_present = any(
            fg.process_name.lower() in _CODEX_PROCESSES and fg.hwnd == focused_hwnd
            for fg in windows
        )
        for fg in windows:
            if fg.process_name.lower() in _CODEX_PROCESSES:
                # Prefer the focused Codex window as the single representative,
                # so its time also contributes to the focus metric.
                if focused_codex_present and fg.hwnd != focused_hwnd:
                    continue
                # A Codex window selected on another display does not need
                # keyboard focus, but all sessions still share one timer.
                if codex_recorded:
                    continue
                codex_recorded = True
            tag = self._record_window(
                fg, elapsed, fg.hwnd == focused_hwnd, record_tag_total=False
            )
            if tag:
                tags_seen.add(tag)

        # One wall-clock sample per tag, even when two same-tag apps occupy
        # different monitors. App/project segments remain independently stored.
        for tag in tags_seen:
            self._recorder.add_tag_time(tag, elapsed)

    def _compute_tag(self, fg: ForegroundInfo) -> Optional[str]:
        """Compute the tag for a window without recording (for dedup checks)."""
        if fg.process_name.lower() in _CODEX_PROCESSES and self._codex_manager:
            active_proj = self._codex_manager.get_current_active_project()
            if active_proj:
                proj_name = active_proj["project_name"]
                tag = "Indie" if "(Indie)" in proj_name else "Work"
                override = self._config.get_app_tag_override(
                    "codex.exe", active_proj["project"], proj_name
                )
                if override:
                    tag = override
                return tag
            # Codex is intentionally not tracked until its local HTTP hook
            # identifies an active project.
            return None
        return self._resolve_tag_with_url(fg)

    def _record_window(
        self,
        fg: ForegroundInfo,
        elapsed: float,
        is_focused: bool = True,
        record_tag_total: bool = True,
    ) -> Optional[str]:
        """Record elapsed time for a single visible window."""
        display_name = self._config.get_display_name(fg.process_name, fg.window_title)

        # Codex is recorded only after a local HTTP hook marks a project active.
        if fg.process_name.lower() in _CODEX_PROCESSES and self._codex_manager:
            active_proj = self._codex_manager.get_current_active_project()
            if active_proj:
                proj_name = active_proj["project_name"]
                proj_path = active_proj["project"]
                tag = "Indie" if "(Indie)" in proj_name else "Work"
                override = self._config.get_app_tag_override(
                    "codex.exe", proj_path, proj_name
                )
                if override:
                    tag = override
                self._recorder.add_codex_time(
                    proj_path, proj_name, elapsed, tag, record_tag_total=record_tag_total
                )
                if is_focused:
                    self._recorder.add_focus_time(tag, elapsed)
                return tag
            return None
        else:
            tag = self._resolve_tag_with_url(fg, use_chrome_url=is_focused)

        # Chrome with no matching URL/keyword rule → skip recording
        if tag is None:
            return None

        if display_name is None:
            other_name = f"Other ({fg.process_name})"
            self._recorder.add_time(
                fg.process_name,
                other_name,
                elapsed,
                fg.project,
                "Other",
                record_tag_total=record_tag_total,
            )
            return "Other"

        display_name = self._build_display_name(display_name, fg.project)
        # An exact App Breakdown override wins over automatic classification.
        # Otherwise retain the URL-derived tag instead of overwriting it with
        # the process default (which previously turned Chrome Indie pages into Work).
        override = self._config.get_app_tag_override(
            fg.process_name, fg.project, display_name
        )
        if override:
            tag = override
        self._recorder.add_time(
            fg.process_name,
            display_name,
            elapsed,
            fg.project,
            tag,
            record_tag_total=record_tag_total,
        )
        if is_focused:
            self._recorder.add_focus_time(tag, elapsed)
        return tag

    def _flush(self):
        """Record any pending time for the last known foreground process."""
        # Time is already accumulated incrementally in _poll, so nothing to do
        # here.  This method exists for potential future batch-flush logic.
        pass

    def _resolve_tag_with_url(
        self, fg: ForegroundInfo, use_chrome_url: bool = True
    ) -> Optional[str]:
        """Resolve tag: try URL domain rules, then keyword rules.

        For Chrome, returns None if no rule matches (skip recording).
        For other processes, falls back to process default tag.
        """
        if self._chrome_url_cache and fg.process_name.lower() == "chrome.exe":
            url = self._chrome_url_cache.get_url(
                fg.window_title, allow_active_fallback=use_chrome_url
            )
            if url:
                url_tag = self._config.resolve_url_tag(fg.process_name, url)
                if url_tag:
                    logger.info("Chrome URL tag resolved: url=%s tag=%s", url, url_tag)
                    return url_tag
                logger.debug("Chrome URL no domain match: url=%s", url)
            else:
                logger.debug("Chrome window visible but no fresh URL from extension")
            # Try keyword rules (without process-default fallback)
            kw_tag = self._config.resolve_keyword_tag(fg.process_name, fg.window_title)
            if kw_tag:
                logger.info("Chrome keyword tag resolved: title=%s tag=%s", fg.window_title, kw_tag)
                return kw_tag
            # No URL match, no keyword match → don't track Chrome
            logger.debug("Chrome no rule matched, skipping: title=%s", fg.window_title)
            return None
        return self._config.resolve_tag(fg.process_name, fg.window_title)

    @staticmethod
    def _build_display_name(display_name: str, project: str) -> str:
        """Insert project into display_name: 'Devin' → 'Devin [zs-cloud]'"""
        if not project or not display_name:
            return display_name
        return f"{display_name} [{project}]"
