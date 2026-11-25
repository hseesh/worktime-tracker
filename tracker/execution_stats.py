"""Read-only execution metrics. Never substitute session spans for missing timing."""
import json
import math
import re
import sqlite3
from datetime import date, datetime, time, timedelta

_CODEX_CACHE = {}

def codex_events(path):
    stat = path.stat()
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    cached = _CODEX_CACHE.get(str(path))
    if cached and cached[0] == fingerprint:
        return cached[1]
    events = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            # Timing never requires response bodies or instruction contents.
            if '"event_msg"' not in line and '"session_meta"' not in line:
                continue
            try:
                obj = json.loads(line)
                p = obj.get("payload") or {}
                if obj.get("type") == "session_meta":
                    events.append({"type": "session_meta", "payload": {k:p.get(k) for k in ("id", "cwd")}})
                elif obj.get("type") == "event_msg" and p.get("type") in ("task_complete", "user_message"):
                    fields = ("type", "turn_id", "started_at", "completed_at", "duration_ms", "message")
                    events.append({"type":"event_msg", "timestamp":obj.get("timestamp"), "payload":{k:p.get(k) for k in fields}})
            except (ValueError, TypeError, AttributeError):
                continue
    _CODEX_CACHE[str(path)] = (fingerprint, events)
    return events


def _task_title(value):
    """Normalize display metadata, not conversation/instruction bodies."""
    if not isinstance(value, str) or not value.strip():
        return None
    if "## My request:" in value:
        value = value.split("## My request:", 1)[1]
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[\n\r]+", " ", value)
    return " ".join(value.replace("**", "").strip().split())[:240] or None


def read_codex_task_titles(session_ids, codex_dirs):
    """Resolve current task names read-only; tolerate older state DB schemas.

    Only id/name/title are read, restricted to the execution tasks being shown.
    No transcript, credential or global settings files are needed.
    """
    ids = set(session_ids)
    if not ids:
        return {}
    titles = {}
    roots = {directory.parent for directory in codex_dirs}
    for root in sorted(roots):
        databases = [p for p in root.glob("state_*.sqlite") if re.fullmatch(r"state_\d+\.sqlite", p.name)]
        databases.sort(key=lambda p: int(p.stem.split("_")[-1]), reverse=True)
        for db in databases:
            con = None
            try:
                con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
                columns = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
                if not {"id", "title"} <= columns:
                    continue
                name = "name" if "name" in columns else "NULL"
                pending = sorted(ids - titles.keys())
                for offset in range(0, len(pending), 500):
                    batch = pending[offset:offset+500]
                    placeholders = ",".join("?" for _ in batch)
                    for sid, display_name, title in con.execute(f"SELECT id, {name}, title FROM threads WHERE id IN ({placeholders})", batch):
                        title = _task_title(display_name) or _task_title(title)
                        if title:
                            titles[sid] = title
            except (OSError, sqlite3.Error):
                continue
            finally:
                if con is not None:
                    con.close()
            if ids <= titles.keys():
                break
        # CLI/older installations may only keep the lightweight title index.
        if ids - titles.keys():
            indexed = {}
            try:
                with (root / "session_index.jsonl").open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                            sid = row.get("id")
                            title = _task_title(row.get("thread_name"))
                            if sid in ids and title:
                                indexed[sid] = title  # append-only index: latest row wins
                        except (ValueError, TypeError, AttributeError):
                            continue
            except OSError:
                pass
            for sid, title in indexed.items():
                titles.setdefault(sid, title)
    return titles


def epoch(value):
    try:
        if isinstance(value, bool):
            return None
        n = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def merge_execution(parts):
    if not parts:
        return None
    result = {"version": 1, "tasks": [], "sources": {}}
    for part in parts:
        result["tasks"].extend(part.get("tasks", []))
        for source, entry in part.get("sources", {}).items():
            acc = result["sources"].setdefault(source, {"seconds": 0, "count": 0, "missing": 0})
            for key in ("seconds", "count", "missing"):
                acc[key] += entry.get(key, 0)
            if entry.get("error"):
                acc["error"] = entry["error"]
    result["tasks"].sort(key=lambda t: -t["seconds"])
    return result


def read_execution_stats(target_date, devin_db, codex_dirs):
    d = date.fromisoformat(target_date)
    start = datetime.combine(d, time.min).timestamp()
    end = datetime.combine(d + timedelta(days=1), time.min).timestamp()
    tasks = {}
    sources = {s: {"seconds": 0, "count": 0, "missing": 0} for s in ("devin", "codex")}
    seen = set()

    def add(source, sid, title, project, seconds=None):
        task = tasks.setdefault((source, sid), {"source": source, "title": title or "(untitled)", "project": project or "(unknown)", "seconds": 0, "count": 0, "missing": 0})
        key = "missing" if seconds is None else "count"
        task[key] += 1
        sources[source][key] += 1
        if seconds is not None:
            task["seconds"] += seconds
            sources[source]["seconds"] += seconds

    if devin_db.exists():
        con = sqlite3.connect(devin_db.as_uri() + "?mode=ro", uri=True)
        try:
            # Filter by message day, not session creation; include cross-midnight requests.
            columns = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
            if {"created_at", "last_activity_at"} <= columns:
                # Drive the indexed per-session lookup; never scan all message blobs.
                rows = con.execute("SELECT n.session_id, s.title, s.working_directory, n.chat_message FROM sessions s CROSS JOIN message_nodes n ON s.id=n.session_id WHERE s.last_activity_at >= ? AND s.created_at < ? AND n.created_at >= ? AND n.created_at < ?", (start, end, start, end + 86400))
            else:
                rows = con.execute("SELECT n.session_id, s.title, s.working_directory, n.chat_message FROM message_nodes n JOIN sessions s ON s.id=n.session_id WHERE n.created_at >= ? AND n.created_at < ?", (start, end + 86400))
            for sid, title, project, raw in rows:
                try:
                    obj = json.loads(raw)
                    if obj.get("role") != "assistant":
                        continue
                    meta = obj.get("metadata") or {}
                    ms = epoch_number((meta.get("metrics") or {}).get("total_time_ms"))
                    ts = epoch(meta.get("started_generation_at"))
                    # Without request identity/timestamp, do not claim a measured duration.
                    if ts is None:
                        created = epoch(meta.get("created_at"))
                        if created is not None and start <= created < end:
                            add("devin", sid, title, project)
                        continue
                    finish = ts + (ms / 1000 if ms is not None else 0)
                    if ts >= end or finish < start:
                        continue
                    identity = meta.get("request_id") or meta.get("started_generation_at")
                    key = ("devin", sid, identity)
                    if key in seen:
                        continue
                    seen.add(key)
                    seconds = max(0, min(finish, end) - max(ts, start)) if ms is not None else None
                    add("devin", sid, title, project, seconds)
                except (ValueError, TypeError, AttributeError):
                    continue
        except sqlite3.Error:
            sources["devin"]["error"] = "source unavailable or incompatible"
        finally:
            con.close()

    for directory in codex_dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*.jsonl"):
            # Old sessions may contain today's turns. mtime is only a safe prefilter.
            if path.stat().st_mtime < start:
                continue
            sid, title, project = str(path), None, None
            completions = []
            try:
                for obj in codex_events(path):
                    payload = obj.get("payload") or {}
                    if obj["type"] == "session_meta":
                        sid = payload.get("id") or sid
                        project = payload.get("cwd")
                    elif payload.get("type") == "user_message" and not title:
                        title = " ".join(str(payload.get("message", "")).split())[:80]
                    elif payload.get("type") == "task_complete":
                        completions.append((payload, obj.get("timestamp")))
                title = _task_title(title) or "Untitled task"
                for p, timestamp in completions:
                    finish = epoch(p.get("completed_at")) or epoch(timestamp)
                    ms = epoch_number(p.get("duration_ms"))
                    begin = epoch(p.get("started_at"))
                    if finish is None:
                        continue
                    if ms is not None:
                        begin = finish - ms / 1000
                    if begin is None or begin > finish:
                        if start <= finish < end:
                            add("codex", sid, title, project)
                        continue
                    if begin >= end or finish < start:
                        continue
                    key = ("codex", sid, p.get("turn_id") or (begin, finish))
                    if key in seen:
                        continue
                    seen.add(key)
                    add("codex", sid, title, project, max(0, min(finish, end) - max(begin, start)))
            except OSError:
                sources["codex"]["error"] = "some source files unreadable"
    current_titles = read_codex_task_titles([sid for source, sid in tasks if source == "codex"], codex_dirs)
    for (source, sid), task in tasks.items():
        if source == "codex" and sid in current_titles:
            task["title"] = current_titles[sid]
    return {"version": 1, "sources": sources, "tasks": sorted(tasks.values(), key=lambda t: -t["seconds"])}


def epoch_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None
