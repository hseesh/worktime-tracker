"""Read AI token usage from Devin CLI sessions.db and Codex JSONL files.

Devin:  C:\\Users\\<user>\\AppData\\Roaming\\devin\\cli\\sessions.db (SQLite)
Codex:  ~/.codex/sessions/**/*.jsonl  +  ~/.codex/archived_sessions/**/*.jsonl

All reads are read-only and never modify the source databases/files.
"""

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Resolve user home once
_HOME = Path.home()
_DEVIN_DB = _HOME / "AppData" / "Roaming" / "devin" / "cli" / "sessions.db"
_CODEX_SESSIONS_DIR = _HOME / ".codex" / "sessions"
_CODEX_ARCHIVED_DIR = _HOME / ".codex" / "archived_sessions"


def _extract_devin_dims(meta_json: str) -> Optional[Dict]:
    """Extract token stats from a Devin session.metadata JSON string."""
    if not meta_json:
        return None
    try:
        m = json.loads(meta_json) if isinstance(meta_json, str) else meta_json
    except (json.JSONDecodeError, TypeError):
        return None
    dims = m.get("response_dimensions", [])
    out = {}
    for d in dims:
        uid = d.get("uid", "")
        kind = d.get("kind", {})
        val = None
        if "CumulativeMetric" in kind:
            val = kind["CumulativeMetric"].get("value")
        elif "Metric" in kind:
            val = kind["Metric"].get("value")
        if val is not None:
            out[uid] = val
    return out if out else None


def _ts_to_date(ts) -> Optional[str]:
    """Convert a unix timestamp (int/float/str) to local ISO date string."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _local_day_epoch_range(d: date) -> tuple[int, int]:
    """Return Unix timestamps for the system-local calendar day."""
    start = datetime(d.year, d.month, d.day)
    next_day = d + timedelta(days=1)
    end = datetime(next_day.year, next_day.month, next_day.day)
    return int(start.timestamp()), int(end.timestamp())


def _event_local_date(obj: Dict, fallback: str = "") -> str:
    """Read a Codex event timestamp and convert it to a local ISO date."""
    ts = obj.get("timestamp") or (obj.get("payload") or {}).get("timestamp")
    if not ts:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().date().isoformat()
    except (TypeError, ValueError):
        return fallback


def read_devin_daily_tokens(target_date: Optional[str] = None) -> Dict[str, Dict]:
    """Read Devin token usage grouped by date and model.

    Returns:
        {date_iso: {model: {"input": int, "output": int, "cached": int, "sessions": int, "messages": int}}}

    If *target_date* is given, only sessions on that date are returned (faster
    because we filter in SQL).  Otherwise all sessions are scanned.
    """
    if not _DEVIN_DB.exists():
        return {}

    result: Dict[str, Dict] = {}
    try:
        con = sqlite3.connect(f"file:{_DEVIN_DB}?mode=ro", uri=True)
        cur = con.cursor()
        if target_date:
            # Convert date to unix timestamp range for SQL filtering
            d = date.fromisoformat(target_date)
            start_ts, end_ts = _local_day_epoch_range(d)
            rows = cur.execute(
                "SELECT model, created_at, metadata FROM sessions "
                "WHERE created_at >= ? AND created_at < ?",
                (start_ts, end_ts),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT model, created_at, metadata FROM sessions"
            ).fetchall()
        con.close()
    except sqlite3.Error as e:
        logger.warning("Failed to read Devin sessions.db: %s", e)
        return {}

    for model, created_at, meta in rows:
        d_iso = _ts_to_date(created_at)
        if not d_iso:
            continue
        if target_date and d_iso != target_date:
            continue
        dims = _extract_devin_dims(meta)
        if not dims:
            continue
        inp = int(dims.get("input_tokens", 0) or 0)
        out = int(dims.get("output_tokens", 0) or 0)
        cached = int(dims.get("cached_input_tokens", 0) or 0)
        msgs = int(dims.get("agent_messages", 0) or 0)
        mdl = model or "unknown"
        day = result.setdefault(d_iso, {})
        entry = day.setdefault(mdl, {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0})
        entry["input"] += inp
        entry["output"] += out
        entry["cached"] += cached
        entry["sessions"] += 1
        entry["messages"] += msgs
    return result


def _codex_file_date(filepath: Path) -> Optional[str]:
    """Extract date from Codex rollout filename or fall back to mtime.

    Filenames look like: rollout-2026-08-19T16-25-51-<uuid>.jsonl
    """
    name = filepath.name
    # Try parsing from filename
    if name.startswith("rollout-"):
        try:
            date_part = name[8:18]  # "2026-08-19"
            date.fromisoformat(date_part)  # validate
            return date_part
        except (ValueError, IndexError):
            pass
    # Fall back to file mtime
    try:
        return datetime.fromtimestamp(filepath.stat().st_mtime).date().isoformat()
    except OSError:
        return None


def _read_codex_file_tokens(filepath: Path) -> Optional[Dict]:
    """Read the last token_count event and model from a Codex JSONL file.

    Returns {"input": int, "output": int, "cached": int, "reasoning": int, "model": str} or None.
    """
    last_usage = None
    model = None
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Extract model from session_meta (first line, but check all)
                if obj.get("type") == "session_meta":
                    payload = obj.get("payload") or {}
                    provenance = (payload.get("base_instructions") or {}).get("provenance") or {}
                    model = provenance.get("model") or ""
                if obj.get("type") == "event_msg" and obj.get("payload", {}).get("type") == "token_count":
                    payload = obj.get("payload") or {}
                    usage = (payload.get("info") or {}).get("total_token_usage")
                    if usage:
                        last_usage = usage
    except OSError as e:
        logger.debug("Failed to read Codex file %s: %s", filepath, e)
    if not last_usage:
        return None
    return {
        "input": int(last_usage.get("input_tokens", 0) or 0),
        "output": int(last_usage.get("output_tokens", 0) or 0),
        "cached": int(last_usage.get("cached_input_tokens", 0) or 0),
        "reasoning": int(last_usage.get("reasoning_output_tokens", 0) or 0),
        "model": model or "codex",
    }


def _read_codex_file_daily_tokens(filepath: Path) -> Dict[str, Dict]:
    """Split cumulative Codex token counters into local-day deltas."""
    model = "codex"
    previous = None
    daily: Dict[str, Dict] = {}
    fallback_date = _codex_file_date(filepath) or ""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    payload = obj.get("payload") or {}
                    provenance = (payload.get("base_instructions") or {}).get("provenance") or {}
                    model = provenance.get("model") or model
                    continue
                if obj.get("type") != "event_msg" or (obj.get("payload") or {}).get("type") != "token_count":
                    continue
                usage = (((obj.get("payload") or {}).get("info") or {}).get("total_token_usage"))
                if not usage:
                    continue
                current = {
                    "input": int(usage.get("input_tokens", 0) or 0),
                    "output": int(usage.get("output_tokens", 0) or 0),
                    "cached": int(usage.get("cached_input_tokens", 0) or 0),
                }
                deltas = {}
                for key, value in current.items():
                    old = previous.get(key, 0) if previous else 0
                    # A counter reset begins a new cumulative sequence.
                    deltas[key] = value - old if value >= old else value
                previous = current
                if not any(deltas.values()):
                    continue
                d_iso = _event_local_date(obj, fallback_date)
                if not d_iso:
                    continue
                entry = daily.setdefault(
                    d_iso,
                    {"input": 0, "output": 0, "cached": 0, "sessions": 1, "messages": 0},
                )
                for key, value in deltas.items():
                    entry[key] += value
    except OSError as e:
        logger.debug("Failed to read Codex file %s: %s", filepath, e)
    return {d_iso: {model: entry} for d_iso, entry in daily.items()}


def read_today_tool_calls() -> Dict:
    """Read today's tool call counts from both Devin and Codex.

    Returns:
        {
            "counts": {"exec": 126, "mcp_call_tool": 39, "skill": 3, ...},
            "mcp_detail": {"mysql.mysql_query": 27, "unityMCP.execute_code": 12, ...},
            "skill_detail": {"auto-merge": 1, "review": 1, ...},
        }
    """
    import ast
    from collections import Counter
    counts: Counter = Counter()
    mcp_detail: Counter = Counter()
    skill_detail: Counter = Counter()
    today_iso = date.today().isoformat()

    # --- Devin ---
    if _DEVIN_DB.exists():
        today_start, tomorrow_start = _local_day_epoch_range(date.today())
        try:
            con = sqlite3.connect(f"file:{_DEVIN_DB}?mode=ro", uri=True)
            try:
                session_ids = [r[0] for r in con.execute(
                    "SELECT id FROM sessions WHERE created_at >= ? AND created_at < ?",
                    (today_start, tomorrow_start),
                ).fetchall()]
                if session_ids:
                    placeholders = ",".join("?" * len(session_ids))
                    for row in con.execute(
                        f"SELECT tool_call_json FROM tool_call_state WHERE session_id IN ({placeholders})",
                        session_ids,
                    ):
                        try:
                            j = json.loads(row[0])
                            meta = j.get("_meta")
                            if isinstance(meta, str):
                                meta = ast.literal_eval(meta)
                            name = (meta or {}).get("cognition.ai/inferenceToolName", "unknown")
                            counts[name] += 1
                            # Extract MCP detail from title: "Calling mysql_query from mysql"
                            title = j.get("title", "")
                            if name == "mcp_call_tool":
                                parts = title.replace("Calling ", "").split(" from ")
                                if len(parts) == 2:
                                    mcp_detail[f"{parts[1]}.{parts[0]}"] += 1
                                else:
                                    mcp_detail[title] += 1
                            elif name == "skill":
                                if "skill " in title:
                                    skill_detail[title.split("skill ")[-1]] += 1
                                else:
                                    skill_detail[title] += 1
                        except (json.JSONDecodeError, ValueError, SyntaxError):
                            pass
            finally:
                con.close()
        except sqlite3.Error as e:
            logger.debug("Failed to read Devin tool calls: %s", e)

    # --- Codex ---
    codex_type_map = {
        "mcp_tool_call_end": "mcp_call_tool",
        "patch_apply_end": "edit",
        "web_search_end": "web_search",
    }
    for d in (_CODEX_SESSIONS_DIR, _CODEX_ARCHIVED_DIR):
        if not d.exists():
            continue
        for filepath in d.rglob("*.jsonl"):
            file_date = _codex_file_date(filepath) or ""
            try:
                modified_date = datetime.fromtimestamp(filepath.stat().st_mtime).date().isoformat()
            except OSError:
                modified_date = ""
            if file_date != today_iso and modified_date != today_iso:
                continue
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") != "event_msg":
                            continue
                        if _event_local_date(obj, file_date) != today_iso:
                            continue
                        payload = obj.get("payload") or {}
                        evt_type = payload.get("type", "")
                        mapped = codex_type_map.get(evt_type)
                        if mapped:
                            counts[mapped] += 1
                            # Extract MCP detail from Codex mcp_tool_call_end
                            if mapped == "mcp_call_tool":
                                invocation = payload.get("invocation") or {}
                                server = invocation.get("server") or ""
                                tool = invocation.get("tool") or ""
                                if server and tool:
                                    mcp_detail[f"{server}.{tool}"] += 1
                                elif tool:
                                    mcp_detail[tool] += 1
            except OSError:
                pass

    return {
        "counts": dict(counts),
        "mcp_detail": dict(mcp_detail),
        "skill_detail": dict(skill_detail),
    }


def read_all_daily_tool_calls(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Dict[str, Dict[str, int]]]:
    """Scan each source once and return tool details grouped by local date."""
    import ast
    from collections import Counter
    daily: Dict[str, Dict[str, Counter]] = {}

    def counters(d_iso: str):
        return daily.setdefault(d_iso, {"mcp": Counter(), "skill": Counter()})

    def in_range(d_iso: str) -> bool:
        return bool(d_iso) and (not start_date or d_iso >= start_date) and (not end_date or d_iso <= end_date)

    def add_devin(d_iso: str, raw_json: str):
        if not in_range(d_iso):
            return
        try:
            item = json.loads(raw_json)
            meta = item.get("_meta")
            if isinstance(meta, str):
                meta = ast.literal_eval(meta)
            name = (meta or {}).get("cognition.ai/inferenceToolName", "unknown")
            title = item.get("title", "")
            day = counters(d_iso)
            if name == "mcp_call_tool":
                parts = title.replace("Calling ", "").split(" from ")
                day["mcp"][f"{parts[1]}.{parts[0]}" if len(parts) == 2 else title] += 1
            elif name == "skill":
                day["skill"][title.split("skill ")[-1] if "skill " in title else title] += 1
        except (json.JSONDecodeError, ValueError, SyntaxError, TypeError):
            return

    # Devin: one joined query instead of one database scan per date.
    if _DEVIN_DB.exists():
        try:
            con = sqlite3.connect(f"file:{_DEVIN_DB}?mode=ro", uri=True)
            try:
                sql = (
                    "SELECT s.created_at, t.tool_call_json FROM tool_call_state t "
                    "JOIN sessions s ON s.id = t.session_id"
                )
                params = []
                clauses = []
                if start_date:
                    start_ts, _ = _local_day_epoch_range(date.fromisoformat(start_date))
                    clauses.append("s.created_at >= ?")
                    params.append(start_ts)
                if end_date:
                    _, end_ts = _local_day_epoch_range(date.fromisoformat(end_date))
                    clauses.append("s.created_at < ?")
                    params.append(end_ts)
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                for created_at, raw_json in con.execute(sql, params):
                    add_devin(_ts_to_date(created_at) or "", raw_json)
            finally:
                con.close()
        except sqlite3.Error as e:
            logger.debug("Failed to read Devin tool calls: %s", e)

    # Codex: each JSONL is opened once and each event uses its own timestamp,
    # so sessions spanning midnight are assigned to the correct local day.
    for d_dir in (_CODEX_SESSIONS_DIR, _CODEX_ARCHIVED_DIR):
        if not d_dir.exists():
            continue
        for filepath in d_dir.rglob("*.jsonl"):
            fallback_date = _codex_file_date(filepath) or ""
            try:
                modified_date = datetime.fromtimestamp(filepath.stat().st_mtime).date().isoformat()
            except OSError:
                modified_date = fallback_date
            if end_date and fallback_date > end_date:
                continue
            if start_date and fallback_date < start_date and modified_date < start_date:
                continue
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") != "event_msg":
                            continue
                        payload = obj.get("payload") or {}
                        if payload.get("type", "") == "mcp_tool_call_end":
                            d_iso = _event_local_date(obj, fallback_date)
                            if not in_range(d_iso):
                                continue
                            invocation = payload.get("invocation") or {}
                            server = invocation.get("server") or ""
                            tool = invocation.get("tool") or ""
                            if server and tool:
                                counters(d_iso)["mcp"][f"{server}.{tool}"] += 1
                            elif tool:
                                counters(d_iso)["mcp"][tool] += 1
            except OSError:
                pass

    return {
        d_iso: {category: dict(items) for category, items in groups.items()}
        for d_iso, groups in daily.items()
    }


def read_daily_tool_calls(target_date: str) -> Dict[str, Dict[str, int]]:
    """Read MCP/skill details for one local calendar day."""
    return read_all_daily_tool_calls(target_date, target_date).get(
        target_date, {"mcp": {}, "skill": {}}
    )


def read_codex_daily_tokens(target_date: Optional[str] = None) -> Dict[str, Dict]:
    """Read Codex token usage grouped by date and model.

    Returns:
        {date_iso: {model: {"input": int, "output": int, "cached": int, "sessions": int, "messages": 0}}}

    If *target_date* is given, only files matching that date are read.
    Older Codex sessions without provenance.model fall back to "codex".
    """
    result: Dict[str, Dict] = {}
    dirs = [_CODEX_SESSIONS_DIR, _CODEX_ARCHIVED_DIR]
    for d in dirs:
        if not d.exists():
            continue
        for filepath in d.rglob("*.jsonl"):
            file_date = _codex_file_date(filepath)
            if not file_date:
                continue
            if target_date:
                try:
                    modified_date = datetime.fromtimestamp(filepath.stat().st_mtime).date().isoformat()
                except OSError:
                    modified_date = file_date
                if file_date > target_date or (file_date < target_date and modified_date < target_date):
                    continue
            file_days = _read_codex_file_daily_tokens(filepath)
            for d_iso, sources in file_days.items():
                if target_date and d_iso != target_date:
                    continue
                day = result.setdefault(d_iso, {})
                for model, tokens in sources.items():
                    entry = day.setdefault(
                        model,
                        {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                    )
                    for key in ("input", "output", "cached", "sessions", "messages"):
                        entry[key] += tokens.get(key, 0)
    return result


def read_all_daily_tokens(target_date: Optional[str] = None) -> Dict[str, Dict]:
    """Merge Devin + Codex daily token data.

    Returns:
        {date_iso: {model_or_source: {"input", "output", "cached", "sessions", "messages"}}}
    """
    result: Dict[str, Dict] = {}
    for source_data in (
        read_devin_daily_tokens(target_date),
        read_codex_daily_tokens(target_date),
    ):
        for d_iso, sources in source_data.items():
            day = result.setdefault(d_iso, {})
            for source, values in sources.items():
                entry = day.setdefault(
                    source,
                    {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                )
                for key in ("input", "output", "cached", "sessions", "messages"):
                    entry[key] += values.get(key, 0)
    return result


def get_today_tokens() -> Dict:
    """Get today's token summary for dashboard display.

    Returns:
        {
            "total_tokens": int,
            "input_tokens": int,
            "output_tokens": int,
            "cached_tokens": int,
            "sessions": int,
            "messages": int,
            "by_source": [ {"source": str, "tokens": int, "sessions": int} ],
            "tool_calls": {"mcp": int, "skill": int, "exec": int, "edit": int, ...},
        }
    """
    today = date.today().isoformat()
    data = read_all_daily_tokens(target_date=today)
    today_data = data.get(today, {})
    summary = _summarize_day(today_data)
    summary["tool_calls"] = read_today_tool_calls()
    return summary


def get_date_tokens(d_iso: str) -> Dict:
    """Get token summary for a specific date."""
    data = read_all_daily_tokens(target_date=d_iso)
    day_data = data.get(d_iso, {})
    return _summarize_day(day_data)


def _summarize_day(day_data: Dict) -> Dict:
    total_input = 0
    total_output = 0
    total_cached = 0
    total_sessions = 0
    total_messages = 0
    by_source = []
    for source, entry in day_data.items():
        inp = entry.get("input", 0)
        out = entry.get("output", 0)
        cached = entry.get("cached", 0)
        sess = entry.get("sessions", 0)
        msgs = entry.get("messages", 0)
        total_input += inp
        total_output += out
        total_cached += cached
        total_sessions += sess
        total_messages += msgs
        by_source.append({
            "source": source,
            "tokens": inp + out + cached,
            "input": inp,
            "output": out,
            "cached": cached,
            "sessions": sess,
            "messages": msgs,
        })
    by_source.sort(key=lambda x: -x["tokens"])
    return {
        "total_tokens": total_input + total_output + total_cached,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cached_tokens": total_cached,
        "sessions": total_sessions,
        "messages": total_messages,
        "by_source": by_source,
    }


def format_tokens(n: int) -> str:
    """Format token count compactly: 1234 -> '1.2K', 1234567 -> '1.23M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def read_daily_devin_activity(target_date: str) -> Dict:
    """Read Devin session activity for a specific date.

    Returns:
        {
            "projects": [{"project": str, "sessions": int, "messages": int, "duration": int(seconds)}],
            "tool_kinds": {"edit": int, "execute": int, "read": int, "search": int, "fetch": int},
            "agent_modes": {"bypass": int, "accept-edits": int},
            "backend_types": {"windsurf": int, "cli": int},
            "msg_dist": {"user": int, "assistant": int, "tool": int, "system": int},
            "titles": [{"title": str, "duration": int(seconds), "project": str}],
        }
    """
    from collections import Counter, defaultdict

    result = {
        "projects": [],
        "tool_kinds": {},
        "agent_modes": {},
        "backend_types": {},
        "msg_dist": {},
        "titles": [],
    }
    if not _DEVIN_DB.exists():
        return result

    try:
        con = sqlite3.connect(f"file:{_DEVIN_DB}?mode=ro", uri=True)
        cur = con.cursor()
        d = date.fromisoformat(target_date)
        start_ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        end_ts = start_ts + 86400

        sessions = cur.execute(
            "SELECT id, working_directory, model, agent_mode, backend_type, "
            "created_at, last_activity_at, title "
            "FROM sessions WHERE created_at >= ? AND created_at < ?",
            (start_ts, end_ts),
        ).fetchall()

        if not sessions:
            con.close()
            return result

        # Per-project aggregation
        proj_data: Dict[str, Dict] = defaultdict(lambda: {"sessions": 0, "messages": 0, "duration": 0})
        agent_modes: Counter = Counter()
        backend_types: Counter = Counter()
        titles = []
        session_ids = []

        for sid, wd, model, agent_mode, backend_type, created, last_activity, title in sessions:
            session_ids.append(sid)
            duration = max(0, int(last_activity - created))
            # Normalize project path to last 2 path components
            proj = _normalize_project_path(wd)
            proj_data[proj]["sessions"] += 1
            proj_data[proj]["duration"] += duration
            if agent_mode:
                agent_modes[agent_mode] += 1
            if backend_type:
                backend_types[backend_type] += 1
            if title and title.strip():
                titles.append({"title": title.strip(), "duration": duration, "project": proj})

        # Message distribution (batch query)
        msg_dist: Counter = Counter()
        if session_ids:
            placeholders = ",".join("?" * len(session_ids))
            rows = cur.execute(
                f"SELECT json_extract(chat_message, '$.role') as role, COUNT(*) "
                f"FROM message_nodes WHERE session_id IN ({placeholders}) "
                f"GROUP BY role",
                session_ids,
            ).fetchall()
            for role, cnt in rows:
                if role and role != "system":
                    msg_dist[role] = cnt
            # Add message counts to projects (user messages only)
            for sid, wd, *_ in sessions:
                proj = _normalize_project_path(wd)
                cnt = cur.execute(
                    "SELECT COUNT(*) FROM message_nodes WHERE session_id = ? "
                    "AND json_extract(chat_message, '$.role') = 'user'",
                    (sid,),
                ).fetchone()[0]
                proj_data[proj]["messages"] += cnt

        # Tool call kinds (batch query)
        tool_kinds: Counter = Counter()
        if session_ids:
            placeholders = ",".join("?" * len(session_ids))
            rows = cur.execute(
                f"SELECT json_extract(tool_call_json, '$.kind') as kind, COUNT(*) "
                f"FROM tool_call_state WHERE session_id IN ({placeholders}) "
                f"GROUP BY kind",
                session_ids,
            ).fetchall()
            for kind, cnt in rows:
                if kind:
                    tool_kinds[kind] = cnt

        con.close()

        # Build result from Devin data
        projects = []
        for proj, d in proj_data.items():
            projects.append({
                "project": proj,
                "sessions": d["sessions"],
                "messages": d["messages"],
                "duration": d["duration"],
            })
        projects.sort(key=lambda x: -x["duration"])

        titles.sort(key=lambda x: -x["duration"])
        titles = titles[:10]

        result["projects"] = projects
        result["tool_kinds"] = dict(tool_kinds)
        result["agent_modes"] = dict(agent_modes)
        result["backend_types"] = dict(backend_types)
        result["msg_dist"] = dict(msg_dist)
        result["titles"] = titles

    except sqlite3.Error as e:
        logger.warning("Failed to read Devin activity for %s: %s", target_date, e)

    # --- Codex: scan JSONL files for the date ---
    from collections import Counter, defaultdict
    codex_msg_dist: Counter = Counter()
    codex_tool_kinds: Counter = Counter()
    codex_backend_types: Counter = Counter()
    codex_projects: Dict[str, Dict] = defaultdict(lambda: {"sessions": 0, "messages": 0, "duration": 0})
    codex_titles = []

    for d_dir in (_CODEX_SESSIONS_DIR, _CODEX_ARCHIVED_DIR):
        if not d_dir.exists():
            continue
        for filepath in d_dir.rglob("*.jsonl"):
            if _codex_file_date(filepath) != target_date:
                continue
            try:
                cwd = None
                model = "codex"
                source = None
                first_ts = None
                last_ts = None
                user_msgs = 0
                agent_msgs = 0
                agent_reasoning = 0
                patches = 0
                tool_calls = 0
                tasks_complete = 0
                title = None

                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        t = obj.get("type", "")
                        if t == "session_meta":
                            payload = obj.get("payload") or {}
                            cwd = payload.get("cwd")
                            source = payload.get("source", "")
                            provenance = (payload.get("base_instructions") or {}).get("provenance") or {}
                            model = provenance.get("model") or "codex"
                            ts_str = payload.get("timestamp", "")
                            if ts_str:
                                try:
                                    first_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                                except (ValueError, TypeError):
                                    pass
                        elif t == "event_msg":
                            payload = obj.get("payload") or {}
                            et = payload.get("type", "")
                            # started_at is a unix timestamp (int)
                            sa = payload.get("started_at")
                            if sa and isinstance(sa, (int, float)):
                                last_ts = float(sa)
                            if et == "user_message":
                                user_msgs += 1
                                if not title:
                                    msg = payload.get("message", "")
                                    if isinstance(msg, str):
                                        # Clean up: remove markdown, newlines, extra spaces
                                        import re
                                        clean = re.sub(r'[#*`\[\]\(\)\\]', '', msg)
                                        clean = re.sub(r'\s+', ' ', clean).strip()
                                        title = clean[:60] if clean else None
                            elif et == "agent_message":
                                agent_msgs += 1
                            elif et == "agent_reasoning":
                                agent_reasoning += 1
                            elif et == "patch_apply_end":
                                patches += 1
                            elif et == "task_complete":
                                tasks_complete += 1
                        elif t == "response_item":
                            payload = obj.get("payload") or {}
                            rt = payload.get("type", "")
                            if rt == "custom_tool_call":
                                tool_calls += 1

                if first_ts and last_ts:
                    duration = int(last_ts - first_ts)
                else:
                    duration = 0

                proj = _normalize_project_path(cwd) if cwd else "codex"
                codex_projects[proj]["sessions"] += 1
                codex_projects[proj]["messages"] += user_msgs
                codex_projects[proj]["duration"] += duration

                codex_msg_dist["user"] += user_msgs
                codex_msg_dist["assistant"] += agent_msgs
                codex_msg_dist["reasoning"] += agent_reasoning
                codex_tool_kinds["edit"] += patches
                codex_tool_kinds["execute"] += tool_calls
                codex_tool_kinds["task_complete"] += tasks_complete
                if source:
                    codex_backend_types[f"codex-{source}"] += 1
                if title:
                    codex_titles.append({"title": title, "duration": duration, "project": proj})

            except OSError:
                pass

    # Merge Codex into result
    if codex_projects:
        existing_projs = {p["project"] for p in result["projects"]}
        for proj, d in codex_projects.items():
            if proj in existing_projs:
                for p in result["projects"]:
                    if p["project"] == proj:
                        p["sessions"] += d["sessions"]
                        p["messages"] += d["messages"]
                        p["duration"] += d["duration"]
                        break
            else:
                result["projects"].append({
                    "project": proj,
                    "sessions": d["sessions"],
                    "messages": d["messages"],
                    "duration": d["duration"],
                })
        result["projects"].sort(key=lambda x: -x["duration"])

    for k, v in codex_msg_dist.items():
        result["msg_dist"][k] = result["msg_dist"].get(k, 0) + v
    for k, v in codex_tool_kinds.items():
        result["tool_kinds"][k] = result["tool_kinds"].get(k, 0) + v
    for k, v in codex_backend_types.items():
        result["backend_types"][k] = result["backend_types"].get(k, 0) + v

    if codex_titles:
        result["titles"].extend(codex_titles)
        result["titles"].sort(key=lambda x: -x["duration"])
        result["titles"] = result["titles"][:10]

    return result


def _normalize_project_path(wd: str) -> str:
    """Normalize a working directory to a short project name."""
    if not wd:
        return "(unknown)"
    parts = wd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else wd
