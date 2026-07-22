"""Lightweight HTTP server for receiving Codex hook events.

Binds only to 127.0.0.1:17890. Uses Python standard library http.server
to avoid extra dependencies. Runs in a daemon thread.
"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from tracker.codex_activity_manager import CodexActivityManager

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 17890


class _CodexEventHandler(BaseHTTPRequestHandler):
    """Per-request handler. Uses self.server._activity_manager."""

    def do_POST(self):
        if self.path != "/events":
            logger.info("POST 404 path=%s from=%s", self.path, self.address_string())
            self._send_json(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0 or length > 65536:
                logger.warning("POST 400 invalid content_length=%d from=%s", length, self.address_string())
                self._send_json(400, {"error": "Invalid content length"})
                return

            raw = self.rfile.read(length)
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("POST 400 invalid_json from=%s err=%s", self.address_string(), e)
            self._send_json(400, {"error": "Invalid JSON"})
            return

        # Extract fields
        event = data.get("event", "")
        session_id = data.get("sessionId", "")
        project = data.get("project", "")
        observed_at_str = data.get("observedAt", "")

        logger.info(
            "POST /events event=%s session=%s project=%s observedAt=%s from=%s",
            event, session_id, project, observed_at_str, self.address_string(),
        )

        # Validate
        is_valid, error_msg, dt = CodexActivityManager.validate_event(
            event, session_id, project, observed_at_str
        )
        if not is_valid:
            logger.warning("POST 400 validation_failed event=%s err=%s", event, error_msg)
            self._send_json(400, {"error": error_msg})
            return

        # Handle
        manager: CodexActivityManager = self.server._activity_manager
        result = manager.handle_event(event, session_id, project, dt)

        logger.info(
            "POST 200 status=%s project=%s added_seconds=%s",
            result.get("status"), result.get("project"), result.get("added_seconds", 0),
        )
        self._send_json(200, result)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "Not found"})

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress default request logging; use logger instead
        logger.debug("HTTP %s - %s", self.address_string(), format % args)


class CodexEventServer:
    """Manages the HTTP server lifecycle in a daemon thread."""

    def __init__(self, activity_manager: CodexActivityManager):
        self._activity_manager = activity_manager
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        if self._server is not None:
            return
        try:
            self._server = HTTPServer((HOST, PORT), _CodexEventHandler)
            self._server._activity_manager = self._activity_manager
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="CodexEventServer",
                daemon=True,
            )
            self._thread.start()
            logger.info("Codex event server listening on http://%s:%d/events", HOST, PORT)
        except OSError as e:
            logger.warning("Failed to start Codex event server on port %d: %s", PORT, e)
            self._server = None

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            if self._thread:
                self._thread.join(timeout=3)
                self._thread = None
            logger.info("Codex event server stopped.")

    @property
    def is_running(self) -> bool:
        return self._server is not None
