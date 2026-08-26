"""Force-refresh ai_token_daily cache for all historical dates.

After changing the messages metric from agent_messages to user-sent messages,
historical cached rows still hold the old values. This script clears the local
device's ai_token cache scan state and rebuilds every day from source data.

Usage:
    py _resync_ai_messages.py            # rebuild last 400 days
    py _resync_ai_messages.py 730        # rebuild last 730 days
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import AppConfig
from tracker.time_recorder import TimeRecorder


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 400

    config = AppConfig()
    device_id = config.device_id
    print(f"device_id={device_id}")

    recorder = TimeRecorder(device_id=device_id)

    # 1. Show current state
    conn = recorder._conn()
    try:
        r = conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM ai_token_daily WHERE device_id=?",
            (device_id,),
        ).fetchone()
        print(f"before: {r[0]} rows, dates {r[1]} .. {r[2]}")
        r2 = conn.execute(
            "SELECT COUNT(*) FROM cache_scan_state WHERE device_id=? AND kind='ai_token'",
            (device_id,),
        ).fetchone()
        print(f"before: {r2[0]} scanned dates")
    finally:
        conn.close()

    # 2. Clear local device's ai_token cache + scan state so sync rescans everything
    conn = recorder._conn()
    try:
        deleted_rows = conn.execute(
            "DELETE FROM ai_token_daily WHERE device_id=?", (device_id,)
        ).rowcount
        deleted_scans = conn.execute(
            "DELETE FROM cache_scan_state WHERE device_id=? AND kind='ai_token'",
            (device_id,),
        ).rowcount
        conn.commit()
        print(f"cleared: {deleted_rows} ai_token_daily rows, {deleted_scans} scan_state rows")
    finally:
        conn.close()

    # 3. Rebuild from source
    print(f"rescanning last {days} days from source data...")
    t0 = time.time()
    recorder.sync_ai_token_cache(days=days)
    elapsed = time.time() - t0
    print(f"rescan done in {elapsed:.1f}s")

    # 4. Show result
    conn = recorder._conn()
    try:
        r = conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM ai_token_daily WHERE device_id=?",
            (device_id,),
        ).fetchone()
        print(f"after: {r[0]} rows, dates {r[1]} .. {r[2]}")
        nonzero = conn.execute(
            "SELECT COUNT(*) FROM ai_token_daily WHERE device_id=? AND messages>0",
            (device_id,),
        ).fetchone()[0]
        print(f"after: {nonzero} rows with messages>0")
        print("\nsample (last 10 rows):")
        for row in conn.execute(
            "SELECT date, source, sessions, messages FROM ai_token_daily "
            "WHERE device_id=? ORDER BY date DESC, source LIMIT 10",
            (device_id,),
        ).fetchall():
            print(f"  {row['date']} {row['source']}: sessions={row['sessions']} messages={row['messages']}")
    finally:
        conn.close()

    print("\nDone. Run cloud sync to push corrected data.")


if __name__ == "__main__":
    main()
