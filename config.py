"""Configuration management for WorkTime Tracker."""

import json
import os
import sys
import winreg
from pathlib import Path
from typing import Dict, List, Optional

# Default monitored processes: process_name -> display_name
DEFAULT_PROCESSES: Dict[str, str] = {
    "Devin.exe": "Devin",
    "idea64.exe": "IntelliJ IDEA",
    "idea.exe": "IntelliJ IDEA",
    "ChatGPT.exe": "Codex",
    "Unity.exe": "Unity Editor",
    "Weixin.exe": "WeChat (Work)",
    "WeChat.exe": "WeChat (Work)",
    "WeChatAppEx.exe": "WeChat (Work)",
    "msedge.exe": "Edge",
    "chrome.exe": "Chrome",
}

DEFAULT_IDLE_THRESHOLD_SECONDS = 300  # 5 minutes
DEFAULT_POLL_INTERVAL_MS = 1000       # 1 second

# Indie keyword rules: process_name -> list of keywords to match in window title
# Only Devin and Codex need Work/Indie split
DEFAULT_INDIE_KEYWORDS: Dict[str, List[str]] = {
    "Devin.exe": [],
    "ChatGPT.exe": [],
}

# Default tag keyword rules: process_name -> [{keyword, tag}]
# Checked in order; first match wins. Default tag is set via process_tags.
DEFAULT_TAG_KEYWORD_RULES: Dict[str, List[Dict]] = {
    "msedge.exe": [
        {"keyword": "ChatGPT", "tag": "Indie"},
        {"keyword": "Unity 资源商店", "tag": "Indie"},
        {"keyword": "Unity Asset Store", "tag": "Indie"},
        {"keyword": "Google Gemini", "tag": "Indie"},
        {"keyword": "Flowus", "tag": "Indie"},
        {"keyword": "P1", "tag": "Indie"},
        {"keyword": "Procedural UI", "tag": "Indie"},
        {"keyword": "Icon Cropper", "tag": "Indie"},
        {"keyword": "教务系统", "tag": "Work"},
        {"keyword": "CRM", "tag": "Work"},
        {"keyword": "任务调度中心", "tag": "Work"},
        {"keyword": "Gitee", "tag": "Work"},
        {"keyword": "Pull Requests", "tag": "Work"},
    ],
}

# Default process tags: process_name -> default tag when no keyword matches
DEFAULT_PROCESS_TAGS: Dict[str, str] = {
    "msedge.exe": "Work",
    "chrome.exe": "Work",
}

# Default URL domain tag rules: process_name -> [{domain, tag}]
# Checked in order; first domain match wins. Used when Chrome extension reports URL.
DEFAULT_URL_TAG_RULES: Dict[str, List[Dict]] = {
    "chrome.exe": [
        {"domain": "chatgpt.com", "tag": "Indie"},
        {"domain": "claude.ai", "tag": "Indie"},
        {"domain": "gemini.google.com", "tag": "Indie"},
        {"domain": "itch.io", "tag": "Indie"},
        {"domain": "unity.com", "tag": "Indie"},
        {"domain": "assetstore.unity.com", "tag": "Indie"},
        {"domain": "flowus.cn", "tag": "Indie"},
        {"domain": "gitee.com", "tag": "Work"},
        {"domain": "github.com", "tag": "Work"},
    ],
}

CONFIG_DIR = Path.home() / ".worktime-tracker"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = CONFIG_DIR / "worktime.db"


class AppConfig:
    """Manages persistent configuration with JSON file."""

    def __init__(self):
        self._processes: Dict[str, str] = dict(DEFAULT_PROCESSES)
        self._idle_threshold: int = DEFAULT_IDLE_THRESHOLD_SECONDS
        self._poll_interval: int = DEFAULT_POLL_INTERVAL_MS
        self._auto_start_minimized: bool = True
        self._auto_start_with_windows: bool = True
        self._indie_keywords: Dict[str, List[str]] = {k: list(v) for k, v in DEFAULT_INDIE_KEYWORDS.items()}
        self._work_keywords: Dict[str, List[str]] = {}
        self._process_tags: Dict[str, str] = {}
        self._tag_keyword_rules: Dict[str, List[Dict]] = {}
        self._app_tag_overrides: Dict[str, Dict[str, str]] = {}
        self._url_tag_rules: Dict[str, List[Dict]] = {}
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                # Merge: start with defaults, then overlay saved (so new defaults appear)
                merged = dict(DEFAULT_PROCESSES)
                merged.update(data.get("processes", {}))
                self._processes = merged
                self._idle_threshold = data.get("idle_threshold", DEFAULT_IDLE_THRESHOLD_SECONDS)
                self._poll_interval = data.get("poll_interval", DEFAULT_POLL_INTERVAL_MS)
                self._auto_start_minimized = data.get("auto_start_minimized", True)
                self._auto_start_with_windows = data.get("auto_start_with_windows", False)
                self._indie_keywords = data.get("indie_keywords", {k: list(v) for k, v in DEFAULT_INDIE_KEYWORDS.items()})
                self._work_keywords = data.get("work_keywords", {})
                self._process_tags = data.get("process_tags", {})
                self._tag_keyword_rules = data.get("tag_keyword_rules", {})
                self._app_tag_overrides = data.get("app_tag_overrides", {})
                self._url_tag_rules = data.get("url_tag_rules", {})

                # Merge default process tags (so new defaults appear)
                for proc, tag in DEFAULT_PROCESS_TAGS.items():
                    if proc not in self._process_tags:
                        self._process_tags[proc] = tag

                # Merge default tag keyword rules (so new defaults appear)
                for proc, rules in DEFAULT_TAG_KEYWORD_RULES.items():
                    if proc not in self._tag_keyword_rules:
                        self._tag_keyword_rules[proc] = list(rules)

                # Merge default URL tag rules (so new defaults appear)
                for proc, rules in DEFAULT_URL_TAG_RULES.items():
                    if proc not in self._url_tag_rules:
                        self._url_tag_rules[proc] = list(rules)

                # One-time migration: convert old indie/work keywords to tag keyword rules
                if not self._tag_keyword_rules and (self._indie_keywords or self._work_keywords):
                    self._migrate_old_keywords_to_tag_rules()
                    self.save()

                # Secondary migration: strip (Work)/(Indie) suffixes from process display names
                # and set process_tags for any that are missing
                need_save = False
                for proc, disp_name in list(self._processes.items()):
                    stripped = False
                    for suffix in (" (Work)", " (Indie)", " (Other)"):
                        if disp_name.endswith(suffix):
                            self._processes[proc] = disp_name[: -len(suffix)]
                            stripped = True
                            break
                    if proc not in self._process_tags:
                        if disp_name.endswith(" (Work)"):
                            self._process_tags[proc] = "Work"
                            need_save = True
                        elif disp_name.endswith(" (Indie)"):
                            self._process_tags[proc] = "Indie"
                            need_save = True
                    if stripped:
                        need_save = True
                if need_save:
                    self.save()
            except (json.JSONDecodeError, IOError):
                pass

    def _migrate_old_keywords_to_tag_rules(self):
        """Convert old indie_keywords + work_keywords into tag_keyword_rules + process_tags."""
        for proc, indie_kws in self._indie_keywords.items():
            rules = []
            for kw in indie_kws:
                rules.append({"keyword": kw, "tag": "Indie"})
            work_kws = self._work_keywords.get(proc, [])
            for kw in work_kws:
                rules.append({"keyword": kw, "tag": "Work"})
            if rules:
                self._tag_keyword_rules[proc] = rules
            # If process has indie keywords but no work keywords, default tag is Work
            if indie_kws and not work_kws:
                self._process_tags[proc] = "Work"

        # Also set process_tags for processes whose display_name had (Work) suffix
        for proc, disp_name in self._processes.items():
            if proc not in self._process_tags:
                if disp_name.endswith(" (Work)"):
                    self._process_tags[proc] = "Work"
                elif disp_name.endswith(" (Indie)"):
                    self._process_tags[proc] = "Indie"

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "processes": self._processes,
            "idle_threshold": self._idle_threshold,
            "poll_interval": self._poll_interval,
            "auto_start_minimized": self._auto_start_minimized,
            "auto_start_with_windows": self._auto_start_with_windows,
            "indie_keywords": self._indie_keywords,
            "work_keywords": self._work_keywords,
            "process_tags": self._process_tags,
            "tag_keyword_rules": self._tag_keyword_rules,
            "app_tag_overrides": self._app_tag_overrides,
            "url_tag_rules": self._url_tag_rules,
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def processes(self) -> Dict[str, str]:
        return self._processes

    @property
    def idle_threshold(self) -> int:
        return self._idle_threshold

    @idle_threshold.setter
    def idle_threshold(self, value: int):
        self._idle_threshold = max(10, value)

    @property
    def poll_interval(self) -> int:
        return self._poll_interval

    @property
    def auto_start_minimized(self) -> bool:
        return self._auto_start_minimized

    @auto_start_minimized.setter
    def auto_start_minimized(self, value: bool):
        self._auto_start_minimized = value

    @property
    def auto_start_with_windows(self) -> bool:
        return self._auto_start_with_windows

    @auto_start_with_windows.setter
    def auto_start_with_windows(self, value: bool):
        self._auto_start_with_windows = value
        self._set_registry_autostart(value)
        self.save()

    # ---- Windows registry auto-start ----

    REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    REGISTRY_VALUE_NAME = "WorkTimeTracker"

    @staticmethod
    def _get_startup_command() -> str:
        """Return the command to launch the app on startup."""
        # If running as a script, use pythonw + main.py
        # If frozen (pyinstaller exe), use the exe path directly
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        # Use pythonw (no console window) with the script
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        if pythonw.exists():
            return f'"{pythonw}" "{Path(__file__).resolve().parent / "main.py"}'
        return f'"{sys.executable}" "{Path(__file__).resolve().parent / "main.py"}'

    def _set_registry_autostart(self, enable: bool):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self.REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
            )
            if enable:
                winreg.SetValueEx(key, self.REGISTRY_VALUE_NAME, 0, winreg.REG_SZ, self._get_startup_command())
            else:
                try:
                    winreg.DeleteValue(key, self.REGISTRY_VALUE_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except OSError as e:
            import logging
            logging.getLogger("worktime-tracker").warning("Failed to set registry autostart: %s", e)

    def add_process(self, process_name: str, display_name: str):
        self._processes[process_name] = display_name
        self.save()

    def remove_process(self, process_name: str):
        self._processes.pop(process_name, None)
        self.save()

    def get_display_name(self, process_name: str, window_title: str = "") -> Optional[str]:
        """Return the base display name for a process (without tag suffix)."""
        for key, val in self._processes.items():
            if key.lower() == process_name.lower():
                # Strip any (Work)/(Indie) suffix from stored display_name for tag-based system
                name = val
                for suffix in (" (Work)", " (Indie)", " (Other)"):
                    if name.endswith(suffix):
                        name = name[: -len(suffix)]
                return name
        return None

    def resolve_tag(self, process_name: str, window_title: str = "") -> str:
        """Determine the tag for a process based on tag rules.

        Priority:
        1. Keyword rules: if any keyword matches window title, use that tag
        2. Process-level tag: if process has a direct tag assignment, use it
        3. Default: 'Other'
        """
        matched_key = None
        for key in self._processes:
            if key.lower() == process_name.lower():
                matched_key = key
                break

        # Check keyword-based tag rules first
        if matched_key and window_title:
            title_lower = window_title.lower()
            keyword_rules = self._tag_keyword_rules.get(matched_key, [])
            for rule in keyword_rules:
                kw = rule.get("keyword", "")
                tag = rule.get("tag", "Other")
                if kw and kw.lower() in title_lower:
                    return tag

        # Check process-level tag assignment
        if matched_key:
            proc_tag = self._process_tags.get(matched_key)
            if proc_tag:
                return proc_tag

        return "Other"

    def resolve_keyword_tag(self, process_name: str, window_title: str = "") -> Optional[str]:
        """Resolve tag from keyword rules only (no process-default fallback).

        Returns the matched tag, or None if no keyword rule matches.
        """
        matched_key = None
        for key in self._processes:
            if key.lower() == process_name.lower():
                matched_key = key
                break
        if not matched_key or not window_title:
            return None
        title_lower = window_title.lower()
        keyword_rules = self._tag_keyword_rules.get(matched_key, [])
        for rule in keyword_rules:
            kw = rule.get("keyword", "")
            tag = rule.get("tag", "Other")
            if kw and kw.lower() in title_lower:
                return tag
        return None

    @staticmethod
    def _app_override_key(project: str = "", display_name: str = "") -> str:
        if project:
            return "project:" + project.strip().lower()
        return "display:" + display_name.strip().lower()

    @property
    def app_tag_overrides(self) -> Dict[str, Dict[str, str]]:
        return self._app_tag_overrides

    def get_app_tag_override(
        self,
        process_name: str,
        project: str = "",
        display_name: str = "",
    ) -> Optional[str]:
        """Return a persistent App Breakdown override for this exact row."""
        overrides = self._app_tag_overrides.get(process_name.lower(), {})
        if project:
            tag = overrides.get(self._app_override_key(project=project))
            if tag:
                return tag
        if display_name:
            return overrides.get(self._app_override_key(display_name=display_name))
        return None

    def set_app_tag_override(
        self,
        process_name: str,
        project: str,
        display_name: str,
        tag: str,
    ):
        proc_key = process_name.lower()
        identity_key = self._app_override_key(project, display_name)
        if not identity_key.split(":", 1)[1]:
            raise ValueError("project or display_name is required")
        self._app_tag_overrides.setdefault(proc_key, {})[identity_key] = tag
        self.save()

    def resolve_app_tag(
        self,
        process_name: str,
        window_title: str = "",
        project: str = "",
        display_name: str = "",
    ) -> str:
        """Resolve an exact app/project override before normal tag rules."""
        override = self.get_app_tag_override(process_name, project, display_name)
        if override:
            return override
        return self.resolve_tag(process_name, window_title)

    def replace_tag_references(self, old_tag: str, new_tag: str):
        """Keep process, keyword and app overrides valid after tag rename/delete."""
        changed = False
        for proc, tag in list(self._process_tags.items()):
            if tag == old_tag:
                self._process_tags[proc] = new_tag
                changed = True
        for rules in self._tag_keyword_rules.values():
            for rule in rules:
                if rule.get("tag") == old_tag:
                    rule["tag"] = new_tag
                    changed = True
        for overrides in self._app_tag_overrides.values():
            for identity, tag in list(overrides.items()):
                if tag == old_tag:
                    overrides[identity] = new_tag
                    changed = True
        if changed:
            self.save()

    # ---- Tag rules management ----

    @property
    def process_tags(self) -> Dict[str, str]:
        return self._process_tags

    @property
    def tag_keyword_rules(self) -> Dict[str, List[Dict]]:
        return self._tag_keyword_rules

    @property
    def url_tag_rules(self) -> Dict[str, List[Dict]]:
        return self._url_tag_rules

    def set_url_tag_rules(self, process_name: str, rules: List[Dict]):
        self._url_tag_rules[process_name] = rules
        self.save()

    def resolve_url_tag(self, process_name: str, url: str) -> Optional[str]:
        """Resolve tag from URL domain rules.

        Returns the matched tag, or None if no URL rule matches
        (caller should fall back to keyword/process rules).
        """
        if not url:
            return None
        matched_key = None
        for key in self._processes:
            if key.lower() == process_name.lower():
                matched_key = key
                break
        if not matched_key:
            return None
        rules = self._url_tag_rules.get(matched_key, [])
        if not rules:
            return None
        from tracker.chrome_url_cache import ChromeUrlCache
        domain = ChromeUrlCache.extract_domain(url)
        if not domain:
            return None
        for rule in rules:
            rule_domain = rule.get("domain", "").lower()
            tag = rule.get("tag", "")
            if rule_domain and tag:
                if domain == rule_domain or domain.endswith("." + rule_domain):
                    return tag
        return None

    def set_process_tag(self, process_name: str, tag: str):
        self._process_tags[process_name] = tag
        self.save()

    def set_tag_keyword_rules(self, process_name: str, rules: List[Dict]):
        self._tag_keyword_rules[process_name] = rules
        self.save()

    def is_monitored(self, process_name: str) -> bool:
        for key in self._processes:
            if key.lower() == process_name.lower():
                return True
        return False

    @property
    def indie_keywords(self) -> Dict[str, List[str]]:
        return self._indie_keywords

    def set_indie_keywords(self, process_name: str, keywords: List[str]):
        self._indie_keywords[process_name] = [k.strip() for k in keywords if k.strip()]
        self.save()

    def get_indie_keywords(self, process_name: str) -> List[str]:
        return self._indie_keywords.get(process_name, [])
