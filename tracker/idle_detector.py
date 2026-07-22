"""Detect user idle time via Windows GetLastInputInfo API."""

import ctypes
import ctypes.wintypes as wintypes


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def get_idle_seconds() -> float:
    """Return the number of seconds since the last keyboard/mouse input."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0

    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    # Handle 32-bit tick count wrap-around (~49 days)
    if millis < 0:
        millis += 2**32
    return millis / 1000.0


def is_idle(threshold_seconds: int) -> bool:
    """Return True if the user has been idle for longer than *threshold_seconds*."""
    return get_idle_seconds() >= threshold_seconds
