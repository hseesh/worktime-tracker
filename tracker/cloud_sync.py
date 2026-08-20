"""Cloud sync via Supabase REST API.

Pushes this device's historical daily aggregates (date < today) to Supabase
and pulls all devices' data back, merging into local SQLite. Runs once per
day on first startup.

Design:
- Uses the publishable (anon) key — safe to embed in a desktop app.
- RLS allows the anon role full access (see schema_supabase.sql).
- Only syncs tag_time_records and time_records (daily aggregates).
- Does NOT sync time_segments (per-second data, too large for 500MB free tier).
- Conflict policy: cloud-priority (UPSERT overwrites). Each device owns its
  own rows keyed by device_id, so there is no cross-device conflict.
- Multi-device merge: local queries SUM across all device_ids, so pulled
  data from other devices naturally adds up in the UI.
"""

import logging
from datetime import date
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

# Supabase REST API endpoints
REST_BASE = "/rest/v1"


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

    def run_if_needed(self) -> bool:
        """Run sync if not already done today. Returns True if sync ran."""
        if not self.enabled:
            logger.info("Cloud sync is disabled, skipping.")
            return False

        today = date.today().isoformat()
        if self._config.last_sync_date == today:
            logger.info("Cloud sync already done today (%s), skipping.", today)
            return False

        logger.info("Starting cloud sync (device_id=%s)...", self._device_id[:8])
        try:
            self._push()
            self._pull()
            self._config.set_last_sync_date(today)
            logger.info("Cloud sync completed successfully.")
            return True
        except Exception as e:
            logger.error("Cloud sync failed: %s", e, exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    #  Push: upload this device's historical data to cloud               #
    # ------------------------------------------------------------------ #

    def _push(self):
        """Upload this device's time_records and tag_time_records (date < today)."""
        today = date.today().isoformat()
        self._push_tag_time_records(today)
        self._push_time_records(today)

    def _push_tag_time_records(self, before_date: str):
        rows = self._recorder.get_local_tag_time_records_for_sync(before_date)
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

    def _push_time_records(self, before_date: str):
        rows = self._recorder.get_local_time_records_for_sync(before_date)
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
        """Pull all devices' data for date < today and merge into local SQLite."""
        today = date.today().isoformat()
        self._pull_tag_time_records(today)
        self._pull_time_records(today)

    def _pull_tag_time_records(self, before_date: str):
        rows = self._fetch_cloud(
            "tag_time_records_cloud",
            select="device_id,date,tag,seconds,updated_at",
            lt_date=before_date,
        )
        if not rows:
            logger.info("No tag_time_records rows to pull.")
            return

        for row in rows:
            self._recorder.upsert_cloud_tag_time_record(row)
        logger.info("Pulled and merged %d tag_time_records rows.", len(rows))

    def _pull_time_records(self, before_date: str):
        rows = self._fetch_cloud(
            "time_records_cloud",
            select="device_id,date,process_name,display_name,project,tag,seconds,updated_at",
            lt_date=before_date,
        )
        if not rows:
            logger.info("No time_records rows to pull.")
            return

        for row in rows:
            self._recorder.upsert_cloud_time_record(row)
        logger.info("Pulled and merged %d time_records rows.", len(rows))

    def _fetch_cloud(self, table: str, select: str, lt_date: str) -> List[Dict]:
        """Fetch rows from a Supabase table where date < lt_date.

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
                "date": f"lt.{lt_date}",
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
