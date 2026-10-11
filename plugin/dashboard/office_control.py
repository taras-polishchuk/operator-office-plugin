"""Operator Agent Office: control-plane projection over Hermes.

Pure functions + small stores. No FastAPI imports here, so it unit-tests
without a dashboard. plugin_api.py wires these into HTTP routes.

Authority (never duplicated here):
  * profiles, tasks, runs, events, comments, attachments -> Hermes (REST)
  * session activity                                     -> <profile>/state.db (read-only)
  * missions                                             -> /home/taras/projects/.project-state (read-only)
Plugin-owned (presentation + audit only):
  * rooms.json        room definitions / routing rules (overrides defaults)
  * office-audit.jsonl  append-only log of operator decisions
  * decisions.json    idempotency ledger for decisions

Contracts (JSON shapes) are documented in docs/operator-office/CONTRACTS.md.
Every object keeps the raw Hermes id + raw status next to the normalized one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import lanes  # noqa: E402  (same directory; plugin_api puts it on sys.path)

CONTRACT_VERSION = "office.v2"

# ── validation ───────────────────────────────────────────────────────────────

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{1,63}$")
BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,120}$")
MISSION_ARTIFACTS = (
    "source-task.md", "progress.md", "decisions.md", "blockers.md",
    "artifacts.md", "environment.md", "execution-log.md", "final-report.md",
)
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".py", ".ts", ".tsx", ".js", ".log", ".csv", ".html", ".sh", ".toml"}
MAX_TEXT_BYTES = 64 * 1024


def valid_task_id(v: str) -> bool:
    return bool(v) and bool(TASK_ID_RE.match(v))


def valid_board(v: str) -> bool:
    return bool(v) and bool(BOARD_RE.match(v))


# ── secret redaction (frontend never receives these) ─────────────────────────

_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWT
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)((?:api[_-]?key|secret|password|passwd|token|session[_-]?token)\s*[=:]\s*[\"']?)[^\s\"'&]{6,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def redact(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


# ── rooms ────────────────────────────────────────────────────────────────────
# Visual + domain boundaries. NOT processes, NOT security boundaries.
# Routing order per task: client marker -> tenant prefix -> title keyword -> assignee residency -> command.

DEFAULT_ROOMS: list[dict] = [
    # Nine functional rooms (keys 1-9) + Client Work (locked control, never a task view).
    # Residents = real Hermes profiles (verified 2026-10-09: 9 profiles; `career` appeared 21:51Z, created outside the office). Product Lab has no own profile:
    # product work routes there by tenant/keyword and is executed by Development/QA profiles.
    {"id": "command", "name": "Mission Control", "glyph": "◆", "color": "#f5a524", "sensitivity": "personal",
     "purpose": "Orchestrator, System Base, global mission status, routing.",
     "residents": ["orchestrator", "default"], "tenant_prefixes": ["mission-", "orchestrator-"],
     "keywords": ["[mission root]", "orchestrat", "routing"]},
    {"id": "product", "name": "Product Lab", "glyph": "⚙", "color": "#4f8cff", "sensitivity": "personal", "broad": True,
     "purpose": "Personal products and experiments. No dedicated profile: executed by Development + QA.",
     "residents": [], "tenant_prefixes": ["video-factory", "staf-", "wave9", "wave-9", "infra-platform", "product-"],
     "keywords": ["product", "mvp", "landing", "prototype"]},
    {"id": "research", "name": "Research", "glyph": "✦", "color": "#9353d3", "sensitivity": "personal",
     "purpose": "Investigations, evaluations, knowledge capture (Knowledge OS stays the authority).",
     "residents": ["research"], "tenant_prefixes": ["research-", "ae-know", "knowledge-", "kos-"],
     "keywords": ["research", "investigat", "evaluate", "[knowledge]", "kgctl", "knowledge os"]},
    {"id": "dev", "name": "Development", "glyph": "⌘", "color": "#38bdf8", "sensitivity": "personal", "broad": True,
     "purpose": "Implementation on local branches, tests. No push without the operator.",
     "residents": ["developer"], "tenant_prefixes": ["dev-", "lane-dev-"],
     "keywords": ["implement", "scaffold", "build", "fix", "refactor", "repo", "deploy"]},
    {"id": "qa", "name": "QA", "glyph": "✓", "color": "#17c964", "sensitivity": "personal", "broad": True,
     "purpose": "Independent review and verification. Default reviewer for governed lanes.",
     "residents": ["qa"], "tenant_prefixes": ["qa-"],
     "keywords": ["qa", "test", "verify", "audit", "review"]},
    {"id": "content", "name": "Content Studio", "glyph": "✎", "color": "#f31260", "sensitivity": "personal",
     "purpose": "X content research, drafting, QA, approval. Publishing adapter disabled.",
     "residents": ["content"], "tenant_prefixes": ["content-", "nova-", "lane-content-"],
     "keywords": ["content", "post", "thread", "tweet", "x.com"]},
    {"id": "comms", "name": "Comms & Career", "glyph": "✉", "color": "#06b6d4", "sensitivity": "personal",
     "purpose": "Inbox triage, reply drafts, vacancies, applications. Send/submit adapters disabled.",
     "residents": ["email", "career"], "tenant_prefixes": ["email-", "inbox-", "career-", "lane-email-", "lane-career-"],
     "keywords": ["email", "inbox", "reply", "recruiter", "career", "vacancy", "cv ", "djinni", "dou", "application", "interview"]},
    {"id": "ops", "name": "Operations", "glyph": "⚑", "color": "#ff7a45", "sensitivity": "personal",
     "purpose": "Infrastructure, gateway, cron, incidents. Change adapter disabled.",
     "residents": ["ops"], "tenant_prefixes": ["ops-", "hermes-agent", "lane-ops-"],
     "keywords": ["[operator]", "incident", "n8n", "cron", "gateway", "backup", "tailscale", "systemd"]},
    {"id": "finance", "name": "Finance", "glyph": "¤", "color": "#a3e635", "sensitivity": "personal",
     "purpose": "Read-only analysis and proposals. Money never moves from the office.",
     "residents": ["finance"], "tenant_prefixes": ["finance-", "lane-finance-"],
     "keywords": ["invoice", "finance", "budget", "stripe", "payment", "billing", "cost"]},
    {"id": "client", "name": "Client Work", "glyph": "▣", "color": "#8b93a7", "sensitivity": "client",
     "purpose": "Locked. Needs a dedicated board, profile, HERMES_HOME and credentials. Nothing is shown here.",
     "residents": [], "tenant_prefixes": [], "keywords": []},
]

# Corporate / client markers. A task matching one is withheld from every
# personal room (only counted). Projection filter only, not isolation.
DEFAULT_CLIENT_MARKERS = ["insidedynamic", "idstar", "founderos", "founder-os", "fos-neu"]

# Client boards (settings `clientBoards`). Every task on these boards is
# treated as confidential client work; on personal boards the existing
# is_client 403 lock stays exactly as today.
DEFAULT_CLIENT_BOARDS: tuple[str, ...] = ("client-work",)


def load_rooms(override: Optional[dict]) -> tuple[list[dict], list[str]]:
    rooms = [dict(r) for r in DEFAULT_ROOMS]
    markers = list(DEFAULT_CLIENT_MARKERS)
    if isinstance(override, dict):
        by_id = {r["id"]: r for r in rooms}
        for patch in override.get("rooms", []) or []:
            if isinstance(patch, dict) and patch.get("id") in by_id:
                for k in ("name", "purpose", "residents", "tenant_prefixes", "keywords", "color", "glyph"):
                    if k in patch:
                        by_id[patch["id"]][k] = patch[k]
        if isinstance(override.get("client_markers"), list):
            markers = [str(m).lower() for m in override["client_markers"] if str(m).strip()]
    return rooms, markers


def is_client(task: dict, markers: Iterable[str]) -> bool:
    hay = " ".join(str(task.get(k) or "") for k in ("tenant", "title", "body", "workspace_path")).lower()
    return any(m and m in hay for m in markers)


def _coerce_board_list(raw: Any, default: tuple[str, ...]) -> list[str]:
    """Read a `clientBoards`-style setting: list of valid board slugs."""
    if isinstance(raw, list):
        out = [str(x) for x in raw if isinstance(x, str) and str(x).strip() and BOARD_RE.match(str(x))]
        return out
    return list(default)


def client_boards(settings: dict) -> list[str]:
    """Boards flagged as client boards. D9: every task there is client by definition."""
    return _coerce_board_list((settings or {}).get("clientBoards"), DEFAULT_CLIENT_BOARDS)


def is_client_board(board: str, settings: dict) -> bool:
    """True when the requested Kanban board is a client board. Case-sensitive."""
    return bool(board) and board in set(client_boards(settings))


def client_audit_record(action: str, board: str, task_id: Optional[str]) -> dict:
    """Client boards: record ids + board + action only. No titles, no bodies."""
    return {"action": action, "board": board, "task_id": task_id, "client": True}


def route_task(task: dict, rooms: list[dict], markers: Iterable[str]) -> tuple[str, str]:
    """Return (room_id, reason). Deterministic; first rule wins:
    client marker -> tenant prefix -> specific keywords -> assignee residency -> broad keywords -> command."""
    if is_client(task, markers):
        return "client", "client-marker"
    tenant = (task.get("tenant") or "").lower()
    title = (task.get("title") or "").lower()
    personal = [r for r in rooms if r["sensitivity"] != "client"]
    if tenant:
        for r in personal:
            if any(tenant.startswith(p) for p in r.get("tenant_prefixes", [])):
                return r["id"], f"tenant:{tenant}"
    for r in personal:
        if not r.get("broad") and any(k in title for k in r.get("keywords", [])):
            return r["id"], "keyword"
    assignee = task.get("assignee") or ""
    for r in personal:
        if assignee and assignee in r.get("residents", []):
            return r["id"], f"assignee:{assignee}"
    for r in personal:
        if r.get("broad") and any(k in title for k in r.get("keywords", [])):
            return r["id"], "keyword"
    return "command", "unrouted"


# ── states ───────────────────────────────────────────────────────────────────

AGENT_STATES = ("idle", "working", "blocked", "awaiting_review", "completed", "offline", "unknown")
TASK_STATES = {
    "triage": "queued", "todo": "queued", "scheduled": "queued", "ready": "queued",
    "running": "working", "blocked": "blocked", "review": "awaiting_review",
    "done": "completed", "archived": "completed", "cancelled": "completed",
}
OPEN_STATUSES = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")
COMPLETED_WINDOW_S = 600


def task_ref(t: dict, board: str, room_id: str, route_reason: str) -> dict:
    status = t.get("status") or "unknown"
    return {
        "kind": "TaskReference",
        "id": t.get("id"),
        "board": board,
        "title": redact(t.get("title") or ""),
        "status": status,                          # raw Hermes status
        "state": TASK_STATES.get(status, "unknown"),  # normalized
        "assignee": t.get("assignee"),
        "tenant": t.get("tenant"),
        "priority": t.get("priority") or 0,
        "room": room_id,
        "route_reason": route_reason,
        "created_at": t.get("created_at"),
        "started_at": t.get("started_at"),
        "completed_at": t.get("completed_at"),
        "current_run_id": t.get("current_run_id"),
        "latest_summary": redact((t.get("latest_summary") or "")[:400]) or None,
        "has_diagnostics": bool(t.get("diagnostics")),
    }


def agent_presence(profile: dict, act: dict, workers: list[dict], tasks: list[dict], now: float) -> dict:
    """Derive one normalized state from real sources. Precedence documented in CONTRACTS.md."""
    name = profile["name"]
    mine = [t for t in tasks if t.get("assignee") == name]
    in_review = [t["id"] for t in mine if t.get("status") == "review"]
    running = [w for w in workers if (w.get("profile") or w.get("task_assignee")) == name]
    evidence: list[str] = []
    if act is None or str(act.get("activity") or "").startswith("error:"):
        state = "unknown"
        evidence.append("activity store unreadable")
        act = act or {}
    elif profile.get("gateway_running") is False:
        state = "offline"
        evidence.append("gateway_running=false")
    elif act.get("status") == "active" or running:
        state = "working"
        if act.get("status") == "active":
            evidence.append("session: tool/turn in progress")
        if running:
            evidence.append(f"kanban worker: {running[0].get('task_id')}"
                            + (f" (board {running[0]['board']})" if running[0].get("board") else ""))
    elif act.get("permission") or act.get("awaiting_input"):
        state = "blocked"
        evidence.append("session: waiting for operator input")
    elif in_review:
        state = "awaiting_review"
        evidence.append(f"kanban review: {', '.join(in_review[:3])}")
    elif act.get("status") == "waiting" and act.get("last_at") and now - act["last_at"] < COMPLETED_WINDOW_S:
        state = "completed"
        evidence.append("session: turn finished")
    else:
        state = "idle"
    return {
        "kind": "AgentPresence",
        "profile": name,
        "state": state,
        "evidence": evidence,
        "activity": redact(act.get("activity") if act else None),
        "last_at": (act or {}).get("last_at") or None,
        # Truthfulness: only a live Kanban worker is confirmed execution. Session activity (CLI/gateway chat)
        # is real but not Kanban work; the UI labels it "session (inferred)".
        "execution": "kanban_worker" if running else ("session_activity" if (act or {}).get("status") == "active" else None),
        "running_tasks": [w.get("task_id") for w in running],
        "review_tasks": in_review,
        "open_tasks": len([t for t in mine if t.get("status") in OPEN_STATUSES]),
    }


def agent_profile(p: dict, room_of: dict[str, str]) -> dict:
    return {
        "kind": "AgentProfile",
        "profile": p["name"],
        "name": p.get("display_name") or p["name"],
        "description": p.get("description") or "",
        "model": p.get("model"),
        "provider": p.get("provider"),
        "is_default": bool(p.get("is_default")),
        "room": room_of.get(p["name"]),
        "on_disk": True,
    }


# ── events ───────────────────────────────────────────────────────────────────

EVENT_SEVERITY = {
    "crashed": "error", "failed": "error", "timed_out": "error", "gave_up": "error",
    "blocked": "warn", "review_requested": "info", "completed": "ok",
}


def office_event(e: dict, board: str) -> dict:
    """Kanban task_event -> OfficeEvent. id is stable: kanban:<board>:<event id>."""
    kind = e.get("kind") or "unknown"
    payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    safe_payload = {k: (redact(v) if isinstance(v, str) else v) for k, v in payload.items()
                    if k != "tenant" and (k not in ("body", "result", "summary") or isinstance(v, (int, float, bool)))}
    return {
        "kind": "OfficeEvent",
        "id": f"kanban:{board}:{e.get('id')}",
        "seq": e.get("id"),
        "source": "hermes.kanban",
        "board": board,
        "type": kind,
        "severity": EVENT_SEVERITY.get(kind, "info"),
        "ts": e.get("created_at"),
        "correlation": {"task_id": e.get("task_id"), "run_id": e.get("run_id")},
        "payload": safe_payload,
    }


# ── approvals ────────────────────────────────────────────────────────────────
# Hermes-native human gates (verified in hermes_cli/kanban_db.py + tools/kanban_tools.py):
#   * kanban.needs_input   : status=blocked, block_kind=needs_input ("you need a human decision")
#   * kanban.review.human  : status=review, assignee is NOT an installed profile (human lane, e.g. taras)
# A review assigned to a profile is AGENT review (the dispatcher claims it), not an operator approval.
# Binding = HMAC over (request id, decision, target, board, task fingerprint, expiry).
# Secret lives in memory: tokens die with the dashboard process (UI refetches).

_APPROVAL_SECRET = secrets.token_bytes(32)
APPROVAL_TTL_S = 900
APPROVAL_KINDS = {
    "kanban.needs_input": ("approve", "reject"),
    "kanban.review.human": ("approve", "request_changes"),
    "lane.gate": ("approve", "reject"),
}
APPROVAL_ACTIONS = ("approve", "reject", "request_changes")


def _sign(fields: list[str]) -> str:
    msg = "\x1f".join(fields).encode("utf-8")
    return hmac.new(_APPROVAL_SECRET, msg, hashlib.sha256).hexdigest()


def task_fingerprint(task: dict, events: list[dict]) -> str:
    last = max((int(e.get("id") or 0) for e in events), default=0)
    return f"{task.get('status')}:{task.get('current_run_id') or '-'}:{last}"


def approval_kind(task: dict, profile_names: set[str]) -> Optional[str]:
    status = task.get("status")
    lm = lanes.parse_marker(task.get("title") or "")
    if lm and lm[0] in lanes.TEMPLATES and lm[2] == lanes.TEMPLATES[lm[0]]["steps"][-1]["key"]:
        # Lane operator gate: human lane, ready once every parent step is done. Nothing else is a lane gate.
        if status == "ready" and (task.get("assignee") or "") not in profile_names:
            return "lane.gate"
        return None
    if status == "blocked" and task.get("block_kind") == "needs_input":
        return "kanban.needs_input"
    if status == "review" and (task.get("assignee") or "") not in profile_names:
        return "kanban.review.human"
    return None


def parse_request_id(rid: str) -> Optional[tuple[str, str, str]]:
    parts = (rid or "").split(":")
    if len(parts) != 3 or parts[0] not in APPROVAL_KINDS:
        return None
    return parts[0], parts[1], parts[2]


def approval_request(task: dict, board: str, fingerprint: str, now: float, kind: str) -> dict:
    exp = int(now + APPROVAL_TTL_S)
    rid = f"{kind}:{board}:{task['id']}"
    decisions = APPROVAL_KINDS[kind]
    tokens = {a: _sign([rid, a, task["id"], board, fingerprint, str(exp)]) for a in decisions}
    effects = {
        "kanban.needs_input": {"approve": "comment 'APPROVED: <note>' + unblock (worker resumes)",
                               "reject": "comment 'REJECTED: <note>' + unblock (worker resumes, must not proceed)"},
        "kanban.review.human": {"approve": "Kanban PATCH status=done",
                                "request_changes": "hermes kanban reopen-review --reason <note> (back to the queue)"},
        "lane.gate": {"approve": "server re-checks independent review (PASS, reviewer != author) -> execution adapter "
                                 "(disabled adapters record not_executed) -> lane record attached -> Kanban done",
                      "reject": "Kanban blocked with your note; nothing is executed"},
    }[kind]
    return {
        "kind": "ApprovalRequest",
        "id": rid,
        "source": "hermes." + kind,
        "action": kind + ".decide",
        "allowed_decisions": list(decisions),
        "effects": effects,
        "target": {"board": board, "task_id": task["id"], "assignee": task.get("assignee")},
        "params": {"expected_status": task.get("status"), "fingerprint": fingerprint},
        "title": redact(task.get("title") or ""),
        "summary": redact((task.get("latest_summary") or "")[:600]) or None,
        "issued_at": int(now),
        "expires_at": exp,
        "tokens": tokens,
    }


def verify_approval(token: str, rid: str, decision: str, task_id: str, board: str,
                    fingerprint: str, exp: int, now: float) -> Optional[str]:
    """None if valid, else a refusal reason."""
    parsed = parse_request_id(rid)
    if not parsed or decision not in APPROVAL_KINDS[parsed[0]]:
        return "unknown decision"
    if now > exp:
        return "approval expired; refresh and review again"
    good = _sign([rid, decision, task_id, board, fingerprint, str(exp)])
    if not hmac.compare_digest(good, token or ""):
        return "approval token does not match this exact action/target/state"
    return None


# ── incidents ────────────────────────────────────────────────────────────────

def incidents_from(board_payload: dict, diagnostics: list[dict], board: str, client_ids: set[str],
                   health: dict[str, Any], human_assignees: Optional[set[str]] = None) -> list[dict]:
    human_assignees = human_assignees or set()
    out: list[dict] = []
    for comp, ok in health.items():
        if ok is False:
            out.append({"kind": "Incident", "id": f"health:{comp}", "source": "office.health", "severity": "critical",
                        "title": f"{comp} unreachable", "task_id": None, "board": board, "detail": None})
    for d in diagnostics or []:
        tid = d.get("task_id")
        if tid in client_ids:
            continue
        for g in d.get("diagnostics", []) or []:
            if g.get("kind") == "stranded_in_ready" and d.get("task_assignee") in human_assignees:
                continue  # human lane: operator queue, not an incident
            out.append({"kind": "Incident", "id": f"diag:{board}:{tid}:{g.get('kind')}", "source": "hermes.kanban.diagnostics",
                        "severity": g.get("severity") or "warning", "title": redact(g.get("title") or g.get("kind") or ""),
                        "task_id": tid, "task_title": redact(d.get("task_title") or ""), "board": board,
                        "detail": redact((g.get("detail") or "")[:500])})
    sev = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    out.sort(key=lambda i: sev.get(i["severity"], 9))
    return out


# ── missions (Workspace OS .project-state, read-only) ────────────────────────

_STATUS_RE = re.compile(r"\[STATUS:\s*([^\]]+)\]|^\s*\**status\**\s*:\s*\**\s*([A-Za-z][A-Za-z _\-]{1,40})", re.I | re.M)


def mission_ref(d: Path) -> dict:
    present = [a for a in MISSION_ARTIFACTS if (d / a).is_file()]
    status = "unknown"
    try:
        head = (d / "progress.md").read_text(encoding="utf-8", errors="replace")[:600]
        m = _STATUS_RE.search(head)
        if m:
            status = (m.group(1) or m.group(2) or "unknown").strip().strip("*").strip().lower()
    except OSError:
        pass
    try:
        mtime = max((d / a).stat().st_mtime for a in present) if present else d.stat().st_mtime
    except OSError:
        mtime = 0
    return {"kind": "MissionReference", "slug": d.name, "source": "workspace-os.project-state", "status": status,
            "artifacts": present, "complete_set": len(present) == len(MISSION_ARTIFACTS), "updated_at": int(mtime)}


def list_missions(root: Path, markers: Iterable[str], limit: int = 40) -> list[dict]:
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and SLUG_RE.match(p.name) and not p.name.startswith(".")]
    refs = [mission_ref(p) for p in dirs]
    refs = [r for r in refs if not any(m in r["slug"].lower() for m in markers)]
    refs.sort(key=lambda r: r["updated_at"], reverse=True)
    return refs[:limit]


def read_mission_artifact(root: Path, slug: str, name: str, markers: Iterable[str]) -> dict:
    if not SLUG_RE.match(slug or "") or name not in MISSION_ARTIFACTS:
        raise ValueError("bad mission slug or artifact name")
    if any(m in slug.lower() for m in markers):
        raise PermissionError("client mission withheld")
    p = (root / slug / name).resolve()
    if root.resolve() not in p.parents:
        raise ValueError("path escapes mission root")
    data = p.read_bytes()[:MAX_TEXT_BYTES]
    return {"kind": "ArtifactReference", "source": "workspace-os.project-state", "mission": slug, "name": name,
            "path": str(p), "size_bytes": p.stat().st_size, "truncated": p.stat().st_size > MAX_TEXT_BYTES,
            "content": redact(data.decode("utf-8", errors="replace"))}


MISSION_LINK_RE = re.compile(r"\.project-state/([a-z0-9][a-z0-9.\-]{1,120})")


def missions_linked(text: str) -> list[str]:
    return list(dict.fromkeys(MISSION_LINK_RE.findall(text or "")))[:5]


# ── task artifacts (kanban workspace dir, read-only) ─────────────────────────

def workspace_artifacts(workspace_path: Optional[str], allowed_root: Path, limit: int = 40) -> list[dict]:
    if not workspace_path:
        return []
    try:
        base = Path(workspace_path).resolve()
    except OSError:
        return []
    if allowed_root.resolve() not in base.parents or not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if len(out) >= limit:
            break
        rel = p.relative_to(base)
        if any(part.startswith(".") or part in ("node_modules", "__pycache__") for part in rel.parts):
            continue
        if p.is_file():
            out.append({"kind": "ArtifactReference", "source": "hermes.kanban.workspace", "name": str(rel),
                        "size_bytes": p.stat().st_size, "readable": p.suffix.lower() in TEXT_SUFFIXES,
                        "updated_at": int(p.stat().st_mtime)})
    return out


def read_workspace_artifact(workspace_path: str, rel: str, allowed_root: Path) -> dict:
    base = Path(workspace_path).resolve()
    if allowed_root.resolve() not in base.parents:
        raise ValueError("workspace outside kanban workspaces root")
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        raise ValueError("artifact path invalid")
    if p.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("binary artifact: not previewable")
    if any(part.startswith(".") for part in p.relative_to(base).parts):
        raise ValueError("hidden files are not served")
    data = p.read_bytes()[:MAX_TEXT_BYTES]
    return {"kind": "ArtifactReference", "source": "hermes.kanban.workspace", "name": rel, "size_bytes": p.stat().st_size,
            "truncated": p.stat().st_size > MAX_TEXT_BYTES, "content": redact(data.decode("utf-8", errors="replace"))}


# ── decisions: idempotency ledger + audit ────────────────────────────────────

class DecisionLedger:
    """Idempotency: same key -> same stored result, mutation applied once."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir
        self.lock = threading.Lock()

    def _path(self) -> Path:
        return self.dir / "decisions.json"

    def get(self, key: str) -> Optional[dict]:
        try:
            return json.loads(self._path().read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError):
            return None

    def run_once(self, key: str, fn: Callable[[], dict]) -> tuple[dict, bool]:
        with self.lock:
            prior = self.get(key)
            if prior is not None:
                return prior, True
            result = fn()
            try:
                data = json.loads(self._path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data[key] = result
            if len(data) > 2000:  # bounded
                for k in list(data)[: len(data) - 2000]:
                    data.pop(k, None)
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f".decisions.{uuid.uuid4().hex}.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path())
            return result, False

    def audit(self, record: dict) -> None:
        rec = {"ts": time.time(), **record}
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "office-audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def recent_audit(self, n: int = 50) -> list[dict]:
        try:
            lines = (self.dir / "office-audit.jsonl").read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out
