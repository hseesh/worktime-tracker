"""Windows process-wide protection against duplicate app launches."""

import ctypes
from ctypes import wintypes


class SingleInstanceGuard:
    """Own a named Windows mutex for the lifetime of one application process."""

    _ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = "Local\\WorkTimeTracker.SingleInstance"):
        self._name = name
        self._handle = None

    def acquire(self) -> bool:
        """Return False when another instance already owns this mutex."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        ctypes.set_last_error(0)
        self._handle = kernel32.CreateMutexW(None, False, self._name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.get_last_error() != self._ERROR_ALREADY_EXISTS

    def release(self):
        """Close this process's mutex handle, if it has one."""
        if self._handle:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
            self._handle = None
