"""Read AI token usage from Devin CLI sessions.db and Codex/WorkBuddy/DSH JSONL files.

Devin:     C:\\Users\\<user>\\AppData\\Roaming\\devin\\cli\\sessions.db (SQLite)
Codex:     ~/.codex/sessions/**/*.jsonl  +  ~/.codex/archived_sessions/**/*.jsonl
WorkBuddy: ~/.workbuddy-ai/projects/**/*.jsonl
DSH:       <DSH_HOME>/sessions/**/*.jsonl.zstd (zstd-compressed JSONL)

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
_WORKBUDDY_PROJECTS_DIR = _HOME / ".workbuddy-ai" / "projects"


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


def _read_devin_message_metrics(con: sqlite3.Connection, sid: str) -> Optional[Dict]:
    """Aggregate per-assistant-message token metrics for one Devin session.

    Devin builds from 2026-09-12 (schema v17) no longer write
    ``response_dimensions`` into sessions.metadata; usage is only kept per
    assistant message at ``message_nodes.chat_message -> metadata.metrics``.
    Nodes are grouped by message_id because branching/compaction stores the
    same message multiple times, and summing them would double-count.

    ``cache_creation_tokens`` are new input tokens written to the prompt cache
    (Anthropic-style models report ``input_tokens`` as only the uncached part),
    so they are folded into ``input``.
    """
    try:
        rows = con.execute(
            "SELECT json_extract(chat_message, '$.metadata.metrics') AS metrics "
            "FROM message_nodes WHERE row_id IN ("
            "SELECT MAX(row_id) FROM message_nodes WHERE session_id = ? "
            "AND json_extract(chat_message, '$.role') = 'assistant' "
            "AND json_extract(chat_message, '$.metadata.metrics') IS NOT NULL "
            "GROUP BY COALESCE(json_extract(chat_message, '$.message_id'), node_id))",
            (sid,),
        ).fetchall()
    except sqlite3.Error as e:
        logger.debug("Failed to read message metrics for session %s: %s", sid, e)
        return None
    if not rows:
        return None
    inp = out = cached = 0
    for (raw,) in rows:
        try:
            m = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(m, dict):
            continue
        inp += int(m.get("input_tokens", 0) or 0) + int(m.get("cache_creation_tokens", 0) or 0)
        out += int(m.get("output_tokens", 0) or 0)
        cached += int(m.get("cache_read_tokens", 0) or 0)
    return {"input": inp, "output": out, "cached": cached}


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
    con = None
    try:
        con = sqlite3.connect(f"file:{_DEVIN_DB}?mode=ro", uri=True)
        cur = con.cursor()
        if target_date:
            # Convert date to unix timestamp range for SQL filtering
            d = date.fromisoformat(target_date)
            start_ts, end_ts = _local_day_epoch_range(d)
            rows = cur.execute(
                "SELECT id, model, created_at, metadata FROM sessions "
                "WHERE created_at >= ? AND created_at < ?",
                (start_ts, end_ts),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT id, model, created_at, metadata FROM sessions"
            ).fetchall()
    except sqlite3.Error as e:
        logger.warning("Failed to read Devin sessions.db: %s", e)
        if con:
            con.close()
        return {}

    for sid, model, created_at, meta in rows:
        d_iso = _ts_to_date(created_at)
        if not d_iso:
            continue
        if target_date and d_iso != target_date:
            continue
        dims = _extract_devin_dims(meta)
        if dims:
            inp = int(dims.get("input_tokens", 0) or 0)
            out = int(dims.get("output_tokens", 0) or 0)
            cached = int(dims.get("cached_input_tokens", 0) or 0)
        else:
            # Newer Devin builds stopped writing response_dimensions, so fall
            # back to the per-message usage metrics.
            metrics = _read_devin_message_metrics(con, sid)
            if not metrics:
                continue
            inp, out, cached = metrics["input"], metrics["output"], metrics["cached"]
        # Count user-sent messages from message_nodes (role=user AND is_user_input=true).
        # Uses idx_message_nodes_session index for fast per-session lookup.
        user_msgs = 0
        try:
            user_msgs = con.execute(
                "SELECT COUNT(*) FROM message_nodes WHERE session_id = ? "
                "AND json_extract(chat_message, '$.role') = 'user' "
                "AND json_extract(chat_message, '$.metadata.is_user_input') = 1",
                (sid,),
            ).fetchone()[0]
        except sqlite3.Error as e:
            logger.debug("Failed to count user messages for session %s: %s", sid, e)
        mdl = model or "unknown"
        day = result.setdefault(d_iso, {})
        entry = day.setdefault(mdl, {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0})
        entry["input"] += inp
        entry["output"] += out
        entry["cached"] += cached
        entry["sessions"] += 1
        entry["messages"] += user_msgs
    con.close()
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
    # Codex (OpenAI API): input_tokens INCLUDES cached_input_tokens.
    # Subtract cached so that input = new (non-cached) tokens only,
    # making total = input + output + cached correct without double-counting.
    raw_input = int(last_usage.get("input_tokens", 0) or 0)
    cached = int(last_usage.get("cached_input_tokens", 0) or 0)
    return {
        "input": max(0, raw_input - cached),
        "output": int(last_usage.get("output_tokens", 0) or 0),
        "cached": cached,
        "reasoning": int(last_usage.get("reasoning_output_tokens", 0) or 0),
        "model": model or "codex",
    }


def _is_codex_user_message(payload: Dict) -> bool:
    """True when a Codex ``response_item`` message carries user-typed text.

    Codex also records framework context as a ``role=user`` message at the
    start of a turn (``<environment_context>``, ``<recommended_plugins>``,
    ``<turn_aborted>``, ...). Those are not something the user sent, so only
    messages with at least one plain text part are counted.
    """
    for item in payload.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            continue
        text = (item.get("text") or "").lstrip()
        if text and not text.startswith("<"):
            return True
    return False


def _read_codex_file_daily_tokens(filepath: Path) -> Dict[str, Dict]:
    """Split cumulative Codex token counters into local-day deltas.

    ``messages`` counts the user-typed messages of the transcript, matching
    how Devin sessions are counted.
    """
    model = "codex"
    previous = None
    daily: Dict[str, Dict] = {}

    def day_entry(d_iso: str) -> Dict:
        return daily.setdefault(
            d_iso,
            {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
        )

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
                if obj.get("type") == "response_item":
                    payload = obj.get("payload") or {}
                    if (
                        payload.get("type") == "message"
                        and payload.get("role") == "user"
                        and _is_codex_user_message(payload)
                    ):
                        d_iso = _event_local_date(obj, fallback_date)
                        if d_iso:
                            day_entry(d_iso)["messages"] += 1
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
                # Codex (OpenAI API): input_tokens INCLUDES cached_input_tokens.
                # Normalize so input = new tokens only, matching Devin's convention.
                current["input"] = max(0, current["input"] - current["cached"])
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
                entry = day_entry(d_iso)
                entry["sessions"] = 1
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

    # Devin: resolve the day's sessions first, then fetch only their tool calls.
    if _DEVIN_DB.exists():
        try:
            con = sqlite3.connect(f"file:{_DEVIN_DB}?mode=ro", uri=True)
            try:
                # ``sessions`` is small and its primary key can drive the join.
                # Filtering on ``s.created_at`` in a single joined query instead
                # makes SQLite plan ``SCAN t`` over every ``tool_call_state``
                # row — 46k rows holding ~340 MB of JSON blobs on a 2.6 GB
                # database — and re-read them from disk on every refresh just to
                # count one day.
                session_sql = "SELECT id, created_at FROM sessions"
                params = []
                clauses = []
                if start_date:
                    start_ts, _ = _local_day_epoch_range(date.fromisoformat(start_date))
                    clauses.append("created_at >= ?")
                    params.append(start_ts)
                if end_date:
                    _, end_ts = _local_day_epoch_range(date.fromisoformat(end_date))
                    clauses.append("created_at < ?")
                    params.append(end_ts)
                if clauses:
                    session_sql += " WHERE " + " AND ".join(clauses)

                id_to_date = {
                    sid: _ts_to_date(created_at) or ""
                    for sid, created_at in con.execute(session_sql, params)
                }
                if id_to_date:
                    placeholders = ",".join("?" * len(id_to_date))
                    for session_id, raw_json in con.execute(
                        "SELECT session_id, tool_call_json FROM tool_call_state "
                        f"WHERE session_id IN ({placeholders})",
                        list(id_to_date),
                    ):
                        add_devin(id_to_date.get(session_id, ""), raw_json)
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


def _workbuddy_usage(obj: Dict) -> Optional[Dict]:
    """Normalize one WorkBuddy transcript entry into token counts.

    WorkBuddy writes a ``providerData.usage`` block on the function_call /
    assistant entry of every request.  ``inputTokens`` is the whole prompt
    (cache hit + miss), so cached reads are subtracted back out to match the
    Devin/Codex convention of ``input`` = new tokens only.
    """
    pd = obj.get("providerData")
    if not isinstance(pd, dict):
        return None
    usage = pd.get("usage")
    if not isinstance(usage, dict):
        return None
    raw = pd.get("rawUsage") if isinstance(pd.get("rawUsage"), dict) else {}

    cached = 0
    for detail in usage.get("inputTokensDetails") or []:
        if isinstance(detail, dict):
            cached += int(detail.get("cached_tokens") or 0)
    if not cached:
        cached = int(raw.get("prompt_cache_hit_tokens") or raw.get("cache_read_input_tokens") or 0)
    # Cache writes are billed as input but reported separately.
    written = int(raw.get("cache_creation_input_tokens") or raw.get("prompt_cache_write_tokens") or 0)

    inp = int(usage.get("inputTokens") or 0) - cached + written
    return {
        "input": max(0, inp),
        "output": int(usage.get("outputTokens") or 0),
        "cached": cached,
        "model": pd.get("model") or "unknown",
        "message_id": pd.get("messageId"),
    }


def _workbuddy_entry_date(obj: Dict, fallback: str = "") -> str:
    """Convert a WorkBuddy entry timestamp (milliseconds) to a local ISO date."""
    ts = obj.get("timestamp")
    if not ts:
        return fallback
    try:
        return datetime.fromtimestamp(int(ts) / 1000).date().isoformat()
    except (ValueError, TypeError, OSError):
        return fallback


def read_workbuddy_daily_tokens(target_date: Optional[str] = None) -> Dict[str, Dict]:
    """Read WorkBuddy token usage grouped by date and model.

    WorkBuddy keeps one JSONL transcript per session under
    ``~/.workbuddy-ai/projects/<project>/<session>.jsonl``.  Transcript names
    carry no date, so a file is only skipped when it was last modified before
    *target_date* and each entry is dated by its own timestamp.

    ``messages`` counts the user-side messages of the conversation (one per
    turn), matching how Devin sessions are counted - not the number of API
    requests, which is one per tool round and an order of magnitude larger.
    """
    result: Dict[str, Dict] = {}
    if not _WORKBUDDY_PROJECTS_DIR.exists():
        return result

    for filepath in _WORKBUDDY_PROJECTS_DIR.rglob("*.jsonl"):
        try:
            modified_date = datetime.fromtimestamp(filepath.stat().st_mtime).date().isoformat()
        except OSError:
            continue
        if target_date and modified_date < target_date:
            continue

        file_days: Dict[str, Dict] = {}
        seen_ids = set()
        seen_user_ids = set()
        # A user message carries no model, so it is held back until the request
        # it triggered reveals which model handled the turn.
        pending_user: List[str] = []
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

                    if obj.get("type") == "message" and obj.get("role") == "user":
                        user_id = obj.get("id")
                        if user_id and user_id in seen_user_ids:
                            continue
                        if user_id:
                            seen_user_ids.add(user_id)
                        d_iso = _workbuddy_entry_date(obj, modified_date)
                        if d_iso and (not target_date or d_iso == target_date):
                            pending_user.append(d_iso)
                        continue

                    entry = _workbuddy_usage(obj)
                    if not entry:
                        continue
                    # One request may be echoed on several entries.
                    if entry["message_id"]:
                        if entry["message_id"] in seen_ids:
                            continue
                        seen_ids.add(entry["message_id"])
                    d_iso = _workbuddy_entry_date(obj, modified_date)
                    if not d_iso or (target_date and d_iso != target_date):
                        continue
                    acc = file_days.setdefault(d_iso, {}).setdefault(
                        entry["model"],
                        {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                    )
                    acc["input"] += entry["input"]
                    acc["output"] += entry["output"]
                    acc["cached"] += entry["cached"]
                    # Every request of a turn belongs to the model that answered
                    # it; only the user message itself is counted once.
                    for user_day in pending_user:
                        user_acc = file_days.setdefault(user_day, {}).setdefault(
                            entry["model"],
                            {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                        )
                        user_acc["messages"] += 1
                    pending_user = []
        except OSError as e:
            logger.debug("Failed to read WorkBuddy file %s: %s", filepath, e)
            continue

        for d_iso, models in file_days.items():
            day = result.setdefault(d_iso, {})
            for model, entry in models.items():
                acc = day.setdefault(
                    model,
                    {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                )
                for key in ("input", "output", "cached", "messages"):
                    acc[key] += entry[key]
                acc["sessions"] += 1
    return result


def _dsh_entry_date(obj: Dict, fallback: str = "") -> str:
    """Convert a DSH event timestamp (milliseconds) to a local ISO date."""
    ts = obj.get("time")
    if not ts:
        return fallback
    try:
        return datetime.fromtimestamp(int(ts) / 1000).date().isoformat()
    except (ValueError, TypeError, OSError):
        return fallback


def _iter_dsh_jsonl(filepath: Path):
    """Yield parsed events from a DSH session log (plain or zstd-compressed)."""
    if filepath.suffix == ".zstd":
        try:
            import zstandard
        except ImportError:
            logger.warning("zstandard not installed; skipping DSH session %s", filepath)
            return
        with open(filepath, "rb") as f:
            text = zstandard.ZstdDecompressor().stream_reader(f).read().decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield line
    else:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield line


def read_dsh_daily_tokens(target_date: Optional[str] = None) -> Dict[str, Dict]:
    """Read DeepSeek Harness token usage grouped by date and model.

    DSH keeps one session log per session under
    ``<DSH_HOME>/sessions/--<workspace>--/session-*/session.v3.jsonl.zstd``.
    ``assistant/message`` events carry ``data.usage`` plus the serving
    ``data.message.source.{provider,model}``; ``user/message`` events with
    ``source.kind == 'user'`` are real user-typed turns (``kind: 'plugin'``
    entries are injected notices and context, not counted).

    The log uses append semantics: a message can be re-logged after branching
    or compaction, so assistant entries are deduplicated by message id.

    Returns:
        {date_iso: {provider/model: {"input", "output", "cached", "sessions", "messages"}}}
    """
    result: Dict[str, Dict] = {}
    dsh_home = os.environ.get("DSH_HOME") or r"D:\DSH\home"
    sessions_dir = Path(dsh_home) / "sessions"
    if not sessions_dir.exists():
        return result

    for filepath in sessions_dir.rglob("*.jsonl*"):
        try:
            modified_date = datetime.fromtimestamp(filepath.stat().st_mtime).date().isoformat()
        except OSError:
            continue
        if target_date and modified_date < target_date:
            continue

        file_days: Dict[str, Dict] = {}
        seen_msg_ids = set()
        seen_user_ids = set()
        pending_user: List[str] = []
        try:
            for line in _iter_dsh_jsonl(filepath):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = obj.get("type")

                if etype == "user/message":
                    data = obj.get("data") or {}
                    if (data.get("source") or {}).get("kind") != "user":
                        continue
                    user_id = data.get("id")
                    if user_id and user_id in seen_user_ids:
                        continue
                    if user_id:
                        seen_user_ids.add(user_id)
                    d_iso = _dsh_entry_date(obj, modified_date)
                    if d_iso and (not target_date or d_iso == target_date):
                        pending_user.append(d_iso)
                    continue

                if etype != "assistant/message":
                    continue
                data = obj.get("data") or {}
                usage = data.get("usage")
                if not isinstance(usage, dict):
                    continue
                message = data.get("message") or {}
                msg_id = message.get("id")
                if msg_id:
                    if msg_id in seen_msg_ids:
                        continue
                    seen_msg_ids.add(msg_id)
                source = message.get("source") or {}
                model = source.get("model") or "unknown"
                provider = source.get("provider") or "dsh"
                key = f"{provider}/{model}"
                d_iso = _dsh_entry_date(obj, modified_date)
                if not d_iso or (target_date and d_iso != target_date):
                    continue
                inp = int(usage.get("inputTokens") or 0)
                # Reasoning tokens are billed output; fold them in since the
                # shared schema has no reasoning field.
                out = int(usage.get("outputTokens") or 0) + int(usage.get("reasoningTokens") or 0)
                cached = int(usage.get("cacheReadTokens") or 0)
                acc = file_days.setdefault(d_iso, {}).setdefault(
                    key,
                    {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                )
                acc["input"] += inp
                acc["output"] += out
                acc["cached"] += cached
                for user_day in pending_user:
                    user_acc = file_days.setdefault(user_day, {}).setdefault(
                        key,
                        {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                    )
                    user_acc["messages"] += 1
                pending_user = []
        except OSError as e:
            logger.debug("Failed to read DSH session %s: %s", filepath, e)
            continue

        for d_iso, models in file_days.items():
            day = result.setdefault(d_iso, {})
            for model, entry in models.items():
                acc = day.setdefault(
                    model,
                    {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
                )
                for key in ("input", "output", "cached", "messages"):
                    acc[key] += entry[key]
                acc["sessions"] += 1
    return result


def normalize_token_source(source: str) -> str:
    """Strip the provider prefix from a source name: ``codebuddy/gpt-5.6-sol`` -> ``gpt-5.6-sol``.

    DSH logs the serving provider next to the model, which splits one model
    across several rows once the same model is reachable through more than one
    provider. The dashboard reports token usage per model, so the prefix (and
    the provider dimension it carries) is dropped and equal model names merge.
    """
    name = (source or "").strip()
    if "/" in name:
        head, tail = name.split("/", 1)
        if head and tail:
            return tail
    return name or "unknown"


def _merge_token_day(dst: Dict[str, Dict], src: Dict[str, Dict]):
    """Accumulate one ``{source: counters}`` mapping into *dst*, merging by model name."""
    for source, values in src.items():
        entry = dst.setdefault(
            normalize_token_source(source),
            {"input": 0, "output": 0, "cached": 0, "sessions": 0, "messages": 0},
        )
        for key in ("input", "output", "cached", "sessions", "messages"):
            entry[key] += values.get(key, 0)


def read_all_daily_tokens(target_date: Optional[str] = None) -> Dict[str, Dict]:
    """Merge Devin + Codex + WorkBuddy + DSH daily token data.

    Source names are normalized to the bare model name, so the same model
    reported by two providers (``codebuddy/deepseek-v4.1-flash`` and
    ``deepseek-v4.1-flash``) ends up as a single row.

    Returns:
        {date_iso: {model: {"input", "output", "cached", "sessions", "messages"}}}
    """
    result: Dict[str, Dict] = {}
    for source_data in (
        read_devin_daily_tokens(target_date),
        read_codex_daily_tokens(target_date),
        read_workbuddy_daily_tokens(target_date),
        read_dsh_daily_tokens(target_date),
    ):
        for d_iso, sources in source_data.items():
            _merge_token_day(result.setdefault(d_iso, {}), sources)
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


def get_today_token_summary() -> Dict:
    """Get today's core token numbers only (no tool-call details)."""
    today = date.today().isoformat()
    data = read_all_daily_tokens(target_date=today)
    today_data = data.get(today, {})
    return _summarize_day(today_data)


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
        # Use local midnight, not UTC, for the date range
        start_ts = int(datetime(d.year, d.month, d.day).timestamp())
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

        # Per-project aggregation (track intervals to merge overlaps)
        proj_data: Dict[str, Dict] = defaultdict(lambda: {"sessions": 0, "messages": 0, "intervals": []})
        agent_modes: Counter = Counter()
        backend_types: Counter = Counter()
        titles = []
        session_ids = []
        # Map session_id -> user message count (filled in batch query below)
        session_user_msgs: Dict[str, int] = {}

        for sid, wd, model, agent_mode, backend_type, created, last_activity, title in sessions:
            session_ids.append(sid)
            duration = max(0, int(last_activity - created))
            # Normalize project path to last 2 path components
            proj = _normalize_project_path(wd)
            proj_data[proj]["sessions"] += 1
            proj_data[proj]["intervals"].append((created, last_activity))
            if agent_mode:
                agent_modes[agent_mode] += 1
            if backend_type:
                backend_types[backend_type] += 1
            if title and title.strip():
                titles.append({"title": title.strip(), "duration": duration, "project": proj, "sid": sid})

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
            # Per-session user message counts (role=user AND is_user_input=true,
            # matching the token reader's user-sent message definition)
            for sid in session_ids:
                cnt = cur.execute(
                    "SELECT COUNT(*) FROM message_nodes WHERE session_id = ? "
                    "AND json_extract(chat_message, '$.role') = 'user' "
                    "AND json_extract(chat_message, '$.metadata.is_user_input') = 1",
                    (sid,),
                ).fetchone()[0]
                session_user_msgs[sid] = cnt
                proj = None
                for s in sessions:
                    if s[0] == sid:
                        proj = _normalize_project_path(s[1])
                        break
                if proj:
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

        # Build result from Devin data (store intervals for Codex merge later)
        projects = []
        proj_intervals: Dict[str, list] = {}
        for proj, d in proj_data.items():
            projects.append({
                "project": proj,
                "sessions": d["sessions"],
                "messages": d["messages"],
                "duration": _merged_duration(d["intervals"]),
            })
            proj_intervals[proj] = list(d["intervals"])
        projects.sort(key=lambda x: -x["duration"])

        titles.sort(key=lambda x: -x["duration"])
        titles = titles[:10]
        # Attach user message counts and strip internal sid field
        for t in titles:
            t["messages"] = session_user_msgs.get(t.pop("sid", None), 0)

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
    codex_projects: Dict[str, Dict] = defaultdict(lambda: {"sessions": 0, "messages": 0, "intervals": []})
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
                if first_ts and last_ts:
                    codex_projects[proj]["intervals"].append((first_ts, last_ts))

                codex_msg_dist["user"] += user_msgs
                codex_msg_dist["assistant"] += agent_msgs
                codex_msg_dist["reasoning"] += agent_reasoning
                codex_tool_kinds["edit"] += patches
                codex_tool_kinds["execute"] += tool_calls
                codex_tool_kinds["task_complete"] += tasks_complete
                if source:
                    codex_backend_types[f"codex-{source}"] += 1
                if title:
                    codex_titles.append({"title": title, "duration": duration, "project": proj, "messages": user_msgs})

            except OSError:
                pass

    # Merge Codex into result (merge intervals across Devin+Codex to avoid
    # double-counting overlapping parallel sessions)
    if codex_projects:
        existing_projs = {p["project"] for p in result["projects"]}
        for proj, d in codex_projects.items():
            if proj in existing_projs:
                # Combine intervals from Devin + Codex and re-merge
                all_intervals = proj_intervals.get(proj, []) + d["intervals"]
                merged_dur = _merged_duration(all_intervals)
                for p in result["projects"]:
                    if p["project"] == proj:
                        p["sessions"] += d["sessions"]
                        p["messages"] += d["messages"]
                        p["duration"] = merged_dur
                        break
            else:
                result["projects"].append({
                    "project": proj,
                    "sessions": d["sessions"],
                    "messages": d["messages"],
                    "duration": _merged_duration(d["intervals"]),
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


def _merge_intervals(intervals: List[tuple]) -> List[tuple]:
    """Merge overlapping time intervals (start, end) to avoid double-counting."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _merged_duration(intervals: List[tuple]) -> int:
    """Total seconds covered by merged intervals."""
    return sum(int(e - s) for s, e in _merge_intervals(intervals))
