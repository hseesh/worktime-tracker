"""Codex activity manager: tracks which project Codex is working on via hook events.

Design:
  - HTTP hook events mark which project is "active" (Codex is working on it).
  - No time accumulation here — time is counted by tracking_engine when Codex
    process is in the foreground window.
  - A project stays active for ACTIVE_TIMEOUT (5 min) after the last event.
  - Stop event immediately deactivates the project.
  - Duplicate events (same sessionId + event + observedAt) are idempotent.
"""

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ACTIVE_TIMEOUT_SECONDS = 300    # 5 min: project stays active this long after last event
VALID_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
HEARTBEAT_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}


class CodexActivityManager:
    """Tracks active Codex projects from hook events. No time accumulation."""

    DEFAULT_INDIE_KEYWORDS = ["P1-c", "unity", "gamedev", "indie"]

    def __init__(self, recorder, indie_keywords: list = None):
        self._recorder = recorder
        self._lock = threading.Lock()
        self._indie_keywords = indie_keywords if indie_keywords is not None else list(self.DEFAULT_INDIE_KEYWORDS)

        # session_id -> {"project": str}
        self._sessions: Dict[str, dict] = {}

        # project_path -> last event datetime
        self._project_last_event: Dict[str, datetime] = {}

        # dedup
        self._seen_events: set = set()

    def set_foreground_checker(self, checker):
        """No-op — foreground checking is done by tracking_engine."""
        pass

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def handle_event(
        self,
        event: str,
        session_id: str,
        project: str,
        observed_at: datetime,
    ) -> Dict:
        with self._lock:
            dedup_key = (session_id, event, observed_at.isoformat())
            if dedup_key in self._seen_events:
                return {"status": "duplicate", "project": project}
            self._seen_events.add(dedup_key)

            self._recorder.add_codex_event(event, session_id, project, observed_at)

            project_name = self._get_project_display_name(project)

            if event in HEARTBEAT_EVENTS:
                return self._handle_heartbeat(session_id, project, project_name, observed_at)
            elif event == "Stop":
                return self._handle_stop(session_id, project, project_name, observed_at)
            else:
                return {"status": "ignored", "project": project}

    def get_active_projects(self) -> List[Dict]:
        """Return list of currently active projects (received event within ACTIVE_TIMEOUT)."""
        with self._lock:
            now = datetime.now(timezone.utc)
            result = []
            for proj, last_ts in self._project_last_event.items():
                gap = (now - last_ts).total_seconds()
                is_active = gap < ACTIVE_TIMEOUT_SECONDS
                if is_active:
                    proj_name = self._get_project_display_name(proj)
                    result.append({
                        "project": proj,
                        "project_name": proj_name,
                        "last_activity": last_ts.isoformat(),
                        "active": True,
                        "idle_seconds": int(gap),
                    })
            return sorted(result, key=lambda x: x["last_activity"], reverse=True)

    def get_current_active_project(self) -> Optional[Dict]:
        """Return the most recently active project, or None."""
        active = self.get_active_projects()
        return active[0] if active else None

    def get_today_codex_summary(self) -> List[Dict]:
        return self._recorder.get_codex_today_summary()

    # ------------------------------------------------------------------ #
    #  Internal                                                           #
    # ------------------------------------------------------------------ #

    def _is_indie_project(self, project_path: str) -> bool:
        path_lower = project_path.lower()
        for kw in self._indie_keywords:
            if kw and kw.lower() in path_lower:
                return True
        return False

    def _get_project_display_name(self, project_path: str) -> str:
        base = os.path.basename(project_path.rstrip("/\\")) or project_path
        if self._is_indie_project(project_path):
            return f"{base} (Indie)"
        return f"{base} (Work)"

    def _handle_heartbeat(
        self,
        session_id: str,
        project: str,
        project_name: str,
        ts: datetime,
    ) -> Dict:
        self._sessions[session_id] = {"project": project}
        self._project_last_event[project] = ts
        return {
            "status": "ok",
            "project": project,
            "project_name": project_name,
            "added_seconds": 0,
        }

    def _handle_stop(
        self,
        session_id: str,
        project: str,
        project_name: str,
        ts: datetime,
    ) -> Dict:
        self._sessions.pop(session_id, None)
        has_other = any(s["project"] == project for s in self._sessions.values())
        if not has_other:
            self._project_last_event.pop(project, None)
        return {
            "status": "stopped",
            "project": project,
            "project_name": project_name,
            "added_seconds": 0,
        }

    # ------------------------------------------------------------------ #
    #  Validation helpers                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def validate_event(event: str, session_id: str, project: str, observed_at_str: str) -> Tuple[bool, str, Optional[datetime]]:
        if event not in VALID_EVENTS:
            return False, f"Invalid event: {event}", None
        if not session_id or not isinstance(session_id, str):
            return False, "Missing or invalid sessionId", None
        if not project or not isinstance(project, str):
            return False, "Missing or invalid project", None
        if not os.path.isabs(project):
            return False, f"project must be an absolute path, got: {project}", None
        try:
            dt = datetime.fromisoformat(observed_at_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False, f"Cannot parse observedAt: {observed_at_str}", None
        return True, "", dt
