"""Regression tests for cloud sync pagination and pull robustness."""

import unittest
from unittest.mock import MagicMock, patch

from tracker import cloud_sync as cloud_sync_module
from tracker.cloud_sync import CloudSync


def _resp(status: int, payload, content_range: str = None):
    """Build a fake requests.Response for ``_fetch_cloud``."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = "" if payload else ""
    headers = {}
    if content_range:
        headers["Content-Range"] = content_range
    resp.headers = headers
    return resp


class TestFetchCloudPagination(unittest.TestCase):

    def _sync(self):
        config = MagicMock()
        config.supabase = {"url": "https://example.supabase.co", "anon_key": "key"}
        config.device_id = "local-device"
        config.supabase_enabled = True
        return CloudSync(config, MagicMock())

    def test_partial_content_pages_are_followed(self):
        """Supabase answers 206 when a page is partial; that is not an error.

        Rejecting 206 raised out of ``_pull``, which aborted the whole sync and
        left the remaining tables (and other devices' rows) unpulled.
        """
        sync = self._sync()
        pages = [
            _resp(206, [{"id": i} for i in range(1000)], "0-999/2500"),
            _resp(206, [{"id": i} for i in range(1000, 2000)], "1000-1999/2500"),
            _resp(206, [{"id": i} for i in range(2000, 2500)], "2000-2499/2500"),
        ]
        with patch.object(cloud_sync_module.requests, "get",
                          side_effect=pages) as get:
            rows = sync._fetch_cloud(
                "time_records_cloud",
                "device_id,date,process_name",
                "2026-10-14",
            )

        self.assertEqual(len(rows), 2500)
        self.assertEqual(rows[0]["id"], 0)
        self.assertEqual(rows[-1]["id"], 2499)
        self.assertEqual(get.call_count, 3)

    def test_single_page_table_needs_one_request(self):
        sync = self._sync()
        rows_in = [{"id": i} for i in range(371)]
        with patch.object(cloud_sync_module.requests, "get",
                          return_value=_resp(200, rows_in, "0-370/371")) as get:
            rows = sync._fetch_cloud("ai_token_daily_cloud", "device_id", "2026-10-14")

        self.assertEqual(len(rows), 371)
        self.assertEqual(get.call_count, 1)

    def test_empty_table_returns_empty_list(self):
        sync = self._sync()
        with patch.object(cloud_sync_module.requests, "get",
                          return_value=_resp(200, [], "*/0")):
            rows = sync._fetch_cloud("tag_time_records_cloud", "device_id", "2026-10-14")
        self.assertEqual(rows, [])

    def test_real_error_status_still_raises(self):
        """A genuine failure (401/500/404) must still surface, not be swallowed."""
        sync = self._sync()
        for status in (400, 401, 403, 404, 500):
            with patch.object(cloud_sync_module.requests, "get",
                              return_value=_resp(status, None)):
                with self.assertRaises(RuntimeError):
                    sync._fetch_cloud("time_records_cloud", "device_id", "2026-10-14")

    def test_last_page_short_of_limit_still_returns_all_rows(self):
        """Exact multiple of limit: the third page is empty and stops the loop."""
        sync = self._sync()
        pages = [
            _resp(206, [{"id": i} for i in range(1000)], "0-999/2000"),
            _resp(206, [{"id": i} for i in range(1000, 2000)], "1000-1999/2000"),
        ]
        with patch.object(cloud_sync_module.requests, "get", side_effect=pages) as get:
            rows = sync._fetch_cloud("time_records_cloud", "device_id", "2026-10-14")

        self.assertEqual(len(rows), 2000)
        self.assertEqual(get.call_count, 2)

    def test_missing_content_range_falls_back_to_short_page(self):
        """When the server gives no count, a short page still ends the loop."""
        sync = self._sync()
        with patch.object(cloud_sync_module.requests, "get",
                          return_value=_resp(200, [{"id": 1}])) as get:
            rows = sync._fetch_cloud("tool_call_daily_cloud", "device_id", "2026-10-14")

        self.assertEqual(rows, [{"id": 1}])
        self.assertEqual(get.call_count, 1)

    def test_content_range_parser(self):
        parse = CloudSync._content_range_total
        self.assertEqual(parse("0-999/1724"), 1724)
        self.assertEqual(parse("2000-2499/2500"), 2500)
        # An empty table reports a known count of zero.
        self.assertEqual(parse("*/0"), 0)
        self.assertIsNone(parse("*"))
        self.assertIsNone(parse(None))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("garbage"))

    def test_pull_failure_does_not_skip_later_tables(self):
        """A pull error must not stop the remaining tables from being pulled.

        ``run_if_needed`` wraps ``_pull`` in one try, so today a failure in the
        first table silently starved tool calls and activity pulls. When a pull
        does fail the day must not be marked synced.
        """
        sync = self._sync()
        pulled = []
        # Failing pulls must not abort the ones after them; patch on the
        # instance so _pull's local references pick the stubs up.
        order = ["tag_time_records", "time_records", "ai_token_daily",
                 "tool_call_daily", "devin_activity_daily"]

        def make(n):
            def pull_one(include_date):
                pulled.append(n)
                if n == "time_records":
                    raise RuntimeError("boom")
            return pull_one

        for name in order:
            setattr(sync, f"_pull_{name}", make(name))
        sync._push = MagicMock()
        sync._config.last_sync_date = ""

        ok = sync.run_if_needed(force=True)

        self.assertEqual(pulled, order)
        self.assertFalse(ok)
        sync._config.set_last_sync_date.assert_not_called()


if __name__ == "__main__":
    unittest.main()
