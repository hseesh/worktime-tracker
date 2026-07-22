"""Detect foreground window and its process on Windows via Win32 API."""

import logging
from dataclasses import dataclass
from typing import Optional

import psutil
import win32gui
import win32process

logger = logging.getLogger(__name__)


@dataclass
class ForegroundInfo:
    process_name: str  # e.g. "idea64.exe"
    window_title: str  # e.g. "MyProject – Main.java"
    pid: int
    project: str = ""  # workspace name parsed from title (e.g. "zs-cloud")


# Processes whose window title contains the workspace/project name
_PROJECT_TITLE_PROCESSES = {"devin.exe", "windsurf.exe", "code.exe"}


def _parse_project(process_name: str, window_title: str) -> str:
    """Extract workspace/project name from window title for multi-window apps.

    Devin/Windsurf title format: "{workspace} - Devin - {file}"
    VS Code title format: "{file} - {workspace} - Visual Studio Code"
    """
    if not window_title:
        return ""
    proc_lower = process_name.lower()
    if proc_lower in ("devin.exe", "windsurf.exe"):
        for sep in (" - Devin - ", " - Windsurf - "):
            if sep in window_title:
                parts = window_title.split(sep, 1)
                if parts and parts[0].strip():
                    return parts[0].strip()
    elif proc_lower in ("code.exe",):
        # VS Code: "{file} - {workspace} - Visual Studio Code"
        parts = window_title.split(" - ")
        if len(parts) >= 3 and parts[-1].strip().lower().startswith("visual studio code"):
            return parts[-2].strip()
    return ""


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

        return ForegroundInfo(
            process_name=proc_name,
            window_title=title,
            pid=pid,
            project=_parse_project(proc_name, title),
        )
    except Exception as e:
        logger.debug("get_foreground_info error: %s", e)
        return None
