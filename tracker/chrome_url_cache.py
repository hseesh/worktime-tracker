"""Thread-safe cache for Chrome tab URLs reported by the browser extension.

The Chrome extension POSTs the active tab URL to the web server;
this cache stores the latest URL per Chrome window (identified by hwnd).
The tracking engine reads from this cache to resolve Work/Indie tags
based on domain rules instead of unreliable window-title keywords.
"""

import threading
import time
import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# URLs older than this (seconds) are considered stale and ignored.
_STALE_THRESHOLD = 120


class ChromeUrlCache:
    """Stores the most recent URL reported by the Chrome extension."""

    def __init__(self, recorder=None):
        self._lock = threading.Lock()
        self._url: str = ""
        self._ts: float = 0.0
        self._recorder = recorder

    def set_url(self, url: str) -> None:
        """Called by the web server when the extension reports a new URL."""
        with self._lock:
            old_url = self._url
            self._url = url or ""
            self._ts = time.monotonic()
        if url and url != old_url:
            domain = self.extract_domain(url)
            logger.info("Chrome URL reported: %s (domain=%s)", url, domain)
            if self._recorder:
                try:
                    self._recorder.record_chrome_url_event(url, domain)
                except Exception as e:
                    logger.warning("Failed to record Chrome URL event: %s", e)

    def get_url(self) -> str:
        """Return the cached URL if fresh, otherwise empty string."""
        with self._lock:
            if not self._url:
                return ""
            if time.monotonic() - self._ts > _STALE_THRESHOLD:
                logger.debug("Chrome URL stale, ignoring: %s", self._url)
                return ""
            return self._url

    @staticmethod
    def extract_domain(url: str) -> str:
        """Extract the registrable domain from a URL.

        e.g. 'https://chatgpt.com/c/abc' -> 'chatgpt.com'
             'https://gitee.com/zs-cloud' -> 'gitee.com'
        """
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            return host.lower()
        except Exception:
            return ""
