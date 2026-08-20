"""Detect foreground window and its process on Windows via Win32 API."""

import ctypes
import ctypes.wintypes as wintypes
import logging
from dataclasses import dataclass
from typing import List, Optional

import psutil
import win32gui
import win32process

from tracker.project_parser import parse_project as _parse_project

logger = logging.getLogger(__name__)

# Windows to skip (desktop, system tray, etc.)
_SKIP_TITLES = {"Program Manager", "Windows Input Experience", "MSCTFIME UI"}
_SKIP_PROCESSES = {"explorer.exe", "applicationframehost.exe", "systemsettings.exe",
                   "textinputhost.exe", "cockpit-tools.exe", "searchhost.exe",
                   "shellexperiencehost.exe"}


@dataclass
class ForegroundInfo:
    process_name: str  # e.g. "idea64.exe"
    window_title: str  # e.g. "MyProject – Main.java"
    pid: int
    project: str = ""  # workspace name parsed from title (e.g. "zs-cloud")
    monitor_index: int = -1
    hwnd: int = 0


def get_foreground_info() -> Optional[ForegroundInfo]:
    """Return the process name, window title and PID of the current foreground window.

    Returns None if the foreground window cannot be determined (e.g. desktop /
    taskbar / lock screen).
    """
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None

        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)

        if pid == 0:
            return None

        try:
            proc = psutil.Process(pid)
            proc_name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        if not title or title in _SKIP_TITLES or proc_name.lower() in _SKIP_PROCESSES:
            return None

        return ForegroundInfo(
            process_name=proc_name,
            window_title=title,
            pid=pid,
            project=_parse_project(proc_name, title),
            hwnd=int(hwnd),
        )
    except Exception as e:
        logger.debug("get_foreground_info error: %s", e)
        return None


def _get_monitor_rects() -> list:
    """Return list of monitor bounding rects via EnumDisplayMonitors (ctypes)."""
    rects = []
    try:
        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        def _callback(hmon, hdc, lprect, lparam):
            r = lprect.contents
            rects.append((r.left, r.top, r.right, r.bottom))
            return True
        ctypes.windll.user32.EnumDisplayMonitors(None, None, _callback, 0)
    except Exception as e:
        logger.debug("EnumDisplayMonitors error: %s", e)
    return rects


def _window_rect(hwnd) -> tuple:
    """Return (left, top, right, bottom) for a window."""
    try:
        return win32gui.GetWindowRect(hwnd)
    except Exception:
        return (0, 0, 0, 0)


def _rect_center(rect) -> tuple:
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def _rect_contains(rect, point) -> bool:
    return rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def _get_monitor_index(point, monitors) -> int:
    """Return the monitor index that contains the given point, or -1."""
    for idx, mon_rect in enumerate(monitors):
        if _rect_contains(mon_rect, point):
            return idx
    return -1


# Set of monitored process names (lowercase), populated by tracking_engine at startup
_monitored_processes: set = set()


def set_monitored_processes(processes: set):
    """Set the list of monitored process names for priority selection."""
    global _monitored_processes
    _monitored_processes = {p.lower() for p in processes}


def _is_monitored(proc_name: str) -> bool:
    return proc_name.lower() in _monitored_processes


def get_visible_windows() -> List[ForegroundInfo]:
    """Return the active window per monitor for dual-monitor tracking.

    For the monitor that has the foreground (focused) window, uses that window.
    For other monitors, picks the largest visible non-minimized window.
    """
    monitors = _get_monitor_rects()
    if not monitors:
        w = win32gui.GetSystemMetrics(0)
        h = win32gui.GetSystemMetrics(1)
        monitors = [(0, 0, w, h)]

    result = {}  # monitor_index -> ForegroundInfo

    # 1. Get the foreground window and assign it to its monitor
    fg_hwnd = win32gui.GetForegroundWindow()
    if fg_hwnd:
        fg_rect = _window_rect(fg_hwnd)
        fg_center = _rect_center(fg_rect)
        fg_mon = _get_monitor_index(fg_center, monitors)
        if fg_mon >= 0:
            _, fg_pid = win32process.GetWindowThreadProcessId(fg_hwnd)
            if fg_pid:
                try:
                    fg_name = psutil.Process(fg_pid).name()
                    fg_title = win32gui.GetWindowText(fg_hwnd)
                    if (
                        not fg_title
                        or fg_title in _SKIP_TITLES
                        or fg_name.lower() in _SKIP_PROCESSES
                    ):
                        raise ValueError("untrackable foreground window")
                    result[fg_mon] = ForegroundInfo(
                        process_name=fg_name,
                        window_title=fg_title,
                        pid=fg_pid,
                        project=_parse_project(fg_name, fg_title),
                        monitor_index=fg_mon,
                        hwnd=int(fg_hwnd),
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    pass

    # 2. For other monitors, find the best visible window
    # Strategy: prefer largest area monitored process; fallback to largest unmonitored
    # Using area instead of z-order because full-screen apps have largest area
    best_monitored = {}   # monitor_index -> (area, ForegroundInfo)
    best_any = {}         # monitor_index -> (area, ForegroundInfo)
    z_order = [0]

    def _enum_callback(hwnd, _):
        z_order[0] += 1
        if hwnd == fg_hwnd:
            return True  # Skip the foreground window itself
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.IsIconic(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if title in _SKIP_TITLES:
            return True

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == 0:
            return True
        try:
            proc_name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        if proc_name.lower() in _SKIP_PROCESSES:
            return True

        rect = _window_rect(hwnd)
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if area <= 0:
            return True
        center = _rect_center(rect)

        mon_idx = _get_monitor_index(center, monitors)
        if mon_idx < 0 or mon_idx in result:
            return True  # Skip monitors that already have the foreground window

        info = ForegroundInfo(
            process_name=proc_name,
            window_title=title,
            pid=pid,
            project=_parse_project(proc_name, title),
            monitor_index=mon_idx,
            hwnd=int(hwnd),
        )

        # Track best monitored (by area) and best overall (by area)
        if _is_monitored(proc_name):
            prev = best_monitored.get(mon_idx)
            if prev is None or area > prev[0]:
                best_monitored[mon_idx] = (area, info)
        else:
            prev = best_any.get(mon_idx)
            if prev is None or area > prev[0]:
                best_any[mon_idx] = (area, info)
        return True

    try:
        win32gui.EnumWindows(_enum_callback, None)
    except Exception as e:
        logger.debug("get_visible_windows EnumWindows error: %s", e)

    # Merge: foreground monitor + best window for other monitors
    # Prefer monitored processes (by z-order); fallback to largest unmonitored
    all_monitors = set(range(len(monitors)))
    for idx in all_monitors:
        if idx in result:
            continue
        if idx in best_monitored:
            result[idx] = best_monitored[idx][1]
        elif idx in best_any:
            result[idx] = best_any[idx][1]

    return list(result.values())
