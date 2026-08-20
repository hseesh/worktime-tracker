"""Thread-safe cache for Chrome tab URLs reported by the browser extension.

The extension supplies the active tab title with its URL.  Windows exposes
that title in the Chrome window caption, allowing visible Chrome windows to
be classified independently without applying the most recently active tab's
URL to every Chrome window.
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
        self._urls_by_title: dict[str, tuple[str, float]] = {}
        self._recorder = recorder

    @staticmethod
    def _title_key(title: str) -> str:
        title = (title or "").strip()
        for suffix in (" - Google Chrome", " - Chrome"):
            if title.endswith(suffix):
                title = title[: -len(suffix)]
                break
        return title.casefold()

    def set_url(self, url: str, title: str = "") -> None:
        """Called by the web server when the extension reports a new URL."""
        with self._lock:
            old_url = self._url
            self._url = url or ""
            self._ts = time.monotonic()
            title_key = self._title_key(title)
            if url and title_key:
                self._urls_by_title[title_key] = (url, self._ts)
        if url and url != old_url:
            domain = self.extract_domain(url)
            logger.info("Chrome URL reported: %s (domain=%s)", url, domain)
            if self._recorder:
                try:
                    self._recorder.record_chrome_url_event(url, domain)
                except Exception as e:
                    logger.warning("Failed to record Chrome URL event: %s", e)

    def get_url(self, window_title: str = "", allow_active_fallback: bool = True) -> str:
        """Return a fresh URL for this window title, if available.

        The global active-tab fallback is safe only for the focused Chrome
        window.  Callers tracking a background Chrome window disable it.
        """
        with self._lock:
            title_key = self._title_key(window_title)
            by_title = self._urls_by_title.get(title_key) if title_key else None
            if by_title and time.monotonic() - by_title[1] <= _STALE_THRESHOLD:
                return by_title[0]
            if not allow_active_fallback:
                return ""
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
