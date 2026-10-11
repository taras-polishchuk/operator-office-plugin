"""Review independence guard (WP-8: independent review routing).

Hermes Kanban has no built-in invariant that prevents the implementer of a task from also being the
reviewer (``hermes_cli/kanban_db.py:3490–3506``). The lane gate already catches this at the
template/runtime boundary (see ``lanes.gate_policy``), but the inline Kanban ``review`` status — used
by ad-hoc workers and short tasks with no lane — has no analog. This module provides a self-contained
server-side guard that scans a configurable set of boards and either flags the offending card as an
Office incident or reassigns it to ``qa``.

Constraints (from the WP-8 contract):

* Pure functions: no HTTP, no FastAPI, no PTB. Side effects are passed in as callables (an
  ``event_reader`` to fetch a task's events, and optional ``reassign`` callable for the
  ``mode='reassign'`` variant). This keeps unit tests hermetic.
* Boards are read from settings key ``reviewGuard.boards`` (default ``['operator-office-lab']``).
  Live ``Kanban core`` is untouched, so non-lab boards opt in by config — never by code change.
* Idempotent: in ``'reassign'`` mode the guard writes a marker audit row (via the same SQLite WAL
  approvals DB that the lane-approval flow uses — schema append) so a re-run after the operator
  already moved the card back to its proper reviewer does NOT reassign a second time.
* Kill switch: settings key ``reviewGuard.enabled`` (default ``True`` for lab-only deployment).
  Setting it to ``False`` short-circuits the guard.
* The guard never touches tasks not in ``status='review'`` and never edits history.

Scope: this file owns finding + applying the guard. Wiring the guard into the Telegram approvals
loop (one call line) lives in ``gateway-plugin/pixel-office-approvals``; see ``tg_approvals.tick_review_guard``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("pixel-office.review_guard")

DEFAULT_GUARD_BOARDS: tuple[str, ...] = ("operator-office-lab",)
DEFAULT_REVIEWER = "qa"
MARKER_AUDIT_EVENT = "review_guard_reassigned"


# ── settings helpers ────────────────────────────────────────────────────────


def _state_dir() -> Path:
    """Mirror approval_store's default: ~/.hermes/state/pixel-office/. Tests can override."""
    import os
    base = os.environ.get("PIXEL_OFFICE_STATE_DIR")
    if base:
        return Path(base)
    return Path.home() / ".hermes" / "state" / "pixel-office"


def guard_db_path() -> Path:
    """Auditable persistence for ``reassign`` mode. Lives in the same state dir as approvals.db."""
    return _state_dir() / "review-guard.db"


def load_guard_settings(settings: dict) -> dict:
    """Pull just the ``reviewGuard`` subtree from a settings dict (defensive: missing -> defaults).

    Schema:
        reviewGuard:
          enabled: bool      (default True)
          boards: [str]      (default DEFAULT_GUARD_BOARDS)
          mode: 'flag'|'reassign'  (default 'flag')
          reviewer: str      (default 'qa'; profile name to reassign to)
    """
    guard = (settings or {}).get("reviewGuard") if isinstance(settings, dict) else None
    if not isinstance(guard, dict):
        guard = {}
    boards = guard.get("boards")
    boards_clean: list[str] = []
    if isinstance(boards, list):
        for b in boards:
            if isinstance(b, str) and b.strip():
                boards_clean.append(b.strip())
    if not boards_clean:
        boards_clean = list(DEFAULT_GUARD_BOARDS)
    mode = str(guard.get("mode") or "flag").lower()
    if mode not in ("flag", "reassign"):
        mode = "flag"
    reviewer = str(guard.get("reviewer") or DEFAULT_REVIEWER).strip() or DEFAULT_REVIEWER
    return {
        "enabled": bool(guard.get("enabled", True)),
        "boards": boards_clean,
        "mode": mode,
        "reviewer": reviewer,
    }


# ── guard audit store (idempotency marker for ``reassign``) ────────────────


_GUARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS guard_actions (
    board        TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    mode         TEXT NOT NULL,
    from_profile TEXT,
    to_profile   TEXT,
    at           INTEGER NOT NULL,
    note         TEXT,
    PRIMARY KEY (board, task_id, mode)
);
CREATE INDEX IF NOT EXISTS ix_guard_actions_at ON guard_actions(at DESC);
"""


class GuardLedger:
    """Append-only SQLite ledger that records ``review_guard_reassigned`` markers.

    The PRIMARY KEY on (board, task_id, mode) is what gives ``reassign`` mode its idempotency: a
    second tick that finds the card still stuck (e.g. assignment did not propagate yet) will skip
    the move instead of bouncing it. A failure during the upstream write also leaves a marker so
    a retry does not double-fire a Telegram notification.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else guard_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_GUARD_SCHEMA)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def already_done(self, board: str, task_id: str, mode: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM guard_actions WHERE board=? AND task_id=? AND mode=?",
                (board, task_id, mode),
            ).fetchone()
            return cur is not None

    def mark(self, board: str, task_id: str, mode: str, from_profile: Optional[str],
             to_profile: Optional[str], note: Optional[str] = None, now: Optional[float] = None) -> None:
        ts = int(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO guard_actions(board, task_id, mode, from_profile, "
                "  to_profile, at, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (board, task_id, mode, from_profile, to_profile, ts, note),
            )


# ── pure logic ──────────────────────────────────────────────────────────────


def implementer_of(events: Iterable[dict]) -> Optional[str]:
    """Implementer recorded by the most recent ``review_requested`` task event.

    Returns ``None`` when there is no such event or the payload is missing the field. Mirrors the
    Hermes core's recording convention at ``kanban_db.py:3539–3547``.
    """
    latest: Optional[str] = None
    for e in events or []:
        if e.get("kind") == "review_requested":
            payload = e.get("payload") or {}
            if isinstance(payload, dict):
                latest = payload.get("implementer") or latest
    return latest


def is_self_review(task: dict, events: Iterable[dict], *, profile_names: Optional[set[str]] = None
                   ) -> Optional[str]:
    """Return the conflicting profile iff ``task`` is a self-review; ``None`` otherwise.

    A self-review is a Kanban card whose current ``assignee`` equals the implementer recorded by
    the latest ``review_requested`` task event — meaning the same profile wrote the code AND is
    now sitting in the review slot. The dispatcher is honest-by-default: it claims whatever
    ``tasks.assignee`` says, so this is the actual leak path (see WP-5 1e).

    Args:
      task: a Kanban task dict with at least ``status`` and ``assignee`` fields.
      events: the task's events list (any order — only ``kind='review_requested'`` is read).
      profile_names: when provided, refuse to flag a ``taras``-style non-profile assignee as a
        self-review. The guard is for agent profiles only; operator/human lanes are NOT agent
        self-reviews, they are operator-claimed reviews and are out of scope.

    Returns the implementer (== assignee) profile name on conflict, ``None`` if it's not a
    self-review for the configured scope.
    """
    if not isinstance(task, dict):
        return None
    if task.get("status") != "review":
        return None
    assignee = task.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return None
    if profile_names is not None and assignee not in profile_names:
        return None
    impl = implementer_of(events)
    if not impl or not isinstance(impl, str):
        return None
    if impl.strip() == assignee.strip():
        return impl.strip()
    return None


def find_self_review_tasks(board: str, tasks: Iterable[dict], events_for: Callable[[str], list[dict]],
                           *, profile_names: Optional[set[str]] = None) -> list[dict]:
    """Scan a board's tasks and return the self-review conflicts.

    Args:
      board: board name (used only for the returned finding shape; no filtering).
      tasks: an iterable of task dicts. Status / assignee are read here.
      events_for: callable ``task_id -> list of event dicts``. Pure injection point so tests can
        pass a dict-backed fake.
      profile_names: optional scope filter (same semantics as ``is_self_review``).

    Returns one finding per self-review task:
      ``{"kind": "Incident", "id": "...self-review:<task_id>", "source": "office.review_guard",
      "severity": "warning", "title": "self-review: <task_id> by <profile>",
      "task_id": "...", "task_title": "...", "board": "<board>", "detail": "<detail>"}``
    """
    out: list[dict] = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not tid:
            continue
        bad = is_self_review(t, events_for(tid), profile_names=profile_names)
        if not bad:
            continue
        out.append({
            "kind": "Incident",
            "id": f"review_guard:self-review:{tid}",
            "source": "office.review_guard",
            "severity": "warning",
            "title": f"self-review: {tid} by {bad}",
            "task_id": tid,
            "task_title": t.get("title") or "",
            "board": board,
            "detail": (f"assignee '{bad}' is the implementer recorded by the latest "
                       f"review_requested event; reviewer and implementer must differ"),
        })
    return out


# ── reassign wrapper (side-effecting, but the side effect is in the callable) ──


def reassign_self_review(task: dict, events: list[dict], *, board: str, reviewer: str,
                         guard_ledger: GuardLedger,
                         reassign_fn: Callable[[str, str, str, str], dict],
                         now: Optional[float] = None) -> dict:
    """Apply the guard in ``reassign`` mode. Idempotent; never touches history.

    Args:
      task: the offending task (must already look like a self-review; caller checked).
      events: events for the task (used to recover implementer for audit).
      board, reviewer: where to move the card to.
      guard_ledger: marker store; the second invocation with the same (board, task_id, 'reassign')
        tuple is a no-op.
      reassign_fn: ``(board, task_id, from_profile, to_profile) -> dict`` — typically a thin
        wrapper around the ``hermes kanban --board B reassign ...`` CLI. The callable is
        injected so unit tests can assert the call without subprocess.
      now: override for the marker timestamp.

    Returns ``{"status": "applied"|"skipped"|"not_self_review", "from": <profile>, "to": <reviewer>,
                "task_id": ..., "board": ..., "marker": {...}}``.
    """
    if not isinstance(task, dict):
        return {"status": "not_self_review", "task_id": None, "board": board}
    tid = task.get("id")
    if not isinstance(tid, str) or not tid:
        return {"status": "not_self_review", "task_id": tid, "board": board}
    if task.get("status") != "review":
        return {"status": "not_self_review", "task_id": tid, "board": board}
    if guard_ledger.already_done(board, tid, "reassign"):
        return {"status": "skipped", "task_id": tid, "board": board,
                "from": task.get("assignee"), "to": reviewer,
                "marker": {"reason": "already reassigned by guard"}}
    bad = is_self_review(task, events)
    if not bad:
        return {"status": "not_self_review", "task_id": tid, "board": board,
                "from": task.get("assignee"), "to": reviewer}
    from_profile = task.get("assignee") or ""
    res = reassign_fn(board, tid, from_profile, reviewer)
    guard_ledger.mark(board, tid, "reassign", from_profile, reviewer,
                      note=f"flagged: implementer '{bad}' == reviewer '{from_profile}'",
                      now=now)
    return {"status": "applied", "task_id": tid, "board": board,
            "from": from_profile, "to": reviewer, "result": res,
            "marker": {"event": MARKER_AUDIT_EVENT,
                       "task_id": tid, "board": board,
                       "from": from_profile, "to": reviewer}}


# ── tick helper used by the gateway loop ────────────────────────────────────


def tick_review_guard(*, settings: dict, boards_payload_for: Callable[[str], dict],
                      events_for: Callable[[str, str], list[dict]],
                      guard_ledger: Optional[GuardLedger] = None,
                      reassign_fn: Optional[Callable[[str, str, str, str], dict]] = None,
                      profile_names_provider: Optional[Callable[[], set[str]]] = None,
                      now: Optional[float] = None) -> dict:
    """Single guard tick. Pure (no network) when ``reassign_fn`` is None — ``flag`` mode.

    Args:
      settings: full settings.json dict.
      boards_payload_for: ``board -> board payload dict``. The payload is expected to contain
        ``columns[*].tasks[*]`` (Kanban ``GET /api/plugins/kanban/board`` shape).
      events_for: ``(board, task_id) -> events list`` — pulled per self-review candidate.
      guard_ledger: optional shared ledger for ``reassign`` mode idempotency. Required when
        ``mode='reassign'``.
      reassign_fn: required only when ``mode='reassign'``.
      profile_names_provider: optional ``() -> set[str]`` to scope the guard to agent profiles.
      now: optional clock injection (used by tests).

    Returns ``{"enabled", "mode", "boards", "incidents", "applied", "skipped", "errors"}``.
    """
    cfg = load_guard_settings(settings)
    out = {"enabled": cfg["enabled"], "mode": cfg["mode"], "boards": list(cfg["boards"]),
           "incidents": [], "applied": [], "skipped": [], "errors": []}
    if not cfg["enabled"]:
        return out
    profile_names = profile_names_provider() if profile_names_provider else None
    for board in cfg["boards"]:
        try:
            payload = boards_payload_for(board)
        except Exception as exc:  # noqa: BLE001 - one bad board must not kill the tick
            out["errors"].append({"board": board, "stage": "boards_payload", "error": str(exc)[:300]})
            continue
        tasks_iter: list[dict] = []
        for col in (payload.get("columns") or []):
            if not isinstance(col, dict):
                continue
            if (col.get("name") or col.get("id")) in ("done", "archived"):
                continue
            for t in col.get("tasks") or []:
                if isinstance(t, dict):
                    tasks_iter.append(t)
        for ev in find_self_review_tasks(board, tasks_iter,
                                         events_for=lambda tid: events_for(board, tid),
                                         profile_names=profile_names):
            out["incidents"].append(ev)
            if cfg["mode"] == "reassign" and guard_ledger is not None and reassign_fn is not None:
                t = next((x for x in tasks_iter if x.get("id") == ev["task_id"]), None)
                if t is None:
                    continue
                try:
                    res = reassign_self_review(
                        t, events_for(board, ev["task_id"]),
                        board=board, reviewer=cfg["reviewer"], guard_ledger=guard_ledger,
                        reassign_fn=reassign_fn, now=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append({"board": board, "task_id": ev["task_id"],
                                           "stage": "reassign", "error": str(exc)[:300]})
                    continue
                if res.get("status") == "applied":
                    out["applied"].append({"board": board, "task_id": ev["task_id"],
                                            "from": res.get("from"), "to": res.get("to")})
                elif res.get("status") == "skipped":
                    out["skipped"].append({"board": board, "task_id": ev["task_id"],
                                            "reason": res.get("marker", {}).get("reason")})
    return out


__all__ = [
    "DEFAULT_GUARD_BOARDS",
    "DEFAULT_REVIEWER",
    "MARKER_AUDIT_EVENT",
    "GuardLedger",
    "implementer_of",
    "is_self_review",
    "find_self_review_tasks",
    "reassign_self_review",
    "load_guard_settings",
    "tick_review_guard",
]


def _json_default(obj: Any) -> Any:  # pragma: no cover - tiny helper for log calls
    try:
        return json.dumps(obj, sort_keys=True, default=str)
    except TypeError:
        return str(obj)
