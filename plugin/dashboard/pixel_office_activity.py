"""Pixel Office: live per-profile activity derived from Hermes session stores.

Read-only projection. Sources:
  * <profile_home>/state.db  (sessions + messages tables, opened mode=ro)
  * kanban workers list (passed in by the caller)

Nothing here writes to any Hermes store.

Model
-----
One Hermes profile = one office character. The profile's most relevant live
session drives the character (primary). Other concurrently live sessions and
pending ``delegate_task`` children are projected as sub-agent characters, the
same way Pixel Agents shows Claude Code sub-agents.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# Windows (seconds)
CANDIDATE_WINDOW_S = 3600      # sessions considered at all
THINK_WINDOW_S = 300           # last msg user/tool newer than this -> thinking
PENDING_TOOL_WINDOW_S = 900    # pending tool call newer than this -> working
WAITING_WINDOW_S = 1800        # finished turn newer than this -> waiting bubble
MESSAGE_SCAN_LIMIT = 40

INTERACTIVE_SOURCES = {"cli", "tui", "telegram", "discord", "slack", "whatsapp", "web", "dashboard", "api", "desktop", "signal", "matrix"}

READING_TOOLS = [
    "read_file", "search_files", "web_search", "web_extract", "skill_view", "skills_list",
    "session_search", "vision_analyze", "browser_snapshot", "browser_navigate", "browser_exec",
    "honcho_search", "honcho_context", "honcho_profile", "honcho_reasoning", "kanban_show",
    "kanban_list", "chat_history_lookup", "tool_search", "tool_describe", "Read", "Grep", "Glob",
    "WebFetch", "WebSearch",
]
SUBAGENT_TOOLS = ["delegate_task", "hermes_session"]
USER_INPUT_TOOLS = {"clarify"}


def _short(text: Any, n: int = 48) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _base(path: Any) -> str:
    s = str(path or "").rstrip("/")
    return s.rsplit("/", 1)[-1] or s


def describe_tool(name: str, args: Any) -> str:
    """Human status line for a tool call. Prefixes mirror Pixel Agents
    (Reading/Searching/Writing/Editing/Running/Subtask)."""
    a = args if isinstance(args, dict) else {}
    n = name or "tool"
    if n == "read_file":
        return f"Reading {_base(a.get('path'))}"
    if n == "search_files":
        return f"Searching {_short(a.get('pattern'), 32)}"
    if n == "write_file":
        return f"Writing {_base(a.get('path'))}"
    if n == "patch":
        return f"Editing {_base(a.get('path'))}"
    if n == "terminal":
        return f"Running: {_short(a.get('command'), 44)}"
    if n == "execute_code":
        return "Running: python"
    if n == "web_search":
        return f"Searching web: {_short(a.get('query'), 36)}"
    if n == "web_extract":
        urls = a.get("urls") or []
        return f"Fetching {_short(urls[0] if urls else '', 40)}"
    if n == "delegate_task":
        tasks = a.get("tasks") or []
        goal = tasks[0].get("goal") if tasks and isinstance(tasks[0], dict) else a.get("goal")
        return f"Subtask: {_short(goal, 44)}"
    if n == "skill_view":
        return f"Reading skill {a.get('name', '')}"
    if n == "skill_manage":
        return "Editing skills"
    if n in ("memory", "context_notes"):
        return "Writing memory"
    if n == "clarify":
        return "Asking you a question"
    if n == "todo" or n == "todo_list":
        return "Planning"
    if n.startswith("kanban_"):
        return f"Kanban: {n[7:]}"
    if n.startswith("browser"):
        return "Browsing the web"
    if n == "vision_analyze":
        return "Looking at an image"
    if n == "text_to_speech":
        return "Speaking"
    if n.startswith("mcp_") or n.startswith("mcp__"):
        return f"Using {n.split('__')[-1]}"
    return f"Using {n}"


def _parse_calls(raw: Optional[str]) -> list[dict]:
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except Exception:
        return []
    out = []
    for c in calls if isinstance(calls, list) else []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        name = fn.get("name") or c.get("name") or "tool"
        args_raw = fn.get("arguments") if fn else c.get("arguments")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except Exception:
            args = {}
        cid = c.get("id") or c.get("call_id") or f"{name}-{len(out)}"
        out.append({"id": str(cid), "name": name, "args": args})
    return out


def _connect(db: Path) -> Optional[sqlite3.Connection]:
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def session_state(conn: sqlite3.Connection, sess: sqlite3.Row, now: float) -> dict:
    """Derive the live state of one session from its tail of messages."""
    rows = conn.execute(
        "SELECT id, role, tool_call_id, tool_calls, tool_name, timestamp, finish_reason "
        "FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (sess["id"], MESSAGE_SCAN_LIMIT),
    ).fetchall()
    last_msg_ts = rows[0]["timestamp"] if rows else 0
    last_at = max(float(sess["last_activity_at"] or 0), float(last_msg_ts or 0), float(sess["started_at"] or 0))
    desc = (sess["last_activity_description"] or "").lower()
    state = {
        "id": sess["id"],
        "title": sess["title"] or "",
        "source": sess["source"] or "",
        "parent_session_id": sess["parent_session_id"],
        "started_at": sess["started_at"],
        "last_at": last_at,
        "message_count": sess["message_count"],
        "state": "idle",
        "tools": [],
        "awaiting_input": False,
        "permission": False,
        "activity": "",
    }
    if not rows:
        if now - last_at < THINK_WINDOW_S:
            state.update(state="active", activity="Starting…",
                         tools=[{"tool_id": f"think:{sess['id']}", "tool_name": "think", "status": "Starting…"}])
        return state

    answered = {r["tool_call_id"] for r in rows if r["role"] == "tool" and r["tool_call_id"]}
    last = rows[0]
    age = now - last_at

    # Find the most recent assistant message carrying tool calls, with pending calls.
    pending: list[dict] = []
    for r in rows:
        if r["role"] == "assistant" and r["tool_calls"]:
            calls = _parse_calls(r["tool_calls"])
            pending = [c for c in calls if c["id"] not in answered]
            break
        if r["role"] == "user":
            break

    if pending and age < PENDING_TOOL_WINDOW_S:
        tools = [{"tool_id": c["id"], "tool_name": c["name"], "status": describe_tool(c["name"], c["args"]), "args": c["args"]}
                 for c in pending]
        state.update(state="active", tools=tools, activity=tools[-1]["status"])
        if any(c["name"] in USER_INPUT_TOOLS for c in pending) or "approval" in desc:
            state["permission"] = True
        return state

    if last["role"] in ("user", "tool") and age < THINK_WINDOW_S:
        state.update(state="active", activity="Thinking…",
                     tools=[{"tool_id": f"think:{last['id']}", "tool_name": "think", "status": "Thinking…"}])
        return state

    if last["role"] == "assistant" and not last["tool_calls"] and age < WAITING_WINDOW_S:
        interactive = (sess["source"] or "") in INTERACTIVE_SOURCES
        state.update(state="waiting", awaiting_input=interactive,
                     activity="Waiting for you" if interactive else "Turn done")
        return state
    return state


def _rank(s: dict) -> tuple:
    order = {"active": 2, "waiting": 1, "idle": 0}
    return (order.get(s["state"], 0), s["last_at"])


def profile_activity(profile_home: str, now: Optional[float] = None) -> dict:
    """Return {status, awaiting_input, permission, activity, tools, subagents, sessions, last_at}."""
    now = now or time.time()
    db = Path(profile_home) / "state.db"
    result = {"status": "idle", "awaiting_input": False, "permission": False, "activity": "",
              "tools": [], "subagents": [], "sessions": [], "last_at": 0.0, "primary_session": None}
    conn = _connect(db)
    if conn is None:
        result["activity"] = "No session store"
        return result
    try:
        try:
            cands = conn.execute(
                "SELECT * FROM sessions WHERE ended_at IS NULL "
                "AND COALESCE(last_activity_at, started_at) > ? "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 12",
                (now - CANDIDATE_WINDOW_S,),
            ).fetchall()
        except sqlite3.Error:
            return result
        states = [session_state(conn, s, now) for s in cands]
        try:
            recent = conn.execute(
                "SELECT id, title, source, COALESCE(last_activity_at, started_at) AS at, ended_at "
                "FROM sessions WHERE source != 'subagent' "
                "ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 6"
            ).fetchall()
            result["last_at"] = float(recent[0]["at"] or 0) if recent else 0.0
            result["recent_sessions"] = [
                {"id": r["id"], "title": r["title"] or "", "source": r["source"], "at": r["at"], "ended": r["ended_at"] is not None}
                for r in recent
            ]
        except sqlite3.Error:
            result["recent_sessions"] = []
    finally:
        conn.close()

    top_level = [s for s in states if s["source"] != "subagent"]
    children = [s for s in states if s["source"] == "subagent"]
    live = sorted([s for s in top_level if s["state"] != "idle"], key=_rank, reverse=True)
    result["sessions"] = [
        {k: s[k] for k in ("id", "title", "source", "state", "activity", "last_at", "message_count")} for s in states
    ]
    if not live:
        if result["last_at"]:
            result["activity"] = "Idle"
        return result

    primary = live[0]
    result["primary_session"] = primary["id"]
    result["last_at"] = max(result["last_at"], primary["last_at"])
    tools: list[dict] = []
    subs: list[dict] = []

    # Other concurrently live sessions -> helper sub-agent characters.
    for s in live[1:]:
        if s["state"] != "active":
            continue
        tid = f"sess:{s['id']}"
        label = _short(s["title"] or f"{s['source']} session", 40)
        tools.append({"tool_id": tid, "tool_name": "hermes_session", "status": f"Subtask: {label}"})
        subs.append({"parent_tool_id": tid, "label": label,
                     "tools": [{"tool_id": t["tool_id"], "status": t["status"]} for t in s["tools"]],
                     "permission": s["permission"]})

    # Primary session tools; delegate_task fans out into one sub-agent per child.
    kids = sorted([c for c in children if c["parent_session_id"] == primary["id"]], key=lambda c: c["started_at"] or 0)
    for t in primary["tools"]:
        if t["tool_name"] != "delegate_task":
            tools.append({k: t[k] for k in ("tool_id", "tool_name", "status")})
            continue
        goals = []
        for task in (t.get("args") or {}).get("tasks") or []:
            if isinstance(task, dict):
                goals.append(task.get("goal") or "")
        if not goals:
            goals = [(t.get("args") or {}).get("goal") or "subtask"]
        n = max(len(goals), len([k for k in kids if k["state"] == "active"]) or 0, 1)
        for i in range(n):
            kid = kids[i] if i < len(kids) else None
            label = _short((kid and kid["title"].removeprefix("Subagent: ")) or (goals[i] if i < len(goals) else "subtask"), 40)
            tid = f"{t['tool_id']}#{i}"
            tools.append({"tool_id": tid, "tool_name": "delegate_task", "status": f"Subtask: {label}"})
            subs.append({"parent_tool_id": tid, "label": label,
                         "tools": [{"tool_id": x["tool_id"], "status": x["status"]} for x in (kid["tools"] if kid else [])],
                         "permission": bool(kid and kid["permission"])})

    result.update(
        status=primary["state"] if primary["state"] == "waiting" and len(tools) == 0 else ("active" if tools else primary["state"]),
        awaiting_input=primary["awaiting_input"],
        permission=primary["permission"],
        activity=primary["activity"] or (tools[-1]["status"] if tools else ""),
        tools=tools,
        subagents=subs,
    )
    return result


def all_activity(profiles: Iterable[dict], now: Optional[float] = None) -> dict[str, dict]:
    now = now or time.time()
    out = {}
    for p in profiles:
        name = p.get("name")
        home = p.get("path")
        if not name or not home:
            continue
        try:
            out[name] = profile_activity(home, now)
        except Exception as exc:  # one broken store must not blank the office
            out[name] = {"status": "idle", "activity": f"error: {exc.__class__.__name__}", "tools": [], "subagents": [],
                         "sessions": [], "awaiting_input": False, "permission": False, "last_at": 0.0}
    return out
