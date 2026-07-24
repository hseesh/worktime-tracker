"""Pure window-title project parsing, kept independent from Win32 imports."""

import re


def parse_project(process_name: str, window_title: str) -> str:
    """Extract a stable workspace/project identity from a window title."""
    if not window_title:
        return ""

    proc_lower = process_name.lower()
    if proc_lower in ("devin.exe", "windsurf.exe"):
        match = re.match(
            r"^\s*(.+?)\s+[-–—]\s+(?:Devin|Windsurf)(?:\s+[-–—]\s+.*)?$",
            window_title,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
    elif proc_lower == "code.exe":
        # VS Code: "{file} - {workspace} - Visual Studio Code"
        parts = window_title.split(" - ")
        if len(parts) >= 3 and parts[-1].strip().lower().startswith("visual studio code"):
            return parts[-2].strip()
    return ""
