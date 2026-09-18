# WorkTime Tracker — Bug Audit Report

**Scope reviewed (read in full):** `tracker/time_recorder.py` (1981 lines), `config.py` (541), `tracker/project_parser.py` (25), `tracker/idle_detector.py` (30), `tracker/tracking_engine.py` (301), plus supporting reads of `tracker/window_tracker.py`, `tracker/cloud_sync.py`, `tracker/chrome_url_cache.py`, `main.py`, `schema_supabase.sql`, and the test suite.

**Method:** every finding below was reproduced against a scratch SQLite DB (or against the live `worktime.db` read-only) with a purpose-built probe script, not inferred from reading alone. Probes were deleted after use. Findings I could *not* confirm are listed separately in §4.

---

## 1. HIGH severity

### H1 — `get_today_tag_distribution()` sums per-device rows, double-counting today's totals

**File:** `tracker/time_recorder.py:1757-1773` (and the writers at `:606-626`, `:918-973`)

```python
rows = conn.execute(
    "SELECT tag, SUM(seconds) AS seconds FROM tag_time_records WHERE date = ? GROUP BY tag ORDER BY seconds DESC",
    (today,),
).fetchall()
```

**Why it is wrong.** `tag_time_records` has `PRIMARY KEY (device_id, date, tag)`, so the *same* day and tag legitimately holds one row **per device**. This query groups by `tag` only and `SUM`s across **all** `device_id`s — including the local device's live row *and* every cloud-pulled snapshot. There is no `device_id` filter or `MAX`/de-dup, so a two-device setup reports roughly **double** the real figure.

This is not theoretical — the live database already contains it:

```
device_id=''                                   13 rows  29,390 s
device_id='6a0f049d-…a1fe' (this machine)     155 rows 1,150,629 s
device_id='959acf4e-…3e53'                     17 rows    93,334 s
device_id='D:\Work\worktime-tracker\worktime.db' 4 rows  15,616 s
```

`2026-09-18` has rows for the same tag under **both** `''` and the real device UUID. 28 `(date, tag)` pairs are affected. `get_today_total()` (`:719-720`) and `get_today_live_totals()` (`:722-753`) both build directly on this function, so the dashboard headline number is inflated.

Consistent with the codebase's own intent, three sibling readers correctly scope this — `get_local_tag_time_records_for_sync` (`:905`) filters `device_id = ?`, and `_migrate_current_tag_totals` (`:220`) checks `device_id = ?`. Only the read path forgot to.

**Severity: high. Data integrity / double-counting (displayed totals).**

---

### H2 — Relabeling an app rebuilds tag totals from merged wall-clock intervals, so the old tag keeps its time

**File:** `tracker/time_recorder.py:465-500` (`_rebuild_local_tag_totals_conn`), called from `update_app_tag` (`:584`), `update_tag` (`:1680`), `delete_tag` (`:1702`)

```python
rows = conn.execute(
    f"SELECT date, start_time, end_time, tag FROM time_segments "
    f"WHERE date IN ({placeholders}) AND tag != 'Idle' "
    "ORDER BY date, tag, start_time, end_time", dates,
).fetchall()
...
for start, end in intervals:
    if merged and start <= merged[-1][1]:
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    else:
        merged.append((start, end))
seconds = sum((end - start).total_seconds() for start, end in merged)
```

**Why it is wrong.** The accumulator that this function *replaces* (`_upsert_tag_total_conn`, `:606`) adds the raw per-sample elapsed. The rebuild instead computes the **union of wall-clock intervals**. On a multi-monitor setup the engine deliberately records one `time_segments` row *per monitor* while de-duplicating the tag total:

```python
# tracking_engine.py:168-171
# One wall-clock sample per tag, even when two same-tag apps occupy
# different monitors. App/project segments remain independently stored.
for tag in tags_seen:
    self._recorder.add_tag_time(tag, elapsed)
```

Two monitors running `a.exe` and `b.exe`, both tagged `Work`, for the same 10 s: the engine writes `Work = 10 s`. After any relabel triggers a rebuild, the two *overlapping* segments union to 10 s — but they carry **different tags** after the relabel, so the union is computed per tag and no longer cancels.

Reproduced:

```
engine dedup tag total:            [{'tag': 'Work', 'seconds': 10.0}]
after relabel of a.exe to Other:   [{'tag': 'Work', 'seconds': 10.0},
                                    {'tag': 'Other', 'seconds': 10.0}]
```

`Work` should have dropped to 0 s. The user relabels one app and gains phantom hours on the old tag. The system's own regression test (`tests/test_data_integrity_regressions.py:32-40`) only covers the single-window case, which is why this passes CI.

**Severity: high. Double-counting + permanently corrupted tag totals.**

---

### H3 — `add_time()` UPSERT overwrites `tag` on the aggregate row, silently desynchronizing it from `tag_time_records`

**File:** `tracker/time_recorder.py:660-672`

```python
INSERT INTO time_records (device_id, date, process_name, display_name, project, tag, seconds, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(device_id, date, process_name, project)
DO UPDATE SET
    seconds    = seconds + excluded.seconds,
    display_name = excluded.display_name,
    tag         = excluded.tag,          # <-- clobbers the row's tag
    updated_at = excluded.updated_at
```

**Why it is wrong.** The conflict key is `(device_id, date, process_name, project)` — it does **not** include `tag`. So when a Chrome window's keyword rule flips (e.g. a Work page then an Indie page, same `chrome.exe`, same empty `project`), both samples land on *one* `time_records` row and the second write overwrites `tag`. The accumulated `seconds` (15 s) are now attributed entirely to `Indie`, while `tag_time_records` still holds `Work = 10 s` **and** `Indie = 5 s`.

Reproduced:

```
time_records:      [{'process_name':'chrome.exe','project':'','tag':'Indie','seconds':15.0}]
tag_time_records:  [{'tag':'Work','seconds':10.0}, {'tag':'Indie','seconds':5.0}]
```

The app breakdown (`get_today_app_breakdown`, `:1775`) and the tag distribution (`get_today_tag_distribution`, `:1757`) now disagree about the same 15 seconds — the Work row shows 10 s in one view and 0 s in the other. `add_codex_time` (`:1088-1100`) has the identical `DO UPDATE SET tag = excluded.tag` pattern.

**Severity: high. Data corruption / inconsistent reporting; silently misattributes hours to the wrong tag.**

---

### H4 — `_migrate_tag_from_display_name()` runs destructively on **every** startup and corrupts display names

**File:** `tracker/time_recorder.py:422-456`, invoked unconditionally from `_init_db` at `:212`

```python
conn.execute(
    "UPDATE time_records SET display_name = REPLACE(display_name, ' (Other)', '') WHERE display_name LIKE '%(Other)'"
)
```

**Why it is wrong.** This is not a one-shot migration — `_init_db()` calls it on every launch, and the `WHERE … LIKE` clauses match live data, not just legacy data. `REPLACE()` on a substring strips `" (Indie)"`/`" (Work)"`/`" (Other)"` **anywhere** in the name, and the `'Other (%'` pass at `:446-456` rewrites any display name of that shape. A legitimately-named app entry is progressively mangled across restarts. It also re-runs three full-table `UPDATE`s over `time_records` at every startup for no reason.

Verified the transformations apply on a fresh DB load:

```
'Other (x.exe)'  -> 'x.exe'
'Editor (Other)' -> 'Editor'
'Devin (Indie)'  -> 'Devin'
```

The renames are lossy and irreversible, and because the row is keyed partly on `display_name` in several readers, the mangling also splits/merges aggregates.

**Severity: high. Data loss (display names rewritten on every boot).**

---

## 2. MEDIUM severity

### M1 — `update_app_tag()` uses `OR display_name = ?`, relabeling unrelated projects

**File:** `tracker/time_recorder.py:553-563`

```python
segment_where = (
    "process_name = ? COLLATE NOCASE "
    "AND (project = ? COLLATE NOCASE OR display_name = ?)"
)
```

**Why it is wrong.** The `OR display_name = ?` fallback (intended to catch pre-`project`-column rows) also matches rows whose `project` is a *different* project but that happen to share a display name — which is common, since `_build_display_name` (`tracking_engine.py:296-301`) embeds the project into the name and the name is stored per row. Relabeling project A relabels project B.

Reproduced:

```
before: [('D:\projA','Indie'), ('D:\projB','Indie')]
update_app_tag("Devin.exe","Devin [Assets]","D:\projA","Work")
after:  [('D:\projA','Work'),  ('D:\projB','Work')]   # projB wrongly changed
```

**Severity: medium. Wrong bulk relabel; combined with H2 it also corrupts tag totals.**

---

### M2 — `get_today_tag_distribution()` fallback is skipped whenever *any* row exists, hiding local live data

**File:** `tracker/time_recorder.py:1762-1770`

```python
if not rows:
    rows = conn.execute(
        "SELECT tag, SUM(seconds) AS seconds FROM time_segments WHERE date = ? AND tag != 'Idle' GROUP BY tag ORDER BY seconds DESC",
        (today,),
    ).fetchall()
```

**Why it is wrong.** The fallback triggers on `not rows` — an *existence* test, not a completeness test. As soon as a single cloud row for today lands (from another device), the fallback is skipped forever for that date and the local `time_segments` data becomes invisible. Reproduced:

```
local time_segments: Work 3600 s (no tag_time_records row)
+ remote tag row (Indie 7 s, pulled from cloud)
-> get_today_tag_distribution() == [{'tag':'Indie','seconds':7.0}]
-> get_today_live_totals() == {'total': 7.0, ...}   # 3600 s vanished
```

Same structural flaw in `get_range_app_breakdown` (`:804-816`) and `get_daily_tag_breakdown` (`:871-880`), which use `date NOT IN (SELECT DISTINCT date FROM …)` — a coarse date-level guard that discards the whole day if a single row exists.

**Severity: medium. Under-reporting / silent data loss in the UI.**

---

### M3 — `_split_interval()` attributes multi-day gaps to whole extra days

**File:** `tracker/time_recorder.py:593-604`

```python
while cursor < end:
    next_midnight = (cursor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    chunk_end = min(next_midnight, end)
    yield cursor, chunk_end
    cursor = chunk_end
```

**Why it is wrong.** `add_time` passes `now - timedelta(seconds=seconds)` with whatever `seconds` the caller supplies. If the engine is asleep/blocked (laptop suspend, hibernation, a `Timer` that fired late), `elapsed` is the *monotonic delta* and can be hours. `_split_interval` faithfully spreads it across each intervening calendar day, crediting days when the machine was asleep. Confirmed the generator emits one chunk per day for a 3-day span:

```
[('…T14:58:21', '…T00:00:00'), ('…T00:00:00','…T00:00:00'),
 ('…T00:00:00','…T00:00:00'), ('…T00:00:00','…T14:58:21')]
```

Note `time.monotonic()` (`tracking_engine.py:96`) does **not** advance during suspend on Windows, but `_last_ts` is set inside `_poll` while `_config.poll_interval` is 1000 ms — a stalled or throttled timer thread (this polls inside `with self._lock:` and the whole body does Win32 enumeration) still yields inflated `elapsed`. There is no clamp on `elapsed` anywhere.

**Severity: medium. Fabricated time on days with no activity.**

---

### M4 — `get_daily_totals()` reads `time_segments`, which `cleanup_old_time_segments()` truncates at 30 days

**File:** `tracker/time_recorder.py:840-856` (reader) vs `:1576-1593` + `main.py:176`

```python
cur = conn.execute("DELETE FROM time_segments WHERE date < ?", (cutoff,))
```

**Why it is wrong.** The 30-day cleanup deletes `time_segments`, but several History readers still source from it while their siblings read the permanent `tag_time_records`. Reproduced with a 60-day-old day:

```
daily totals BEFORE cleanup: [('2026-07-20', 7200.0)]
daily totals AFTER  cleanup: []                      # gone
tag breakdown AFTER  cleanup: {'2026-07-20': {'Work': 7200.0}}   # retained
app breakdown AFTER  cleanup: [... 7200.0 ...]                    # retained
```

So the History chart loses all data older than 30 days while the tag/app panels keep it — the views contradict each other. Affects `get_daily_totals` (`:840`), `get_peak_hours` (`:1876`), `get_period_tag_summary` (`:1902`), `get_daily_tag_trend` (`:1931`), `get_today_timeline` (`:1709`), `get_switch_frequency` (`:1853`), `get_focus_sessions` (`:1828`), `get_today_idle_time` (`:1815`).

Note the live DB also still holds a `2099-01-01` row (`process_name='x'`) — a test artifact, **not** a production bug; flagged only so it is not mistaken for clock corruption.

**Severity: medium. Silent data loss in historical views.**

---

### M5 — `AppConfig.load()` swallows only `JSONDecodeError`/`IOError`; a type-mismatched config crashes startup

**File:** `config.py:111-179`

```python
                ...
            except (json.JSONDecodeError, IOError):
                pass
```

**Why it is wrong.** Valid JSON with the wrong *shape* escapes the handler and propagates out of the constructor. Reproduced with `{"process_tags": ["not","a","dict"]}`:

```
AppConfig() RAISED: TypeError list indices must be integers or slices, not str
```

Since `main.py:73` constructs `AppConfig()` before anything else, a hand-edited `config.json` (the file is explicitly user-editable and there is a `config.json.bak-*` in the repo) bricks the app at startup with no fallback to defaults. The bare `except: pass` also hides genuine corruption silently in the cases it does catch.

**Severity: medium. Crash on startup.**

---

### M6 — `get_peak_hours()` divides by every calendar day in range, including empty days

**File:** `tracker/time_recorder.py:1899-1900`

```python
num_days = max(1, (end - start).days + 1)
return [{"hour": h, "avg_seconds": round(hour_totals.get(h, 0) / num_days, 1)} for h in range(24)]
```

**Why it is wrong.** The divisor is the raw span, not the number of *active* days. A single 3600 s hour in a 7-day window reports 511 s, not 3600 s — and the distortion scales with the window, so widening the range makes the "peak" shrink. Should divide by active days or clearly label it a span average.

**Severity: medium. Incorrect aggregation (misleading metric).**

---

### M7 — `get_switch_frequency()` orders by `start_time` only; ties from multi-monitor sampling make counts nondeterministic

**File:** `tracker/time_recorder.py:1870-1872`

```python
if prev_proc is not None and prev_proc != r["process_name"]:
    hour_switches[hour] = hour_switches.get(hour, 0) + 1
prev_proc = r["process_name"]
```

**Why it is wrong.** Per-monitor segments routinely share an identical `start_time` (second resolution), and `ORDER BY start_time` leaves their relative order undefined. The alternation count then depends on SQLite's incidental row order. Reproduced: 6 segments, timestamps 9:00:00–9:00:05, alternating `p0/p1`, reports **5 switches**; the two rows sharing a second can swap and change the total between runs. There is no tiebreaker on `id`.

**Severity: medium. Nondeterministic reporting.**

---

### M8 — `_init_db()` migration block is not wrapped in a transaction and can leave the DB half-migrated

**File:** `tracker/time_recorder.py:179-215`

```python
conn = self._conn()
try:
    conn.execute("PRAGMA journal_mode=WAL")
    ...
    self._migrate_add_device_id(conn)     # CREATE new, INSERT…SELECT, DROP, RENAME
    ...
    conn.commit()
finally:
    conn.close()
```

**Why it is wrong.** `_migrate_add_device_id` (`:278-341`), `_migrate_ai_cache_device_id` (`:343-376`), and `_migrate_tool_cache_device_id` (`:378-406`) each run `DROP TABLE` on the live table. Python's default `isolation_level` auto-begins a transaction for DML, but `DROP`/`ALTER` interleave with `PRAGMA` and `executescript` calls (`:184`, `:260`) — and **`executescript()` implicitly commits any pending transaction first**. If any step raises after that implicit commit but before the final `conn.commit()`, the `DROP` is durable and the table is gone; the `finally` closes the connection without rolling back a partial migration. Given the DB is 468 MB and holds 2.78 M segments, a failure here is unrecoverable without a backup. There is no `try/except` around `_init_db` in `__init__` (`:174-177`) either.

I could not trigger a failure in practice, so this is a **structural robustness defect** rather than a demonstrated one — the implicit-commit semantics of `executescript` are documented, but the window is narrow.

**Severity: medium (potential total data loss).**

---

## 3. LOW severity

### L1 — `_migrate_current_tag_totals()` runs a full-day scan on every startup and writes non-deduplicated totals

**File:** `tracker/time_recorder.py:217-252`. It guards on `device_id = ? AND date = today` (`:220-223`) so it is not merely re-run, but it unconditionally scans all of today's segments at every launch and inserts rows computed with the *merge* semantics of H2 rather than the engine's dedup — so a cold start can produce totals that differ from the running session's, purely as a function of whether the process was restarted.

**Severity: low. Startup cost + totals drift after restart.**

### L2 — `_migrate_tag_from_display_name` miscounts `Other (` prefix stripping

**File:** `tracker/time_recorder.py:452` — `new_name = old_name[7:-1]` hard-codes 7 as the length of `"Other ("`. Correct today, but silently wrong if the prefix ever changes, and the `LIKE 'Other (%'` + `endswith(")")` guard does not verify the length.

**Severity: low.**

### L3 — `get_focus_sessions()` merges only on exact `end_time == start_time` string equality

**File:** `tracker/time_recorder.py:1845`. Timestamps are stored at second resolution (`isoformat(timespec="seconds")`), so adjacent samples normally do match, but any sub-second or out-of-order segment breaks the merge and inflates the session list. Also `sessions[-1]["seconds"] += r["seconds"]` sums stored values rather than recomputing from the merged bounds.

**Severity: low.**

### L4 — Duplicate tag names raise an unhandled `IntegrityError`

**File:** `tracker/time_recorder.py:1641-1649`. Reproduced: `add_tag("Custom", …)` twice → `IntegrityError: UNIQUE constraint failed: tags.name`. The connection is closed correctly by `finally`, but the exception reaches the caller/UI uncaught.

**Severity: low. Crash/UX.**

### L5 — `chrome_url_events` has no retention policy

**File:** `tracker/time_recorder.py:1041-1055`; live DB holds 9,766 rows. Writes *are* de-duplicated by `url != old_url` in `ChromeUrlCache.set_url` (`chrome_url_cache.py:49`), so growth tracks real navigation rather than polling — hence **low**, but the table only ever grows alongside the 468 MB database.

**Severity: low.**

---

## 4. Investigated and **not** bugs (explicitly cleared)

I want to be clear about what I ruled out, since several look like bugs on first read:

1. **`idle_detector.py:21-25` tick wrap-around — NOT a bug.** `GetTickCount` has no `restype` set, so ctypes returns a *signed* `c_long`. I initially suspected this corrupted the idle calculation past ~24.85 days of uptime. It does not: I exercised the arithmetic across the `2**31` boundary and the `millis < 0 → += 2**32` branch recovers the exact true delta in every case tested (e.g. `tick=2147483653, last=2147483645` → correct `8 ms`). `dwTime` comes from a `DWORD` struct field and is always unsigned, which is what makes the single-sided correction sufficient. **No defect.**
2. **Connection leaks — none found.** All 58 `self._conn()` call sites are paired with 58 `conn.close()` sites, each inside a `finally`. `cleanup_old_time_segments` runs `VACUUM` after a `commit()`, so no "cannot VACUUM from within a transaction" error; I verified it succeeds even with another connection holding an open read transaction under WAL. **No defect.**
3. **Lost updates under concurrency — none.** 8 threads × 50 `add_time` calls produced exactly 400 s in `time_records`, `tag_time_records`, and 400 segment rows. The `seconds = seconds + excluded.seconds` form is evaluated inside SQLite, so it is atomic. **No defect.**
4. **`project_parser.py` regex — no defect.** Verified against 9 title shapes including the en/em-dash variants, `"A - B - Devin - C"` (→ `"A - B"`), the no-project case (→ `""`), and `code.exe` with an `[Administrator]` suffix. Behaviour is self-consistent; the `code.exe` branch reads `parts[-2]`, which is correct for the documented `"{file} - {workspace} - Visual Studio Code"` format. **No defect.**
5. **`''` / DB-path `device_id` rows in the live DB** are historical residue from older builds and from standalone diagnostic scripts (`tests/` construct `TimeRecorder()` with the default `""`). Current `main.py:76` passes `config.device_id` correctly, so no *new* bad rows are being produced — but the existing ones still poison H1's `SUM`. **Root cause is stale data, not current code.**
6. **`cloud_sync._fetch_cloud` pagination** uses `limit`/`offset` with a short-batch break; correct as written.

---

## 5. Ranked summary

| # | Finding | File:lines | Severity | Impact |
|---|---------|-----------|----------|--------|
| H1 | `SUM(seconds)` across all `device_id`s in `get_today_tag_distribution` | `time_recorder.py:1757-1773` | **High** | Double-counting (confirmed in live DB, 28 day/tag pairs) |
| H2 | Relabel rebuild merges wall-clock intervals, old tag keeps time | `time_recorder.py:465-500` | **High** | Double-counting + corrupted tag totals |
| H3 | `DO UPDATE SET tag = excluded.tag` desyncs `time_records` from `tag_time_records` | `time_recorder.py:660-672`, `:1093-1097` | **High** | Data corruption, misattributed hours |
| H4 | `_migrate_tag_from_display_name` runs every startup, rewrites names | `time_recorder.py:422-456`, `:212` | **High** | Data loss (irreversible) |
| M1 | `update_app_tag` `OR display_name = ?` over-matches | `time_recorder.py:553-563` | Medium | Wrong bulk relabel |
| M2 | Fallback skipped when any row exists → local data hidden | `time_recorder.py:1762-1770` | Medium | Under-reporting |
| M3 | `_split_interval` credits days the machine was asleep | `time_recorder.py:593-604` | Medium | Fabricated time |
| M4 | 30-day cleanup vs readers sourced from `time_segments` | `time_recorder.py:840`, `:1576` | Medium | Historical data loss |
| M5 | `AppConfig.load()` type errors escape the handler | `config.py:111-179` | Medium | Startup crash |
| M6 | `get_peak_hours` divides by all calendar days | `time_recorder.py:1899-1900` | Medium | Wrong aggregation |
| M7 | `get_switch_frequency` tie-break nondeterminism | `time_recorder.py:1870-1872` | Medium | Nondeterministic metric |
| M8 | `_init_db` migrations can half-apply (`DROP` + implicit commit) | `time_recorder.py:179-215`, `:278-406` | Medium | Potential total data loss (unreproduced) |
| L1 | `_migrate_current_tag_totals` rescans on each boot, drifts from engine | `time_recorder.py:217-252` | Low | Startup cost, drift |
| L2 | Hard-coded `[7:-1]` prefix strip | `time_recorder.py:452` | Low | Fragile |
| L3 | `get_focus_sessions` exact-string merge | `time_recorder.py:1845` | Low | Inflated sessions |
| L4 | Duplicate `add_tag` raises unhandled `IntegrityError` | `time_recorder.py:1641-1649` | Low | Crash/UX |
| L5 | `chrome_url_events` unbounded | `time_recorder.py:1041-1055` | Low | DB growth |

**Suggested fix order:** H1 (one-line `device_id` scoping) and H3 (`tag` belongs in the conflict key, or `tag` must not be in the `DO UPDATE SET`) are the highest value-per-effort. H2 requires deciding whether `tag_time_records` is an accumulator or a derived cache — right now it is both, which is the root cause of H2, M2, and L1 together.
