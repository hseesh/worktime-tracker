"""Codex hook script — called by Codex CLI hooks via stdin JSON protocol.

Codex pipes a JSON payload to stdin:
  {"hook_event_name": "PreToolUse", "session_id": "...", "cwd": "...", ...}

We extract event/session/project and POST to the local Codex event server.
"""
import json
import sys
import urllib.request
import datetime

try:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
except Exception:
    data = {}

event = data.get("hook_event_name", "Unknown")
session_id = data.get("session_id", "")
project = data.get("cwd", "")

payload = {
    "event": event,
    "sessionId": session_id,
    "project": project,
    "observedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
}
try:
    req = urllib.request.Request(
        "http://127.0.0.1:17890/events",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=3)
except Exception:
    pass
