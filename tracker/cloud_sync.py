"""Cloud sync via Supabase REST API.

Pushes this device's daily aggregates (including today) to Supabase and pulls
all devices' data back, merging into local SQLite. Runs at startup and every
30 minutes while enabled.

Design:
- Uses the publishable (anon) key — safe to embed in a desktop app.
- RLS allows the anon role full access (see schema_supabase.sql).
- Syncs time, tag, AI token and tool-call daily aggregates.
- Does NOT sync time_segments (per-second data, too large for 500MB free tier).
- Conflict policy: cloud-priority (UPSERT overwrites). Each device owns its
  own rows keyed by device_id, so there is no cross-device conflict.
- Multi-device merge: local queries SUM across all device_ids, so pulled
  data from other devices naturally adds up in the UI.
"""

import logging
import threading
from datetime import date
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

# Supabase REST API endpoints
REST_BASE = "/rest/v1"


def _strip_tz(ts: str) -> str:
    """Remove timezone suffix from an ISO timestamp (e.g. '2026-08-19T10:00:00+00:00' -> '2026-08-19T10:00:00')."""
    if not ts:
        return ""
    # Strip '+00:00' or 'Z' suffix to match local SQLite format
    for suffix in ("+00:00", "+0000"):
        if ts.endswith(suffix):
            return ts[: -len(suffix)]
    if ts.endswith("Z"):
        return ts[:-1]
    return ts


class CloudSync:
    """Handles push/pull of daily aggregates to/from Supabase."""

    def __init__(self, config, recorder):
        """Args:
            config: AppConfig instance with supabase config.
            recorder: TimeRecorder instance with device_id set.
        """
        self._config = config
        self._recorder = recorder
        self._url = config.supabase.get("url", "").rstrip("/")
        self._anon_key = config.supabase.get("anon_key", "")
        self._device_id = config.device_id
        self._sync_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._config.supabase_enabled

    def _headers(self, prefer: str = "") -> Dict[str, str]:
        h = {
            "apikey": self._anon_key,
            "Content-Type": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    def run_if_needed(self, force: bool = False) -> bool:
        """Run sync if not already done today. Returns True if sync ran.

        When *force* is True, skips the once-per-day guard (used by the
        periodic 30-minute timer to push today's growing data).
        """
        if not self.enabled:
            logger.info("Cloud sync is disabled, skipping.")
            return False

        if not self._sync_lock.acquire(blocking=False):
            logger.info("Cloud sync already running, skipping overlapping request.")
            return False

        try:
            today = date.today().isoformat()
            if not force and self._config.last_sync_date == today:
                logger.info("Cloud sync already done today (%s), skipping.", today)
                return False

            logger.info("Starting cloud sync (device_id=%s, force=%s)...",
                        self._device_id[:8], force)
            try:
                self._push()
                self._pull()
                self._config.set_last_sync_date(today)
                logger.info("Cloud sync completed successfully.")
                return True
            except Exception as e:
                logger.error("Cloud sync failed: %s", e, exc_info=True)
                return False
        finally:
            self._sync_lock.release()

    # ------------------------------------------------------------------ #
    #  Push: upload this device's historical data to cloud               #
    # ------------------------------------------------------------------ #

    def _push(self):
        """Upload this device's time_records, tag_time_records, ai_token_daily and tool_call_daily (date <= today)."""
        today = date.today().isoformat()
        self._push_tag_time_records(today)
        self._push_time_records(today)
        self._push_ai_token_daily(today)
        self._push_tool_call_daily(today)

    def _push_tag_time_records(self, include_date: str):
        rows = self._recorder.get_local_tag_time_records_for_sync(include_date)
        if not rows:
            logger.info("No tag_time_records to push.")
            return

        payload = [
            {
                "device_id": r["device_id"],
                "date": r["date"],
                "tag": r["tag"],
                "seconds": r["seconds"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        self._upsert_cloud("tag_time_records_cloud", payload)
        logger.info("Pushed %d tag_time_records rows.", len(payload))

    def _push_time_records(self, include_date: str):
        rows = self._recorder.get_local_time_records_for_sync(include_date)
        if not rows:
            logger.info("No time_records to push.")
            return

        payload = [
            {
                "device_id": r["device_id"],
                "date": r["date"],
                "process_name": r["process_name"],
                "display_name": r["display_name"],
                "project": r["project"],
                "tag": r["tag"],
                "seconds": r["seconds"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        self._upsert_cloud("time_records_cloud", payload)
        logger.info("Pushed %d time_records rows.", len(payload))

    def _push_ai_token_daily(self, include_date: str):
        rows = self._recorder.get_local_ai_token_daily_for_sync(include_date)
        if not rows:
            logger.info("No ai_token_daily rows to push.")
            return

        payload = [
            {
                "device_id": r["device_id"],
                "date": r["date"],
                "source": r["source"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "cached_tokens": r["cached_tokens"],
                "sessions": r["sessions"],
                "messages": r["messages"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        self._upsert_cloud("ai_token_daily_cloud", payload)
        logger.info("Pushed %d ai_token_daily rows.", len(payload))

    def _push_tool_call_daily(self, include_date: str):
        rows = self._recorder.get_local_tool_call_daily_for_sync(include_date)
        if not rows:
            logger.info("No tool_call_daily rows to push.")
            return

        payload = [
            {
                "device_id": r["device_id"],
                "date": r["date"],
                "category": r["category"],
                "name": r["name"],
                "count": r["count"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
        self._upsert_cloud("tool_call_daily_cloud", payload)
        logger.info("Pushed %d tool_call_daily rows.", len(payload))

    def _upsert_cloud(self, table: str, payload: List[Dict]):
        """Upsert rows to a Supabase table via REST API.

        Uses Prefer: resolution=merge-duplicates to upsert on conflict.
        The on_conflict query param specifies the unique constraint columns.
        """
        if not payload:
            return

        # Determine conflict columns per table
        if table == "tag_time_records_cloud":
            on_conflict = "device_id,date,tag"
        elif table == "time_records_cloud":
            on_conflict = "device_id,date,process_name,project"
        elif table == "ai_token_daily_cloud":
            on_conflict = "device_id,date,source"
        elif table == "tool_call_daily_cloud":
            on_conflict = "device_id,date,category,name"
        else:
            raise ValueError(f"Unknown table: {table}")

        url = f"{self._url}{REST_BASE}/{table}?on_conflict={on_conflict}"
        headers = self._headers(prefer="resolution=merge-duplicates,return=minimal")

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Supabase upsert to {table} failed: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )

    # ------------------------------------------------------------------ #
    #  Pull: download all devices' historical data from cloud            #
    # ------------------------------------------------------------------ #

    def _pull(self):
        """Pull all devices' data for date <= today and merge into local SQLite."""
        today = date.today().isoformat()
        self._pull_tag_time_records(today)
        self._pull_time_records(today)
        self._pull_ai_token_daily(today)
        self._pull_tool_call_daily(today)

    def _pull_tag_time_records(self, include_date: str):
        rows = self._fetch_cloud(
            "tag_time_records_cloud",
            select="device_id,date,tag,seconds,updated_at",
            lte_date=include_date,
        )
        if not rows:
            logger.info("No tag_time_records rows to pull.")
            return

        for row in rows:
            row["updated_at"] = _strip_tz(row.get("updated_at", ""))
            self._recorder.upsert_cloud_tag_time_record(row)
        logger.info("Pulled and merged %d tag_time_records rows.", len(rows))

    def _pull_time_records(self, include_date: str):
        rows = self._fetch_cloud(
            "time_records_cloud",
            select="device_id,date,process_name,display_name,project,tag,seconds,updated_at",
            lte_date=include_date,
        )
        if not rows:
            logger.info("No time_records rows to pull.")
            return

        for row in rows:
            row["updated_at"] = _strip_tz(row.get("updated_at", ""))
            self._recorder.upsert_cloud_time_record(row)
        logger.info("Pulled and merged %d time_records rows.", len(rows))

    def _pull_ai_token_daily(self, include_date: str):
        rows = self._fetch_cloud(
            "ai_token_daily_cloud",
            select="device_id,date,source,input_tokens,output_tokens,cached_tokens,sessions,messages,updated_at",
            lte_date=include_date,
        )
        if not rows:
            logger.info("No ai_token_daily rows to pull.")
            return

        for row in rows:
            row["updated_at"] = _strip_tz(row.get("updated_at", ""))
            self._recorder.upsert_cloud_ai_token_daily(row)
        logger.info("Pulled and merged %d ai_token_daily rows.", len(rows))

    def _pull_tool_call_daily(self, include_date: str):
        rows = self._fetch_cloud(
            "tool_call_daily_cloud",
            select="device_id,date,category,name,count,updated_at",
            lte_date=include_date,
        )
        if not rows:
            logger.info("No tool_call_daily rows to pull.")
            return

        for row in rows:
            row["updated_at"] = _strip_tz(row.get("updated_at", ""))
            self._recorder.upsert_cloud_tool_call_daily(row)
        logger.info("Pulled and merged %d tool_call_daily rows.", len(rows))

    def _fetch_cloud(self, table: str, select: str, lte_date: str) -> List[Dict]:
        """Fetch rows from a Supabase table where date <= lte_date.

        Supabase REST API returns paginated results. We follow the
        Pagination-Range / Link header or use limit + offset.
        """
        all_rows: List[Dict] = []
        limit = 1000
        offset = 0

        while True:
            url = f"{self._url}{REST_BASE}/{table}"
            params = {
                "select": select,
                "date": f"lte.{lte_date}",
                "limit": limit,
                "offset": offset,
            }
            headers = self._headers()
            # Request count to know when to stop
            headers["Prefer"] = "count=exact"

            resp = requests.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Supabase fetch from {table} failed: "
                    f"HTTP {resp.status_code} {resp.text[:200]}"
                )

            batch = resp.json()
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < limit:
                break
            offset += limit

        return all_rows
