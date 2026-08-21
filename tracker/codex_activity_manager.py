"""Codex activity manager: tracks which project Codex is working on via hook events.

Design:
  - HTTP hook events mark which project is "active" (Codex is working on it).
  - No time accumulation here — time is counted by tracking_engine when Codex
    process is in the foreground window.
  - A project stays active for five minutes after its latest heartbeat; Stop
    can end it earlier.
  - Timer eligibility is further gated by Codex being the foreground window.
  - Duplicate events (same sessionId + event + observedAt) are idempotent.
"""

import logging
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_FUTURE_SKEW_SECONDS = 60    # tolerate minor clock skew, reject future events beyond this
ACTIVE_TIMEOUT_SECONDS = 300
VALID_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
HEARTBEAT_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}
MAX_SEEN_EVENTS = 10000


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
        self._seen_event_order = deque()

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
            self._seen_event_order.append(dedup_key)
            if len(self._seen_event_order) > MAX_SEEN_EVENTS:
                self._seen_events.discard(self._seen_event_order.popleft())

            self._recorder.add_codex_event(event, session_id, project, observed_at)

            project_name = self._get_project_display_name(project)

            if event in HEARTBEAT_EVENTS:
                # Keep the raw observedAt in the event log, but never let a
                # slightly fast client clock prolong active tracking.
                activity_ts = min(observed_at, datetime.now(timezone.utc))
                return self._handle_heartbeat(session_id, project, project_name, activity_ts)
            elif event == "Stop":
                return self._handle_stop(session_id, project, project_name, observed_at)
            else:
                return {"status": "ignored", "project": project}

    def get_active_projects(self) -> List[Dict]:
        """Return projects with a live session and a recent heartbeat."""
        with self._lock:
            now = datetime.now(timezone.utc)
            expired = {
                project for project, last_ts in self._project_last_event.items()
                if (now - last_ts).total_seconds() > ACTIVE_TIMEOUT_SECONDS
            }
            for project in expired:
                self._project_last_event.pop(project, None)
            if expired:
                self._sessions = {
                    session_id: session
                    for session_id, session in self._sessions.items()
                    if session["project"] not in expired
                }
            active_project_paths = {s["project"] for s in self._sessions.values()}
            result = []
            for proj, last_ts in self._project_last_event.items():
                if proj not in active_project_paths:
                    continue
                gap = (now - last_ts).total_seconds()
                proj_name = self._get_project_display_name(proj)
                result.append({
                    "project": proj,
                    "project_name": proj_name,
                    "last_activity": last_ts.isoformat(),
                    "active": True,
                    "idle_seconds": max(0, int(gap)),
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
        previous = self._sessions.get(session_id, {}).get("project")
        self._sessions[session_id] = {"project": project}
        if previous and previous != project:
            # A session can change working directories. Once it does, the old
            # project must not remain active unless another session still owns it.
            has_other = any(
                sid != session_id and s["project"] == previous
                for sid, s in self._sessions.items()
            )
            if not has_other:
                self._project_last_event.pop(previous, None)
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
        session = self._sessions.pop(session_id, None)
        stopped_project = session["project"] if session else project
        has_other = any(
            s["project"] == stopped_project for s in self._sessions.values()
        )
        if not has_other:
            self._project_last_event.pop(stopped_project, None)
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
        if dt.tzinfo is None or dt.utcoffset() is None:
            return False, "observedAt must include a timezone (for example, Z)", None
        if dt > datetime.now(timezone.utc) + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
            return False, "observedAt is too far in the future", None
        return True, "", dt
