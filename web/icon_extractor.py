"""Extract application icons from .exe files on Windows and cache as PNG."""

import logging
import os
from pathlib import Path

import psutil
import win32gui
import win32ui
from PIL import Image

logger = logging.getLogger(__name__)

ICON_CACHE_DIR = Path.home() / ".worktime-tracker" / "icons"
ICON_SIZE = 32

# In-memory cache: process_name -> PNG bytes (or None if no icon)
_path_cache: dict[str, str | None] = {}
_png_cache: dict[str, bytes | None] = {}


def _find_exe_path(process_name: str) -> str | None:
    """Find the full path of an executable by process name using psutil."""
    if process_name in _path_cache:
        return _path_cache[process_name]

    proc_lower = process_name.lower()
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            info = proc.info
            if info["name"] and info["name"].lower() == proc_lower:
                _path_cache[process_name] = info["exe"]
                return info["exe"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _path_cache[process_name] = None
    return None


def _extract_icon_png(exe_path: str, size: int = ICON_SIZE) -> bytes | None:
    """Extract the first icon from *exe_path* and return PNG bytes."""
    try:
        large, small = win32gui.ExtractIconEx(exe_path, 0, 1)
        hicon = (large[0] if large else (small[0] if small else None))
        if not hicon:
            return None

        hdc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
        hdc_mem = hdc.CreateCompatibleDC()

        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(hdc, size, size)
        hdc_mem.SelectObject(bmp)

        win32gui.DrawIconEx(
            hdc_mem.GetHandleOutput(),
            0, 0, hicon, size, size,
            0, None, 0x0003,  # DI_NORMAL
        )

        bmpinfo = bmp.GetInfo()
        bmpbits = bmp.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGBA",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpbits,
            "raw", "BGRA", 0, 1,
        )

        # Cleanup
        win32gui.DestroyIcon(hicon)
        hdc_mem.DeleteDC()
        win32gui.ReleaseDC(0, hdc.GetHandleOutput())

        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.debug("extract_icon failed for %s: %s", exe_path, e)
        return None


def get_icon_png(process_name: str) -> bytes | None:
    """Return cached PNG bytes for *process_name*'s icon, or None."""
    if not process_name:
        return None

    if process_name in _png_cache:
        return _png_cache[process_name]

    ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = ICON_CACHE_DIR / f"{process_name}.png"

    if cache_file.exists():
        png = cache_file.read_bytes()
        _png_cache[process_name] = png
        return png

    exe_path = _find_exe_path(process_name)
    if not exe_path or not os.path.isfile(exe_path):
        _png_cache[process_name] = None
        return None

    png = _extract_icon_png(exe_path)
    if png:
        cache_file.write_bytes(png)

    _png_cache[process_name] = png
    return png
