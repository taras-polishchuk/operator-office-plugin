"""Approval store (SQLite WAL, single-use CAS).

Shared by the dashboard plugin AND the gateway plugin (Telegram approvals).
No FastAPI imports here so it unit-tests without a dashboard.

Path: ~/.hermes/state/pixel-office/approvals.db (override by env
``PIXEL_OFFICE_APPROVALS_DB`` for tests).

UNIQUE(board, task_id, kind, fingerprint)  -> re-sync never creates duplicates.
Single-use: a decision transition goes through a CAS
``UPDATE ... SET status=? WHERE id=? AND status='pending'`` so two channels
(Office + Telegram) cannot both win.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("pixel-office.approval_store")

# Opaque approval id: 16 chars of urlsafe base64. Used in Telegram callback_data.
_ID_BYTES = 12


def _new_id() -> str:
    return secrets.token_urlsafe(_ID_BYTES)


def action_digest(action: dict) -> str:
    """Canonical SHA-256 of an action dict (sort_keys, compact separators)."""
    return hashlib.sha256(
        json.dumps(action, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ── schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS approvals (
    id               TEXT PRIMARY KEY,
    board            TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    kind             TEXT NOT NULL,
    episode          TEXT,
    fingerprint      TEXT NOT NULL,
    action_json      TEXT NOT NULL,
    action_digest    TEXT NOT NULL,
    summary_json     TEXT NOT NULL,
    created_at       INTEGER NOT NULL,
    expires_at       INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    decided_at       INTEGER,
    decided_by       TEXT,
    decided_via      TEXT,
    apply_state      TEXT NOT NULL DEFAULT 'none',
    apply_detail     TEXT,
    notify_state     TEXT NOT NULL DEFAULT 'pending',
    notify_attempts  INTEGER NOT NULL DEFAULT 0,
    notify_last_error TEXT,
    notify_next_at   INTEGER,
    tg_chat_id       INTEGER,
    tg_message_id    INTEGER,
    tg_synced_status TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_approvals_pending
    ON approvals(board, task_id, kind, fingerprint)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_approvals_board_status
    ON approvals(board, status, created_at DESC);

CREATE TABLE IF NOT EXISTS audit (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    approval_id TEXT NOT NULL,
    event       TEXT NOT NULL,
    actor       TEXT,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS ix_audit_approval ON audit(approval_id, seq);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts DESC);
"""

_KINDS = ("kanban.needs_input", "lane.gate")
_STATUSES = ("pending", "approved", "rejected", "expired", "stale", "superseded")
_NOTIFY_STATES = ("pending", "sent", "failed", "disabled")
_APPLY_STATES = ("none", "applied", "apply_failed")


def _now_int(now: Optional[float]) -> int:
    return int(now if now is not None else time.time())


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # WAL + check_same_thread=False so multiple threads can share the
    # connection; the in-process ``Store._lock`` serializes writes and
    # SQLite's own write-lock gives cross-process safety. busy_timeout keeps
    # short waits on contention from raising immediately.
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _audit(conn: sqlite3.Connection, approval_id: str, event: str, actor: Optional[str], detail: Any) -> None:
    detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail is not None else None
    conn.execute(
        "INSERT INTO audit (ts, approval_id, event, actor, detail_json) VALUES (?, ?, ?, ?, ?)",
        (int(time.time()), approval_id, event, actor, detail_json),
    )


def _audit_safe_id(approval_id: Any) -> str:
    """Last 3 chars of the id for log messages — never the full id."""
    if approval_id is None:
        return "-"
    s = str(approval_id)
    return s[-3:] if len(s) > 3 else (s or "-")


def _audit_public(conn: sqlite3.Connection, approval_id: str, event: str,
                  actor: Optional[str], detail: Any) -> None:
    """Public-safe thin wrapper around the audit insert — keeps the timeline
    intact when callers (e.g. the Telegram gateway) want to log outcome
    events like ``tg_unauthorized`` / ``tg_malformed`` against an id they
    could not resolve to a row yet."""
    try:
        # Allow a None / unknown approval id; audit row stores NULL in that case.
        if not approval_id:
            approval_id = ""
        _audit(conn, approval_id, event, actor, detail)
    except sqlite3.IntegrityError as exc:
        # Defensive: never let audit-write crash the caller.
        logger.warning("audit insert rejected for %s/%s: %s", approval_id, event, exc)


def _load_row(conn: sqlite3.Connection, approval_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return _row_dict(row) if row else None


def _serialize_row(row: Optional[dict]) -> dict:
    """Return a JSON-safe copy of a row (decode JSON columns). Empty dict when None."""
    if not row:
        return {}
    out = dict(row)
    for k in ("action_json", "summary_json"):
        if isinstance(out.get(k), str) and out[k]:
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    return out


# ── Store ─────────────────────────────────────────────────────────────────────


class Store:
    """Approval store. Single instance per process; safe for concurrent threads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = _connect(self.path)
        self._conn.executescript(SCHEMA_SQL)
        # Migration: existing DBs may pre-date the `episode` column.
        cur = self._conn.execute("PRAGMA table_info(approvals)").fetchall()
        cols = {row["name"] for row in cur}
        if "episode" not in cols:
            self._conn.execute("ALTER TABLE approvals ADD COLUMN episode TEXT")
        if "last_block_event_id" not in cols:
            # Tracks the latest task_events.blocked id used to open the
            # current episode; lets rerequest() pick the right episode
            # when fingerprint isn't enough.
            self._conn.execute("ALTER TABLE approvals ADD COLUMN last_block_event_id INTEGER")

    @classmethod
    def open(cls, path: Optional[Path] = None) -> "Store":
        if path is None:
            env = os.environ.get("PIXEL_OFFICE_APPROVALS_DB")
            if env:
                path = Path(env)
            else:
                path = Path.home() / ".hermes" / "state" / "pixel-office" / "approvals.db"
        return cls(path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── upsert ───────────────────────────────────────────────────────────────

    def upsert_request(self, board: str, task_id: str, kind: str, fingerprint: str,
                       action: dict, summary: dict, ttl_s: int = 1800,
                       now: Optional[float] = None,
                       episode: Optional[str] = None) -> tuple[dict, bool]:
        """Create or refresh a pending approval; never resurrects a decided row.

        Episode semantics (F2):
          * ``episode`` binds the request to a specific gating event on the
            task (e.g. the latest ``blocked`` event id for
            ``kanban.needs_input``, or the latest event that put a lane
            gate into ``ready``). It defaults to ``fingerprint`` when the
            caller doesn't have a fresh episode id.
          * A non-superseded row with status ``approved|rejected|expired``
            for the same (board, task_id, kind, episode) is returned as-is
            (created=False) — never resurrected.
          * A pending row with a DIFFERENT fingerprint/digest for the same
            episode is marked ``superseded`` and a new row is created.
          * A stale or superseded last row triggers a fresh row in the
            same episode.
          * A new (non-matching) episode creates a fresh row and marks
            older pending rows of other episodes ``superseded``.

        F1 fix: identical fingerprint+action_digest on an existing pending
        row is a PURE NO-OP. The store never refreshes created_at,
        expires_at, status, decided_*, notify_*, or tg_* — so the TTL
        keeps elapsing and the Telegram loop cannot re-send the same
        approval every tick.
        """
        if kind not in _KINDS:
            raise ValueError(f"unsupported approval kind: {kind!r}")
        digest = action_digest(action)
        now_i = _now_int(now)
        expires = now_i + int(ttl_s)
        action_json = json.dumps(action, ensure_ascii=False, sort_keys=True)
        summary_json = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        ep = episode or fingerprint
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # 1) Already-decided match for the same episode -> return it,
                #    never resurrect.
                decided = self._conn.execute(
                    "SELECT id FROM approvals "
                    "WHERE board = ? AND task_id = ? AND kind = ? AND episode = ? "
                    "AND status IN ('approved','rejected','expired','stale') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (board, task_id, kind, ep),
                ).fetchone()
                if decided:
                    self._conn.execute("COMMIT")
                    row = _load_row(self._conn, decided["id"])
                    return _serialize_row(row), False

                # 2) Exact (board, task_id, kind, fingerprint, action_digest) row
                #    that is still pending -> pure no-op (F1 fix).
                cur = self._conn.execute(
                    "SELECT id, status FROM approvals "
                    "WHERE board = ? AND task_id = ? AND kind = ? "
                    "AND fingerprint = ? AND action_digest = ?",
                    (board, task_id, kind, fingerprint, digest),
                ).fetchone()
                if cur and cur["status"] == "pending":
                    self._conn.execute("COMMIT")
                    row = _load_row(self._conn, cur["id"])
                    return _serialize_row(row), False

                # 3) Pending row in the same episode with a DIFFERENT
                #    fingerprint/digest -> mark superseded.
                superseded = False
                same_episode_pending = self._conn.execute(
                    "SELECT id, fingerprint, action_digest FROM approvals "
                    "WHERE board = ? AND task_id = ? AND kind = ? "
                    "AND episode = ? AND status = 'pending'",
                    (board, task_id, kind, ep),
                ).fetchall()
                for p in same_episode_pending:
                    self._conn.execute(
                        "UPDATE approvals SET status = 'superseded' WHERE id = ?",
                        (p["id"],),
                    )
                    _audit(self._conn, p["id"], "superseded", "store.upsert",
                           {"reason": "episode_changed_or_action_changed",
                            "old_fingerprint": p["fingerprint"], "new_fingerprint": fingerprint})
                    superseded = True

                # 4) Pending rows from a DIFFERENT episode (older gate
                #    episodes still open) -> also superseded.
                other_episode_pending = self._conn.execute(
                    "SELECT id FROM approvals "
                    "WHERE board = ? AND task_id = ? AND kind = ? "
                    "AND episode != ? AND status = 'pending'",
                    (board, task_id, kind, ep),
                ).fetchall()
                for p in other_episode_pending:
                    self._conn.execute(
                        "UPDATE approvals SET status = 'superseded' WHERE id = ?",
                        (p["id"],),
                    )
                    _audit(self._conn, p["id"], "superseded", "store.upsert",
                           {"reason": "new_episode",
                            "old_episode": None, "new_episode": ep})
                    superseded = True

                # 5) Decide whether we can REUSE an existing same-fingerprint
                #    decided row in the same episode (e.g. an expired row the
                #    operator explicitly wants re-issued) — only when that row
                #    has the same fingerprint+action_digest AND nothing newer
                #    exists for the episode. If a row exists and is decided
                #    with matching binding, we still insert a NEW row so the
                #    audit chain is preserved. (rerequest() is the explicit
                #    path for that.)

                # 6) Fresh insert.
                approval_id = _new_id()
                self._conn.execute(
                    "INSERT INTO approvals (id, board, task_id, kind, episode, fingerprint, "
                    "  action_json, action_digest, summary_json, created_at, expires_at, "
                    "  status, apply_state, notify_state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'none', 'pending')",
                    (approval_id, board, task_id, kind, ep, fingerprint, action_json,
                     digest, summary_json, now_i, expires),
                )
                _audit(self._conn, approval_id, "created", "store.upsert",
                       {"kind": kind, "ttl_s": ttl_s,
                        "superseded_previous": superseded, "episode": ep})
                self._conn.execute("COMMIT")
                row = _load_row(self._conn, approval_id)
                return _serialize_row(row), True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── re-request (F3) ──────────────────────────────────────────────────────

    def rerequest(self, board: str, task_id: str, kind: str, *,
                  fingerprint: Optional[str] = None,
                  action: Optional[dict] = None,
                  summary: Optional[dict] = None,
                  ttl_s: int = 1800,
                  now: Optional[float] = None,
                  actor: str = "operator",
                  episode: Optional[str] = None) -> Optional[dict]:
        """Create a NEW pending approval for an expired episode (F3).

        Allowed only when the gate is still open (we still hold a non-empty
        episode id, and the latest row for that episode is ``expired`` /
        ``rejected`` / ``stale`` / ``superseded`` / absent — i.e. nothing
        currently pending for that episode). When a pending row already
        exists for the episode, ``None`` is returned (caller must wait or
        decide the live row).

        The new row's action+summary defaults to the latest non-superseded
        row's binding when the caller doesn't supply them, so an ``expired``
        gate re-opens with the same action the worker originally requested.
        """
        ep = episode or fingerprint
        if ep is None:
            # Re-requesting without an episode id is meaningless: we cannot
            # tell which expired episode the operator wants re-issued.
            return None
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                # Refuse when the episode still has a pending row.
                live = self._conn.execute(
                    "SELECT id FROM approvals "
                    "WHERE board = ? AND task_id = ? AND kind = ? AND episode = ? "
                    "AND status = 'pending' LIMIT 1",
                    (board, task_id, kind, ep),
                ).fetchone()
                if live:
                    self._conn.execute("COMMIT")
                    return None
                # The (board, task_id, kind, fingerprint) tuple is uniquely
                # indexed while pending; supersede any stale pending rows
                # of OTHER episodes before inserting the new one.
                other_pending = self._conn.execute(
                    "SELECT id FROM approvals "
                    "WHERE board = ? AND task_id = ? AND kind = ? "
                    "AND status = 'pending'",
                    (board, task_id, kind),
                ).fetchall()
                for p in other_pending:
                    self._conn.execute(
                        "UPDATE approvals SET status = 'superseded' WHERE id = ?",
                        (p["id"],),
                    )
                    _audit(self._conn, p["id"], "superseded", "store.rerequest",
                           {"reason": "rerequest_other_episode",
                            "new_episode": ep})
                # Look up the binding from the latest row of the episode so
                # the re-request stays faithful to the original action.
                latest = self._conn.execute(
                    "SELECT fingerprint, action_json, action_digest, summary_json "
                    "FROM approvals WHERE board = ? AND task_id = ? AND kind = ? "
                    "AND episode = ? ORDER BY created_at DESC LIMIT 1",
                    (board, task_id, kind, ep),
                ).fetchone()
                if latest is None:
                    # No history for that episode -> we cannot recreate the binding.
                    self._conn.execute("COMMIT")
                    return None
                fp_used = fingerprint or latest["fingerprint"]
                # Action digest must match the original binding unless the
                # caller explicitly supplies a new one (e.g. re-requesting
                # after the worker fixed the action payload).
                if action is None:
                    try:
                        action_dict = json.loads(latest["action_json"])
                    except ValueError:
                        action_dict = {"type": kind, "board": board, "task_id": task_id}
                else:
                    action_dict = action
                if summary is None:
                    try:
                        summary_dict = json.loads(latest["summary_json"])
                    except ValueError:
                        summary_dict = {}
                else:
                    summary_dict = summary
                now_i = _now_int(now)
                expires = now_i + int(ttl_s)
                action_json = json.dumps(action_dict, ensure_ascii=False, sort_keys=True)
                summary_json = json.dumps(summary_dict, ensure_ascii=False, sort_keys=True)
                digest = action_digest(action_dict)
                approval_id = _new_id()
                self._conn.execute(
                    "INSERT INTO approvals (id, board, task_id, kind, episode, fingerprint, "
                    "  action_json, action_digest, summary_json, created_at, expires_at, "
                    "  status, apply_state, notify_state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'none', 'pending')",
                    (approval_id, board, task_id, kind, ep, fp_used, action_json,
                     digest, summary_json, now_i, expires),
                )
                _audit(self._conn, approval_id, "rerequested", actor,
                       {"episode": ep, "ttl_s": ttl_s,
                        "reopens_fingerprint": fp_used})
                self._conn.execute("COMMIT")
                row = _load_row(self._conn, approval_id)
                return _serialize_row(row)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── decide (single-use CAS) ──────────────────────────────────────────────

    def decide(self, approval_id: str, decision: str, actor: str, via: str,
               now: Optional[float] = None, verify: Optional[Callable[[dict], tuple[str, Optional[str]]]] = None
               ) -> "DecisionResult":
        """CAS-transition a pending row to 'approved' or 'rejected'.

        ``verify(row)`` must return ``('ok'|'stale', reason)`` and is called
        AFTER fingerprint/action digest match but BEFORE the status CAS.

        Outcomes (DecisionResult.outcome):
            - 'unknown'            -> no such row
            - 'already_decided'    -> status != pending (details: who/when/via)
            - 'expired'            -> now >= expires_at (CAS to 'expired')
            - 'stale'              -> verify() returned stale (CAS to 'stale')
            - 'decided'            -> CAS winner; caller MUST apply
            - 'cas_lost'           -> CAS lost (someone else won)
        """
        if decision not in ("approve", "reject"):
            raise ValueError(f"decision must be approve|reject, got {decision!r}")
        new_status = "approved" if decision == "approve" else "rejected"
        now_i = _now_int(now)
        with self._lock:
            row = _load_row(self._conn, approval_id)
            if not row:
                return DecisionResult("unknown", None, "no such approval")
            row = row
            if row["status"] != "pending":
                return DecisionResult("already_decided", _serialize_row(row),
                                      f"status={row['status']} by {row['decided_by']} via {row['decided_via']} at {row['decided_at']}")
            if now_i >= row["expires_at"]:
                # CAS to expired (only the winner returns this outcome; losers get already_decided)
                upd = self._conn.execute(
                    "UPDATE approvals SET status = 'expired', decided_at = ?, decided_by = ?, decided_via = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (now_i, actor, via, approval_id),
                )
                if upd.rowcount:
                    _audit(self._conn, approval_id, "expired", actor, {"via": via})
                    row = _load_row(self._conn, approval_id) or {}
                    return DecisionResult("expired", _serialize_row(row), "ttl elapsed")
                row = _load_row(self._conn, approval_id) or {}
                return DecisionResult("already_decided", _serialize_row(row),
                                      f"status={row['status']}")
            if verify is not None:
                verdict, reason = verify(row)
                if verdict != "ok":
                    if verdict == "stale":
                        upd = self._conn.execute(
                            "UPDATE approvals SET status = 'stale', decided_at = ?, decided_by = ?, decided_via = ? "
                            "WHERE id = ? AND status = 'pending'",
                            (now_i, actor, via, approval_id),
                        )
                        if upd.rowcount:
                            _audit(self._conn, approval_id, "stale", actor,
                                   {"reason": reason, "via": via})
                            row = _load_row(self._conn, approval_id) or {}
                            return DecisionResult("stale", _serialize_row(row), reason or "stale")
                        row = _load_row(self._conn, approval_id) or {}
                        return DecisionResult("already_decided", _serialize_row(row),
                                              f"status={row['status']}")
                    return DecisionResult("verify_failed", _serialize_row(row), reason or verdict)
            upd = self._conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, decided_via = ? "
                "WHERE id = ? AND status = 'pending'",
                (new_status, now_i, actor, via, approval_id),
            )
            if not upd.rowcount:
                row = _load_row(self._conn, approval_id) or {}
                return DecisionResult("cas_lost", _serialize_row(row),
                                      f"status={row['status']}")
            _audit(self._conn, approval_id, new_status, actor,
                   {"via": via, "decision": decision})
            row = _load_row(self._conn, approval_id) or {}
            return DecisionResult("decided", _serialize_row(row), None)

    # ── apply bookkeeping ────────────────────────────────────────────────────

    def record_apply(self, approval_id: str, ok: bool, detail: Any = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET apply_state = ?, apply_detail = ? WHERE id = ?",
                ("applied" if ok else "apply_failed",
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                 approval_id),
            )
            _audit(self._conn, approval_id, "apply_ok" if ok else "apply_failed",
                   "store.record_apply", {"detail": detail})

    def audit(self, approval_id: str, event: str,
              actor: Optional[str] = None, detail: Any = None) -> None:
        """Public audit-write entry point for callers that need to log events
        which never mutate the approval row itself (e.g. Telegram gateway
        callback outcomes for unauthorized / malformed / unknown ids).

        ``approval_id`` may be empty / None (audit row stores NULL). The
        write goes through the same connection, so it serializes against any
        other store mutation and never deadlocks the gateway.

        Never raises — broken sqlite logs a warning and returns.
        """
        try:
            with self._lock:
                _audit_public(self._conn, approval_id or "", event, actor, detail)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Store.audit(%s,%s) failed: %s",
                           _audit_safe_id(approval_id), event, exc)

    # ── notification bookkeeping ─────────────────────────────────────────────

    def pending_notifications(self, now: Optional[float] = None) -> list[dict]:
        now_i = _now_int(now)
        rows = self._conn.execute(
            "SELECT * FROM approvals "
            "WHERE status = 'pending' AND notify_state = 'pending' "
            "AND (notify_next_at IS NULL OR notify_next_at <= ?) "
            "ORDER BY created_at ASC LIMIT 200",
            (now_i,),
        ).fetchall()
        return [_serialize_row(_row_dict(r)) for r in rows]

    def mark_notified(self, approval_id: str, chat_id: int, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET notify_state = 'sent', notify_attempts = notify_attempts + 1, "
                "tg_chat_id = ?, tg_message_id = ?, notify_last_error = NULL, "
                "notify_next_at = NULL WHERE id = ?",
                (int(chat_id), int(message_id), approval_id),
            )

    def mark_notify_failed(self, approval_id: str, err: str, now: Optional[float] = None) -> None:
        """Keep the gate pending. Exponential backoff 30s..15m."""
        now_i = _now_int(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT notify_attempts FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            attempts = int(row["notify_attempts"]) + 1 if row else 1
            delay = min(900, 30 * (2 ** min(attempts - 1, 8)))
            self._conn.execute(
                "UPDATE approvals SET notify_state = 'pending', notify_attempts = ?, "
                "notify_last_error = ?, notify_next_at = ? WHERE id = ?",
                (attempts, (err or "")[:500], now_i + delay, approval_id),
            )

    # ── TTL sweep ────────────────────────────────────────────────────────────

    def expire_due(self, now: Optional[float] = None) -> int:
        now_i = _now_int(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM approvals WHERE status = 'pending' AND expires_at <= ?",
                (now_i,),
            ).fetchall()
            if not rows:
                return 0
            ids = [r["id"] for r in rows]
            self._conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE id IN ({})".format(
                    ",".join("?" * len(ids))),
                ids,
            )
            for i in ids:
                _audit(self._conn, i, "expired", "store.expire_due", {"ttl": True})
            return len(ids)

    # ── queue ────────────────────────────────────────────────────────────────

    def queue(self, board: Optional[str] = None, limit: int = 100) -> dict:
        """Return {'pending': [...], 'decided': [...]} (decided = last 50)."""
        params: list[Any] = []
        where = "WHERE status = 'pending'"
        if board:
            where += " AND board = ?"
            params.append(board)
        pend = self._conn.execute(
            f"SELECT * FROM approvals {where} ORDER BY created_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
        decided_where = "WHERE status != 'pending'"
        decided_params: list[Any] = []
        if board:
            decided_where += " AND board = ?"
            decided_params.append(board)
        dec = self._conn.execute(
            f"SELECT * FROM approvals {decided_where} ORDER BY decided_at DESC LIMIT 50",
            decided_params,
        ).fetchall()
        return {
            "pending": [_serialize_row(_row_dict(r)) for r in pend],
            "decided": [_serialize_row(_row_dict(r)) for r in dec],
        }

    # ── telegram sync ────────────────────────────────────────────────────────

    def needs_tg_sync(self, now: Optional[float] = None) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM approvals "
            "WHERE status != 'pending' AND (tg_message_id IS NOT NULL) "
            "AND (tg_synced_status IS NULL OR tg_synced_status != status) "
            "ORDER BY decided_at DESC LIMIT 100"
        ).fetchall()
        return [_serialize_row(_row_dict(r)) for r in rows]

    def mark_tg_synced(self, approval_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET tg_synced_status = ? WHERE id = ?",
                (status, approval_id),
            )


# ── DecisionResult ────────────────────────────────────────────────────────────


class DecisionResult:
    __slots__ = ("outcome", "row", "detail")

    def __init__(self, outcome: str, row: Optional[dict], detail: Optional[str]) -> None:
        self.outcome = outcome
        self.row = row
        self.detail = detail

    def __repr__(self) -> str:
        return f"DecisionResult(outcome={self.outcome!r}, detail={self.detail!r})"


def open_store(path: Optional[Path] = None) -> Store:
    """Module-level helper matching the contract."""
    return Store.open(path)