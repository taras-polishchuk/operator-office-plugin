"""Approval kanban bridge.

Used by both the dashboard plugin (Operator Office) and the gateway plugin
(Telegram approvals). Reads go through ``hermes_cli.kanban_db`` (direct DB,
the gateway cannot make HTTP calls to the dashboard); mutations go through
the Kanban CLI subprocess so both plugins stay on the same code path.

Owns:
  * read_task(board, task_id) -> (task, events)   via hermes_cli.kanban_db
  * build_action(task, board, kind)              -> dict (kanban.unblock default)
  * verify(row)                                  -> ('ok'|'stale', reason)
  * apply(row, decision, via)                    -> dict; mutates Kanban via CLI
  * sync_gates(board, store, profile_names, now) -> upserts pending rows
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

# Local package (approval_store is sibling; office_control is sibling).
import approval_store
import office_control as oc

# `hermes_cli` lives in the user's Hermes venv; the dashboard plugin is loaded
# from that venv too, so a plain import works.
try:
    from hermes_cli import kanban_db as kb  # type: ignore
    from hermes_cli import kanban_db_connect as kbc  # type: ignore
except Exception:  # noqa: BLE001  - tests may stub
    kb = None  # type: ignore
    kbc = None  # type: ignore


# ── paths ─────────────────────────────────────────────────────────────────────

def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        p = Path(env)
        if p.parent.name == "profiles":
            p = p.parent.parent
        return p
    return Path.home() / ".hermes"


def _dashboard_base() -> str:
    port = os.environ.get("HERMES_DASHBOARD_PORT", "9119")
    return f"http://127.0.0.1:{port}"


# ── read ──────────────────────────────────────────────────────────────────────


def _conn_for_board(board: Optional[str]):
    if kb is None or kbc is None:
        raise RuntimeError("hermes_cli.kanban_db unavailable in this environment")
    return kbc.connect(board=board)


def read_task(board: str, task_id: str) -> tuple[Optional[dict], list[dict]]:
    """Return (task_dict, events) read live from Hermes Kanban.

    Task dict uses the canonical field names office_control expects:
    id, status, assignee, current_run_id, block_kind, title, latest_summary.
    """
    if kb is None or kbc is None:
        raise RuntimeError("hermes_cli.kanban_db unavailable in this environment")
    # ``connect_closing`` is a @contextlib.contextmanager; must enter it as a
    # `with` block so the underlying sqlite3 connection actually gets yielded
    # (otherwise ``kb.*(conn, ...)`` fails with
    # ``AttributeError: '_GeneratorContextManager' object has no attribute 'execute'``).
    with kbc.connect_closing(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if not task:
            return None, []
        events = kb.list_events(conn, task_id)
        # ``latest_summary`` is NOT a Task attribute; ``hermes_cli.kanban_db``
        # exposes it as a function that picks the newest non-empty run summary.
        latest_summ = kb.latest_summary(conn, task_id)
        tdict: dict[str, Any] = {
            "id": task.id,
            "status": task.status,
            "assignee": task.assignee,
            "current_run_id": task.current_run_id,
            "block_kind": task.block_kind,
            "title": task.title,
            "latest_summary": latest_summ,
        }
        # Attach metadata/approval_action if present
        for k in ("tenant", "body", "metadata"):
            v = getattr(task, k, None)
            if v is not None:
                tdict[k] = v
        return tdict, [ev.__dict__ for ev in events]


# ── action + fingerprint ──────────────────────────────────────────────────────


def _latest_event_id(events: list[dict], kind: Optional[str] = None) -> Optional[int]:
    """Largest ``id`` in *events* whose ``kind`` matches ``kind`` (any when None).

    Returns ``None`` when there is no matching event.
    """
    best: Optional[int] = None
    for ev in events:
        if kind is not None and ev.get("kind") != kind:
            continue
        try:
            eid = int(ev.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if best is None or eid > best:
            best = eid
    return best


def episode_for(task: dict, events: list[dict], kind: str) -> str:
    """Stable id of the gate episode this approval binds to.

    ``kanban.needs_input``: id of the latest ``blocked`` event (the
    worker just blocked for human input — a new block means a new
    episode, so an old approved/rejected row doesn't resurrect).

    ``lane.gate``: id of the latest event that put the task into
    ``ready`` (typically ``unblocked`` / ``reclaimed`` / ``status`` /
    ``changes_requested`` — i.e. anything that flipped the card back
    into ``ready``). When the task is still ready and we can't find a
    matching event, fall back to the latest event id, then to the
    task fingerprint.

    Always returns a non-empty string.
    """
    if kind == "kanban.needs_input":
        eid = _latest_event_id(events, "blocked")
        if eid is not None:
            return f"block:{eid}"
    elif kind == "lane.gate":
        # The "ready" transition is whatever the latest lifecycle event
        # that ended up putting the task into ``ready`` was. We don't
        # track transitions in events, so use the latest event whose
        # kind is in the lifecycle set that may flip status.
        for k in ("unblocked", "reclaimed", "changes_requested", "status",
                  "review_reopened", "stale"):
            eid = _latest_event_id(events, k)
            if eid is not None:
                return f"ready:{eid}"
        eid = _latest_event_id(events)
        if eid is not None:
            return f"ready:{eid}"
    # Fallback: fingerprint.
    return f"fp:{oc.task_fingerprint(task, events)}"


def build_action(task: dict, board: str, kind: str) -> dict:
    """Build the action dict this approval binds to.

    Preference: ``task.metadata.approval_action`` when present (the worker
    stamps the rich action), else the canonical ``kanban.unblock`` shape.
    """
    md = task.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = {}
    if isinstance(md, dict):
        act = md.get("approval_action")
        if isinstance(act, dict):
            return act
    # Lane gates: bind to the lane template (so changing the run/template
    # invalidates the approval).
    title = task.get("title") or ""
    parsed = oc.lanes.parse_marker(title)
    if kind == "lane.gate" and parsed:
        tid, run, _ = parsed
        return {"type": "lane.gate", "board": board, "task_id": task["id"],
                "template_id": tid, "run": run, "title": oc.redact(title)}
    return {"type": "kanban.unblock", "board": board, "task_id": task["id"],
            "title": oc.redact(title)}


def verify_row(row: dict, *, get_task: Callable[[str, str], tuple[Optional[dict], list[dict]]]) -> tuple[str, Optional[str]]:
    """Re-read live Kanban for the row's (board, task_id) and confirm binding.

    Returns ``('ok'|'stale', reason)``. stale -> CAS to 'stale'.
    """
    board = row.get("board")
    task_id = row.get("task_id")
    if not board or not task_id:
        return "stale", "row missing board/task_id"
    task, events = get_task(board, task_id)
    if not task:
        return "stale", "task no longer exists"
    fp = oc.task_fingerprint(task, events)
    if fp != row.get("fingerprint"):
        return "stale", f"fingerprint changed ({row.get('fingerprint')} -> {fp})"
    # Action digest: rebuild action from the live task, compare digest.
    action = build_action(task, board, row.get("kind") or "")
    if approval_store.action_digest(action) != row.get("action_digest"):
        return "stale", "action digest changed"
    return "ok", None


# ── apply (kanban mutation after CAS win) ─────────────────────────────────────


def _cli_run(args: list[str], *, timeout: int = 60) -> tuple[bool, str]:
    env = {**os.environ, "HERMES_HOME": str(_hermes_home())}
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"
    if p.returncode != 0:
        return False, oc.redact((p.stdout or "") + (p.stderr or ""))[-800:]
    return True, oc.redact(p.stdout or "").strip()[:400]


def _cli_kanban(board: str, *verb: str) -> tuple[bool, str]:
    """Invoke the on-disk ``hermes`` shell wrapper, not ``sys.executable -m
    hermes_cli.main``.

    The dashboard plugin runs under whichever python the service was started
    with; the live integration test runs under the hermes tools python which
    does NOT have ``hermes_cli`` on its path. The ``hermes`` shell wrapper
    (the same one systemd uses for ``hermes-dashboard.service``) handles the
    hermes-agent source dir injection, so it works from either caller. This
    matches WP-14: every ``hermes_cli.kanban`` mutation must go through the
    canonical launcher, not be coupled to ``sys.executable``.
    """
    return _cli_run(["hermes", "kanban", "--board", board, *verb])


def _cli_kanban_with_text(board: str, verb: list[str], body: str) -> tuple[bool, str]:
    """Like _cli_kanban but the body is appended to argv instead of stdin.

    ``hermes_cli.kanban comment`` joins all positional ``text`` args with a
    single space and strips, so a multi-line body passed as one argv element
    survives intact (the comment CLI does NOT read stdin). Inline argv keeps
    the apply path self-contained and avoids spawning ``bash -c`` for quoting.
    """
    return _cli_kanban(board, *verb, body)


# ── HTTP upstream for things the Kanban CLI cannot do (lane gate context). ──


def _http_get_json(path: str, *, token: Optional[str] = None, timeout: float = 15) -> Any:
    url = _dashboard_base() + path
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               **({"X-Hermes-Session-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"http {exc.code}", "body": oc.redact(exc.read().decode("utf-8", errors="replace"))[:500]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}"}


def _lane_gate_context(board: str, gate_id: str, *, get_details: Optional[Callable] = None) -> tuple[Optional[str], Optional[str], dict]:
    """Return (template_id, run, details_map) for a lane gate task.

    ``get_details`` is an optional injection point for tests (returns
    ``{step_key: {"task": ..., "runs": [...], "attachments": [...], "events": [...]}}``).
    Falls back to the dashboard HTTP if not provided.
    """
    if get_details is not None:
        task_show = get_details("show", board, gate_id)
        title = (task_show or {}).get("task", {}).get("title", "")
        parsed = oc.lanes.parse_marker(title)
        if not parsed:
            return None, None, {}
        tid, run, _ = parsed
        # caller-provided get_details is a step resolver
        details = get_details("steps", board, run)
        return tid, run, details or {}

    task_show = _http_get_json(f"/api/plugins/kanban/tasks/{urllib.parse.quote(gate_id)}?board={urllib.parse.quote(board)}")
    if not isinstance(task_show, dict) or "task" not in task_show:
        return None, None, {}
    title = task_show["task"].get("title") or ""
    parsed = oc.lanes.parse_marker(title)
    if not parsed:
        return None, None, {}
    tid, run, _ = parsed
    # Discover run tasks
    board_payload = _http_get_json(f"/api/plugins/kanban/board?board={urllib.parse.quote(board)}", timeout=45)
    run_tasks = []
    for col in (board_payload.get("columns") or []):
        for t in col.get("tasks") or []:
            p = oc.lanes.parse_marker(t.get("title") or "")
            if p and p[1] == run:
                run_tasks.append(t)
    details: dict[str, dict] = {}
    for t in run_tasks:
        step = oc.lanes.parse_marker(t["title"])[2]
        if step == oc.lanes.TEMPLATES[tid]["steps"][-1]["key"]:
            continue
        d = _http_get_json(f"/api/plugins/kanban/tasks/{urllib.parse.quote(t['id'])}?board={urllib.parse.quote(board)}", timeout=30)
        details[step] = {"task": d.get("task", {}), "runs": d.get("runs", []),
                         "attachments": d.get("attachments", []), "events": d.get("events", [])}
    return tid, run, details


# ── HTTP upload attachment (lane record) ───────────────────────────────────────

BOUNDARY = "----office"


def _upload_attachment(board: str, task_id: str, filename: str, text: str, *, token: Optional[str] = None) -> Optional[dict]:
    """Upload a text attachment via the dashboard HTTP API.

    Used to drop the lane record next to the gate task.
    """
    import uuid as _uuid
    boundary = BOUNDARY + _uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"uploaded_by\"\r\n\r\noperator-office\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        "Content-Type: text/markdown\r\n\r\n"
    ).encode() + text.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode()
    h = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        h["X-Hermes-Session-Token"] = token
    req = urllib.request.Request(
        _dashboard_base() + f"/api/plugins/kanban/tasks/{urllib.parse.quote(task_id)}/attachments?board={urllib.parse.quote(board)}",
        data=body, method="POST", headers=h,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("attachment")
    except Exception:  # noqa: BLE001
        return None


def _lane_record(tid: str, run: str, board: str, gate_id: str, decision: str, note: str,
                 policy: dict, execution: Optional[dict], details: dict, request_id: str) -> str:
    """Format the markdown lane record (mirror plugin_api._lane_record)."""
    lines = [f"# Lane record: {oc.lanes.TEMPLATES[tid]['name']} ({tid}) run {run}", "",
             f"- board: {board}", f"- gate task: {gate_id}", f"- decision: {decision} (operator, Approval store)",
             f"- request: {request_id}", f"- decided_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
             f"- note: {oc.redact(note) or '-'}", "", "## Policy checks (server-side, before apply)", ""]
    lines += [f"- [{'x' if c['ok'] else ' '}] {c['name']}: {c['detail']}" for c in policy.get("checks", [])]
    lines += ["", "## Steps", ""]
    for step, d in details.items():
        t = d["task"]
        runs = ", ".join(f"{r.get('profile')}:{r.get('outcome')}" for r in d.get("runs", []))
        atts = ", ".join(a.get("filename") or "" for a in d.get("attachments", []))
        lines.append(f"- {step}: {t.get('id')} status={t.get('status')} runs=[{runs}] attachments=[{atts}]")
        summ = oc.redact((t.get("latest_summary") or "")[:500])
        if summ:
            lines.append(f"  summary: {summ}")
    lines += ["", "## Execution", "", f"- {json.dumps(execution) if execution else 'none (rejected)'}", ""]
    return "\n".join(lines)


# ── apply (the only place mutates Kanban after CAS win) ───────────────────────


def apply_needs_input(row: dict, decision: str, via: str, request_id: str) -> dict:
    """Apply kanban.needs_input approve/reject via Kanban CLI."""
    board = row["board"]
    task_id = row["task_id"]
    verdict = "APPROVED" if decision == "approve" else "REJECTED (do not proceed)"
    body = f"[operator decision via {via}] {verdict}\nrequest={request_id}"
    ok, out = _cli_kanban_with_text(board, ["comment", task_id], body)
    if not ok:
        return {"ok": False, "stage": "comment", "error": out}
    if decision == "approve":
        ok, out = _cli_kanban(board, "unblock", task_id)
        if not ok:
            return {"ok": False, "stage": "unblock", "error": out}
        return {"ok": True, "status": "ready", "verdict": verdict}
    # reject: stay blocked (no unblock)
    return {"ok": True, "status": "blocked", "verdict": verdict}


def apply_lane_gate(row: dict, decision: str, via: str, request_id: str, *,
                    get_lane_context: Optional[Callable] = None,
                    upload_attachment: Optional[Callable] = None,
                    write_lane_approvals: Optional[Callable] = None,
                    audit: Optional[Callable] = None) -> dict:
    """Apply lane.gate approve/reject (the reusable portion of plugin_api._apply_lane_gate).

    The injected callables are optional test seams; defaults read the
    dashboard HTTP (gate context + attachment upload) and write
    ``lane-approvals.json`` via the plugin_api state.
    """
    import lanes  # sibling module, same sys.path
    board = row["board"]
    task_id = row["task_id"]
    note = ""
    if decision == "reject":
        tid, run, details = _lane_gate_context(board, task_id, get_details=get_lane_context)
        if not tid:
            return {"ok": False, "stage": "context", "error": "gate has no lane marker"}
        policy = lanes.gate_policy(tid, details)
        rec = _lane_record(tid, run, board, task_id, "reject", note, policy, None, details, request_id)
        att = (upload_attachment or _upload_attachment)(board, task_id, f"lane-record-{run}.md", rec)
        ok, out = _cli_kanban_with_text(board, ["comment", task_id],
                                        f"[operator decision via {via}] REJECTED\nrequest={request_id}")
        if not ok:
            return {"ok": False, "stage": "comment", "error": out}
        ok, out = _cli_kanban(board, "block", task_id)
        if not ok:
            return {"ok": False, "stage": "block", "error": out}
        if audit:
            audit({"action": "lane.gate.decide", "decision": decision, "via": via,
                   "board": board, "task_id": task_id, "request_id": request_id,
                   "result": "rejected",
                   "record_attachment": (att or {}).get("id") if isinstance(att, dict) else None})
        return {"ok": True, "status": "blocked", "verdict": "rejected", "run": run,
                "record_attachment": (att or {}).get("id") if isinstance(att, dict) else None}

    # approve
    tid, run, details = _lane_gate_context(board, task_id, get_details=get_lane_context)
    if not tid:
        return {"ok": False, "stage": "context", "error": "gate has no lane marker"}
    policy = lanes.gate_policy(tid, details)
    failed = [c for c in policy["checks"] if not c["ok"]]
    if failed:
        return {"ok": False, "stage": "policy", "error": "lane policy not satisfied: " +
                "; ".join(f"{c['name']} ({c['detail']})" for c in failed),
                "policy": policy}
    execution = lanes.execute_adapter(lanes.TEMPLATES[tid]["adapter"])
    rec = _lane_record(tid, run, board, task_id, "approve", note, policy, execution, details, request_id)
    att = (upload_attachment or _upload_attachment)(board, task_id, f"lane-record-{run}.md", rec)
    if write_lane_approvals is not None:
        try:
            write_lane_approvals(task_id, {"board": board, "run": run, "template": tid,
                                           "request_id": request_id, "at": time.time(),
                                           "execution": execution})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "stage": "lane_approvals_write", "error": str(exc)}
    summary = json.dumps({"approved_via": via, "request_id": request_id, "run": run,
                          "policy_checks": policy["checks"], "execution": execution})
    ok, out = _cli_kanban(board, "complete", task_id,
                          "--summary", f"Approved by operator via {via}. Execution: "
                                       f"{execution['status']} ({execution.get('reason','')})"[:400],
                          "--metadata", summary)
    if not ok:
        return {"ok": False, "stage": "complete", "error": out}
    if audit:
        audit({"action": "lane.gate.decide", "decision": decision, "via": via,
               "board": board, "task_id": task_id, "request_id": request_id,
               "result": "applied", "run": run,
               "execution": execution, "policy": policy,
               "record_attachment": (att or {}).get("id") if isinstance(att, dict) else None})
    return {"ok": True, "status": "done", "verdict": "approved", "run": run,
            "policy": policy, "execution": execution,
            "record_attachment": (att or {}).get("id") if isinstance(att, dict) else None}


def apply(row: dict, decision: str, via: str, request_id: str,
          *, get_lane_context: Optional[Callable] = None,
          upload_attachment: Optional[Callable] = None,
          write_lane_approvals: Optional[Callable] = None,
          audit: Optional[Callable] = None) -> dict:
    """Apply the decision to Kanban. Caller already won the store CAS."""
    kind = row.get("kind")
    if kind == "kanban.needs_input":
        return apply_needs_input(row, decision, via, request_id)
    if kind == "lane.gate":
        return apply_lane_gate(row, decision, via, request_id,
                               get_lane_context=get_lane_context,
                               upload_attachment=upload_attachment,
                               write_lane_approvals=write_lane_approvals,
                               audit=audit)
    return {"ok": False, "stage": "dispatch", "error": f"unknown kind: {kind!r}"}


# ── sync_gates ────────────────────────────────────────────────────────────────


def _gate_tasks(board: str, profile_names: set[str]) -> list[tuple[str, dict, list[dict]]]:
    """Return [(task_id, task, events)] for every task whose approval_kind is non-None.

    Mirrors the projection in plugin_api._office_state: re-reads every open
    task, computes fingerprint + kind, and yields it.
    """
    if kb is None or kbc is None:
        return []
    # ``connect_closing`` is a @contextlib.contextmanager; must enter it as a
    # `with` block so the underlying sqlite3 connection actually gets yielded
    # (otherwise ``kb.list_tasks/list_events(conn, ...)`` fails with
    # ``AttributeError: '_GeneratorContextManager' object has no attribute 'execute'``).
    with kbc.connect_closing(board=board) as conn:
        tasks = kb.list_tasks(conn, include_archived=False, limit=400,
                              order_by="priority-desc")
        out = []
        for t in tasks:
            status = t.status
            if status not in oc.OPEN_STATUSES:
                continue
            events = kb.list_events(conn, t.id)
            # ``latest_summary`` is a function, not a Task attribute.
            latest_summ = kb.latest_summary(conn, t.id)
            tdict = {"id": t.id, "status": t.status, "assignee": t.assignee,
                     "current_run_id": t.current_run_id, "block_kind": t.block_kind,
                     "title": t.title, "latest_summary": latest_summ}
            kind = oc.approval_kind(tdict, profile_names)
            if kind:
                out.append((t.id, tdict, [ev.__dict__ for ev in events]))
        return out


def sync_gates(board: str, store: approval_store.Store, profile_names: set[str],
               now: Optional[float] = None, *,
               get_task: Optional[Callable] = None,
               list_tasks: Optional[Callable] = None) -> dict:
    """Walk visible gates, upsert pending store rows.

    Returns counters so callers (the gateway loop) can log activity.
    Skipped when the dashboard plugin is reachable and prefers its own
    HTTP-derived queue (caller chooses).
    """
    upserted = 0
    superseded = 0
    skipped = 0
    if list_tasks is not None:
        items = list_tasks(board, profile_names)
    else:
        items = [(tid, t, ev) for (tid, t, ev) in _gate_tasks(board, profile_names)]
    for task_id, task, events in items:
        kind = oc.approval_kind(task, profile_names)
        if kind not in ("kanban.needs_input", "lane.gate"):
            skipped += 1
            continue
        fp = oc.task_fingerprint(task, events)
        ep = episode_for(task, events, kind)
        action = build_action(task, board, kind)
        summary = {"task_id": task_id, "title": oc.redact(task.get("title") or "")[:200],
                   "assignee": task.get("assignee"), "status": task.get("status"),
                   "block_kind": task.get("block_kind"), "expected_status": task.get("status")}
        row, created = store.upsert_request(board, task_id, kind, fp, action, summary,
                                            ttl_s=oc.APPROVAL_TTL_S, now=now, episode=ep)
        if created:
            upserted += 1
        elif row.get("status") == "superseded":
            superseded += 1
    return {"upserted": upserted, "superseded": superseded, "skipped": skipped,
            "scanned": len(items)}