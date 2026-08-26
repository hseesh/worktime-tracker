"""Force-refresh ai_token_daily + devin_activity_daily cache for all historical dates.

After changing the messages metric from agent_messages to user-sent messages,
historical cached rows still hold the old values. This script clears the local
device's ai_token + devin_activity cache and rebuilds every day from source data.

Usage:
    py _resync_ai_messages.py            # rebuild last 400 days
    py _resync_ai_messages.py 730        # rebuild last 730 days
"""
import sys
import os
import time
import json

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
        print(f"ai_token before: {r[0]} rows, dates {r[1]} .. {r[2]}")
        r2 = conn.execute(
            "SELECT COUNT(*) FROM cache_scan_state WHERE device_id=? AND kind='ai_token'",
            (device_id,),
        ).fetchone()
        print(f"ai_token before: {r2[0]} scanned dates")
        r3 = conn.execute("SELECT COUNT(*) FROM devin_activity_daily").fetchone()
        print(f"devin_activity before: {r3[0]} rows")
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
        deleted_activity = conn.execute("DELETE FROM devin_activity_daily").rowcount
        conn.commit()
        print(f"cleared: {deleted_rows} ai_token_daily rows, {deleted_scans} scan_state rows, {deleted_activity} devin_activity rows")
    finally:
        conn.close()

    # 3. Rebuild ai_token from source
    print(f"rescanning ai_token last {days} days from source data...")
    t0 = time.time()
    recorder.sync_ai_token_cache(days=days)
    print(f"ai_token rescan done in {time.time() - t0:.1f}s")

    # 4. Rebuild devin_activity from source
    print(f"rescanning devin_activity last {days} days from source data...")
    t0 = time.time()
    recorder.sync_devin_activity_cache(days=days)
    print(f"devin_activity rescan done in {time.time() - t0:.1f}s")

    # 5. Show result
    conn = recorder._conn()
    try:
        r = conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM ai_token_daily WHERE device_id=?",
            (device_id,),
        ).fetchone()
        print(f"ai_token after: {r[0]} rows, dates {r[1]} .. {r[2]}")
        nonzero = conn.execute(
            "SELECT COUNT(*) FROM ai_token_daily WHERE device_id=? AND messages>0",
            (device_id,),
        ).fetchone()[0]
        print(f"ai_token after: {nonzero} rows with messages>0")
        r3 = conn.execute("SELECT COUNT(*) FROM devin_activity_daily").fetchone()
        print(f"devin_activity after: {r3[0]} rows")
        # Check a sample title for messages field
        sample = conn.execute(
            "SELECT date, data_json FROM devin_activity_daily ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if sample:
            data = json.loads(sample["data_json"])
            titles = data.get("titles", [])
            print(f"devin_activity sample ({sample['date']}): {len(titles)} titles")
            for t in titles[:3]:
                print(f"  {t.get('title','')[:40]}: messages={t.get('messages','<missing>')}")
    finally:
        conn.close()

    print("\nDone. Run cloud sync to push corrected data.")


if __name__ == "__main__":
    main()
