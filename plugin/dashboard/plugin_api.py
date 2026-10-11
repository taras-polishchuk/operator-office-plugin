"""Pixel Office: Hermes dashboard plugin backend.

Mounted by the Hermes dashboard at /api/plugins/pixel-office/.

Canonical state stays where it lives:
  * agents  = Hermes profiles        (read via /api/profiles, created via POST /api/profiles)
  * work    = Hermes Kanban          (read via /api/plugins/kanban/*, tasks created via POST /tasks)
  * activity= profile session stores (read-only, see activity.py)

This plugin only owns presentation state under <HERMES_HOME>/state/pixel-office/:
  layout.json, seats.json, settings.json, area-mappings.json, teams.json,
  ids.json (profile -> stable character id), hidden.json.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pixel_office_activity as activity  # noqa: E402

PLUGIN = "pixel-office"
VERSION = "1.4.1"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _origin_guard(request: Request) -> None:
    """Defense in depth for every plugin mutation (the dashboard session token is checked by Hermes core).

    Browsers send Origin on cross-site and same-site POST/PUT/PATCH/DELETE. Only loopback origins reach this
    process legitimately: direct loopback, or the tailnet gateway, which rewrites an allowed tailnet Origin
    to loopback. Anything else is refused before the handler runs.
    """
    if request.method in _SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser client (curl, tests); the core token check still applies
    try:
        host = (urllib.parse.urlparse(origin).hostname or "").lower()
    except ValueError:
        host = ""
    if host not in _LOOPBACK:
        raise HTTPException(403, "cross-origin mutation refused by pixel-office")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise HTTPException(403, "cross-site mutation refused by pixel-office")


router = APIRouter(dependencies=[Depends(_origin_guard)])


# ── paths / storage ──────────────────────────────────────────────────────────

def _hermes_home() -> Path:
    # Machine root, NOT the request-scoped profile home: the dashboard can be
    # switched between profiles, the office (layout, teams) is machine-wide.
    env = os.environ.get("HERMES_HOME")
    if env:
        p = Path(env)
        # A profile home looks like <root>/profiles/<name>; climb to the root.
        if p.parent.name == "profiles":
            p = p.parent.parent
        return p
    return Path.home() / ".hermes"


STATE_DIR = _hermes_home() / "state" / PLUGIN
_io_lock = threading.RLock()


def _read(name: str, default: Any) -> Any:
    p = STATE_DIR / name
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def _write(name: str, data: Any) -> None:
    with _io_lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / f".{name}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_DIR / name)


DEFAULT_SETTINGS = {
    "soundEnabled": False,
    "alwaysShowLabels": True,
    "showAreas": True,
    "ghostHeadlessAgents": False,
    "lastSeenVersion": "",
    "showSystemBase": True,
}


# ── upstream (Hermes dashboard) access ───────────────────────────────────────

def _base(request: Request) -> str:
    port = request.url.port or 9119
    return f"http://127.0.0.1:{port}"


def _headers(request: Request) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    tok = request.headers.get("X-Hermes-Session-Token") or ""
    auth = request.headers.get("Authorization") or ""
    if tok:
        h["X-Hermes-Session-Token"] = tok
    if auth:
        h["Authorization"] = auth
    return h


def _upstream(request: Request, method: str, path: str, body: Any = None, timeout: float = 15) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_base(request) + path, data=data, method=method, headers=_headers(request))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = str(exc)
        raise HTTPException(status_code=exc.code if 400 <= exc.code < 500 else 502,
                            detail={"upstream": path, "error": detail})
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"upstream": path, "error": str(exc)})


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float, fn):
        now = time.monotonic()
        with self.lock:
            hit = self.items.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self.lock:
            self.items[key] = (time.monotonic(), val)
        return val

    def drop(self, *keys: str) -> None:
        with self.lock:
            for k in keys:
                self.items.pop(k, None)


_cache = _Cache()


def _profiles(request: Request) -> list[dict]:
    def load():
        data = _upstream(request, "GET", "/api/profiles")
        return data.get("profiles", data) if isinstance(data, dict) else data
    return _cache.get("profiles", 5, load)


def _workers(request: Request) -> list[dict]:
    def load():
        try:
            return _upstream(request, "GET", "/api/plugins/kanban/workers/active", timeout=5).get("workers", [])
        except HTTPException:
            return []
    return _cache.get("workers", 3, load)


def _board_counts(request: Request) -> dict[str, dict]:
    def load():
        try:
            board = _upstream(request, "GET", "/api/plugins/kanban/board", timeout=10)
        except HTTPException:
            return {}
        out: dict[str, dict] = {}
        for col in board.get("columns", []):
            cname = col.get("name") or col.get("id")
            if cname in ("done", "archived"):
                continue
            for t in col.get("tasks", []):
                who = t.get("assignee")
                if not who:
                    continue
                out.setdefault(who, {}).setdefault(cname, []).append(
                    {"id": t.get("id"), "title": t.get("title"), "priority": t.get("priority"), "status": cname})
        return out
    return _cache.get("board", 15, load)


# ── ids / teams helpers ──────────────────────────────────────────────────────

def _ids_for(names: list[str]) -> dict[str, int]:
    with _io_lock:
        ids = _read("ids.json", {})
        changed = False
        nxt = max(ids.values(), default=0) + 1
        for n in names:
            if n not in ids:
                ids[n] = nxt
                nxt += 1
                changed = True
        if changed:
            _write("ids.json", ids)
        return ids


def _teams() -> list[dict]:
    return _read("teams.json", [])


def _team_index(teams: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for t in teams:
        for m in t.get("members", []):
            idx.setdefault(m, t)
        if t.get("lead"):
            idx[t["lead"]] = t
    return idx


# ── snapshot ─────────────────────────────────────────────────────────────────

_snap_lock = threading.Lock()


def _build_snapshot(request: Request) -> dict:
    profiles = _profiles(request)
    settings = {**DEFAULT_SETTINGS, **_read("settings.json", {})}
    hidden = set(_read("hidden.json", []))
    names = [p["name"] for p in profiles]
    ids = _ids_for(names)
    teams = _teams()
    tindex = _team_index(teams)
    act = activity.all_activity(profiles)
    workers = _workers(request)
    by_worker: dict[str, list] = {}
    for w in workers:
        by_worker.setdefault(w.get("profile") or w.get("task_assignee"), []).append(
            {"task_id": w.get("task_id"), "title": w.get("task_title"), "run_id": w.get("run_id"),
             "started_at": w.get("started_at")})
    counts = _board_counts(request)

    agents = []
    for p in profiles:
        name = p["name"]
        a = act.get(name, {})
        team = tindex.get(name)
        lead = team.get("lead") if team else None
        kanban_running = by_worker.get(name, [])
        status = a.get("status", "idle")
        activity_text = a.get("activity") or ""
        if status == "idle" and kanban_running:
            activity_text = f"Kanban: {kanban_running[0]['title']}"
        c = counts.get(name, {})
        agents.append({
            "id": ids[name],
            "key": name,
            "name": p.get("display_name") or name,
            "profile": name,
            "is_default": bool(p.get("is_default")),
            "model": p.get("model"),
            "provider": p.get("provider"),
            "description": p.get("description") or "",
            "gateway_running": p.get("gateway_running"),
            "team": team.get("name") if team else None,
            "team_id": team.get("id") if team else None,
            "team_color": team.get("color") if team else None,
            "is_lead": bool(team and lead == name),
            "lead_id": ids.get(lead) if lead else None,
            "status": status,
            "awaiting_input": bool(a.get("awaiting_input")),
            "permission": bool(a.get("permission")),
            "activity": activity_text,
            "last_at": a.get("last_at") or 0,
            "tools": a.get("tools", []),
            "subagents": a.get("subagents", []),
            "sessions": a.get("sessions", []),
            "recent_sessions": a.get("recent_sessions", []),
            "primary_session": a.get("primary_session"),
            "kanban": {"running": kanban_running,
                       "counts": {k: len(v) for k, v in c.items()}},
            "hidden": name in hidden or (p.get("is_default") and not settings.get("showSystemBase", True)),
        })
    return {"plugin": PLUGIN, "version": VERSION, "generated_at": time.time(), "agents": agents, "teams": teams}


@router.get("/health")
def health():
    return {"plugin": PLUGIN, "version": VERSION, "state_dir": str(STATE_DIR)}


@router.get("/snapshot")
def snapshot(request: Request):
    with _snap_lock:
        return _cache.get("snapshot", 1.0, lambda: _build_snapshot(request))


@router.get("/bootstrap")
def bootstrap(request: Request):
    bsnap = {
        "plugin": PLUGIN,
        "version": VERSION,
        "settings": {**DEFAULT_SETTINGS, **_read("settings.json", {})},
        "layout": _read("layout.json", None),
        "seats": _read("seats.json", {}),
        "area_mappings": _read("area-mappings.json", {}),
        "teams": _teams(),
    }
    layout = bsnap["layout"]
    if isinstance(layout, dict):
        ids = _ids_for([p["name"] for p in _profiles(request)])
        bsnap["seats_drift"] = _seats_drift(layout, bsnap["seats"], ids)
    return bsnap


# ── presentation state writes ────────────────────────────────────────────────

class LayoutBody(BaseModel):
    layout: dict


def validate_layout(layout: Any) -> list[str]:
    """Structural validation before a layout is persisted (the editor and presets go through here)."""
    errs: list[str] = []
    if not isinstance(layout, dict):
        return ["layout must be an object"]
    cols, rows = layout.get("cols"), layout.get("rows")
    if not (isinstance(cols, int) and isinstance(rows, int) and 4 <= cols <= 160 and 4 <= rows <= 160):
        return ["cols/rows must be integers in 4..160"]
    n = cols * rows
    for k in ("tiles", "tileColors", "carpetTiles", "areaTiles"):
        v = layout.get(k)
        if v is not None and (not isinstance(v, list) or len(v) != n):
            errs.append(f"{k}: expected a list of {n} cells")
    tiles = layout.get("tiles")
    if isinstance(tiles, list) and any(not isinstance(t, int) or not (0 <= t <= 255) for t in tiles):
        errs.append("tiles: integers 0..255 only")
    furn = layout.get("furniture") or []
    if not isinstance(furn, list) or len(furn) > 3000:
        errs.append("furniture: list of at most 3000 items")
    else:
        uids = set()
        for i, f in enumerate(furn):
            if not isinstance(f, dict) or not isinstance(f.get("type"), str) or not isinstance(f.get("uid"), str):
                errs.append(f"furniture[{i}]: uid and type required"); continue
            if f["uid"] in uids:
                errs.append(f"furniture[{i}]: duplicate uid {f['uid'][:40]}")
            uids.add(f["uid"])
            c, r = f.get("col"), f.get("row")
            if not (isinstance(c, int) and isinstance(r, int) and -4 <= c < cols and -4 <= r < rows):
                errs.append(f"furniture[{i}]: col/row out of grid")
            if len(errs) > 20:
                break
    areas = layout.get("areas")
    if areas is not None and (not isinstance(areas, list) or len(areas) > 64):
        errs.append("areas: list of at most 64")
    return errs[:20]


@router.put("/layout")
def put_layout(body: LayoutBody):
    errs = validate_layout(body.layout)
    if errs:
        raise HTTPException(400, {"layout_errors": errs})
    with _io_lock:
        cur = STATE_DIR / "layout.json"
        if cur.is_file():
            hist = STATE_DIR / "backups" / "layout-history"
            hist.mkdir(parents=True, exist_ok=True)
            (hist / f"layout.{time.strftime('%Y%m%dT%H%M%S')}.json").write_bytes(cur.read_bytes())
            for old in sorted(hist.glob("layout.*.json"))[:-20]:  # keep the last 20 versions
                old.unlink(missing_ok=True)
        _write("layout.json", body.layout)
    return {"ok": True}


PRESETS_DIR = Path(__file__).resolve().parent / "presets"


@router.get("/layout/presets")
def list_presets():
    return {"presets": sorted(p.name.removesuffix("-layout.json") for p in PRESETS_DIR.glob("*-layout.json"))}


@router.post("/layout/preset/{name}")
def apply_preset(request: Request, name: str):
    """Replace the office layout with a bundled preset and seat everyone sensibly.

    Current layout + seats are backed up next to them. Palettes are kept.
    Seating: orchestrator -> 'command', research -> 'library', everyone else
    fills the desk pods in id order.
    """
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise HTTPException(400, "bad preset name")
    src = PRESETS_DIR / f"{name}-layout.json"
    if not src.is_file():
        raise HTTPException(404, "preset not found")
    layout = json.loads(src.read_text(encoding="utf-8"))
    errs = validate_layout(layout)
    if errs:
        raise HTTPException(500, {"preset_invalid": errs})
    named = {}
    seats_file = PRESETS_DIR / f"{name}-seats.json"
    if seats_file.is_file():
        named = json.loads(seats_file.read_text(encoding="utf-8"))
    with _io_lock:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        for fname in ("layout.json", "seats.json"):
            cur = STATE_DIR / fname
            if cur.is_file():
                (STATE_DIR / "backups").mkdir(parents=True, exist_ok=True)
                (STATE_DIR / "backups" / f"{fname}.{stamp}").write_bytes(cur.read_bytes())
        _write("layout.json", layout)
        ids = _ids_for([p["name"] for p in _profiles(request)])
        old = _read("seats.json", {})
        special = {"orchestrator": "command", "research": "library"}
        # interleave pods: top0, bot0, top1, bot1 ...
        tops = [named[f"top{i}"] for i in range(16) if f"top{i}" in named]
        bots = [named[f"bot{i}"] for i in range(16) if f"bot{i}" in named]
        pool = [s for pair in zip(tops, bots) for s in pair] + tops[len(bots):] + bots[len(tops):]
        seats = {}
        for prof, aid in sorted(ids.items(), key=lambda kv: kv[1]):
            # Presets may name a seat per profile ("profile:<name>"): the Operator Office preset seats every
            # real profile at its own workstation in its own room.
            seat = named.get(f"profile:{prof}") or named.get(special.get(prof, ""))
            if not seat:
                seat = pool.pop(0) if pool else None
            prev = old.get(str(aid), {})
            seats[str(aid)] = {"palette": prev.get("palette"), "hueShift": prev.get("hueShift"), "seatId": seat}
        _write("seats.json", seats)
    _cache.drop("snapshot")
    return {"ok": True, "preset": name, "cols": layout.get("cols"), "rows": layout.get("rows"),
            "furniture": len(layout.get("furniture", [])), "seats": seats}


class SeatsBody(BaseModel):
    seats: dict


@router.put("/seats")
def put_seats(body: SeatsBody):
    clean = {}
    for k, v in body.seats.items():
        if isinstance(v, dict):
            clean[str(k)] = {"palette": v.get("palette"), "hueShift": v.get("hueShift"), "seatId": v.get("seatId")}
    with _io_lock:
        cur = _read("seats.json", {})
        cur.update(clean)
        _write("seats.json", cur)
    return {"ok": True}


def _named_seats_for_layout(layout: dict) -> dict[str, str]:
    """Return the named-seat mapping (profile:<name> -> seatId) for the active layout.

    The Operator Office preset is the only one that ships explicit per-profile seats. For other layouts we
    fall back to the Hermes-HQ interleave pool (top0/bot0/top1/bot1 ...). When the active layout's furniture
    has no chair uid (e.g. user replaced the floor plan), we return an empty map.
    """
    cols = layout.get("cols")
    furn = layout.get("furniture") or []
    valid_uids = {f.get("uid") for f in furn if isinstance(f, dict) and isinstance(f.get("uid"), str)}
    oo_seats = PRESETS_DIR / "operator-office-seats.json"
    named: dict[str, str] = {}
    if oo_seats.is_file() and len(valid_uids) > 0:
        for prof_name, uid in json.loads(oo_seats.read_text(encoding="utf-8")).items():
            if isinstance(uid, str) and uid in valid_uids:
                named[prof_name] = uid
    if named:
        return named
    hq_seats = PRESETS_DIR / "hermes-hq-seats.json"
    if hq_seats.is_file():
        hq = json.loads(hq_seats.read_text(encoding="utf-8"))
        tops = [hq[f"top{i}"] for i in range(16) if f"top{i}" in hq and hq[f"top{i}"] in valid_uids]
        bots = [hq[f"bot{i}"] for i in range(16) if f"bot{i}" in hq and hq[f"bot{i}"] in valid_uids]
        pool = [s for pair in zip(tops, bots) for s in pair] + tops[len(bots):] + bots[len(tops):]
        for i, uid in enumerate(pool):
            named[f"seat:{i}"] = uid
    return named


def _room_for_uid(layout: dict, uid: str) -> Optional[str]:
    """Return the area label of the room a chair uid belongs to."""
    if not uid:
        return None
    for f in layout.get("furniture") or []:
        if f.get("uid") == uid:
            col, row = f.get("col"), f.get("row")
            area_tiles = layout.get("areaTiles") or []
            cols_n = layout.get("cols") or 0
            if cols_n and 0 <= col < cols_n and 0 <= row < len(area_tiles) // cols_n:
                return area_tiles[row * cols_n + col]
            return None
    return None


def _seats_drift(layout: dict, seats: dict[str, dict], ids_by_name: dict[str, int]) -> list[dict]:
    """Return one entry per profile whose seat is not inside the room its named mapping says it should be.

    Detection is data-driven: takes the *active* layout's areaTiles (so HQ rooms or Operator rooms) and the
    named-seat file. A drift means the on-disk seats.json has moved the profile to a seat in the wrong room.
    """
    named = _named_seats_for_layout(layout)
    drft = []
    for prof_name, aid in ids_by_name.items():
        seat = (seats.get(str(aid)) or {}).get("seatId")
        if not seat:
            drft.append({"profile": prof_name, "aid": aid, "issue": "missing_seat",
                         "expected_room": None, "actual_room": None})
            continue
        expected_seat = named.get(f"profile:{prof_name}")
        if expected_seat is None:
            continue
        expected_room = _room_for_uid(layout, expected_seat)
        actual_room = _room_for_uid(layout, seat)
        if expected_room != actual_room:
            drft.append({"profile": prof_name, "aid": aid, "issue": "wrong_room",
                         "expected_seat": expected_seat, "actual_seat": seat,
                         "expected_room": expected_room, "actual_room": actual_room})
    return drft


@router.post("/seats/apply-named")
def apply_named_seats(request: Request, only_profiles: Optional[str] = None):
    """Re-seat every profile at its named seat from the active preset (idempotent, profile-keyed).

    Robust to profile reordering, new profiles, and removed profiles. Backs up seats.json first. A drift is
    silently corrected. `only_profiles` is a comma-separated allow-list used by tests to reseat a single id.
    """
    layout = _read("layout.json", None)
    if not isinstance(layout, dict):
        raise HTTPException(404, "no layout")
    ids = _ids_for([p["name"] for p in _profiles(request)])
    named = _named_seats_for_layout(layout)
    if not named:
        raise HTTPException(404, "no named seats for this layout")
    allow = set(s for s in (only_profiles or "").split(",") if s) or None
    with _io_lock:
        cur = _read("seats.json", {})
        stamp = time.strftime("%Y%m%dT%H%M%S")
        (STATE_DIR / "backups").mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "backups" / f"seats.json.{stamp}.apply-named").write_bytes(
            (STATE_DIR / "seats.json").read_bytes() if (STATE_DIR / "seats.json").is_file() else b"{}"
        )
        before_drift = _seats_drift(layout, cur, ids)
        changed: dict[str, dict] = {}
        for prof_name, aid in ids.items():
            if allow and prof_name not in allow:
                continue
            seat = named.get(f"profile:{prof_name}")
            if not seat:
                continue
            existing = cur.get(str(aid), {})
            if existing.get("seatId") == seat:
                continue
            cur[str(aid)] = {"palette": existing.get("palette"),
                             "hueShift": existing.get("hueShift"),
                             "seatId": seat}
            changed[prof_name] = {"seatId": seat, "aid": aid}
        _write("seats.json", cur)
    _cache.drop("snapshot")
    return {"ok": True, "changed": changed, "drift_before": before_drift, "layout": layout.get("cols")}


@router.get("/seats/drift")
def seats_drift(request: Request):
    """Return the list of profiles whose seat is not in the room their department is named."""
    layout = _read("layout.json", None)
    if not isinstance(layout, dict):
        return {"ok": True, "drift": [], "layout_present": False}
    ids = _ids_for([p["name"] for p in _profiles(request)])
    seats = _read("seats.json", {})
    drift = _seats_drift(layout, seats, ids)
    return {"ok": True, "drift": drift, "layout_present": True,
            "n_profiles": len(ids), "layout_rooms": [a.get("label") for a in layout.get("areas", [])]}


@router.put("/settings")
def put_settings(body: dict):
    allowed = set(DEFAULT_SETTINGS)
    with _io_lock:
        cur = _read("settings.json", {})
        cur.update({k: v for k, v in body.items() if k in allowed})
        _write("settings.json", cur)
    _cache.drop("snapshot")
    return {**DEFAULT_SETTINGS, **cur}


class MappingsBody(BaseModel):
    mappings: dict


@router.put("/area-mappings")
def put_mappings(body: MappingsBody):
    _write("area-mappings.json", body.mappings)
    return {"ok": True}


class HideBody(BaseModel):
    hidden: bool = True


@router.post("/agents/{name}/hide")
def hide_agent(name: str, body: HideBody):
    with _io_lock:
        cur = set(_read("hidden.json", []))
        (cur.add if body.hidden else cur.discard)(name)
        _write("hidden.json", sorted(cur))
    _cache.drop("snapshot")
    return {"ok": True, "hidden": sorted(cur)}


# ── teams ────────────────────────────────────────────────────────────────────

class TeamBody(BaseModel):
    name: str
    lead: Optional[str] = None
    members: list[str] = Field(default_factory=list)
    color: Optional[str] = None
    description: Optional[str] = ""


def _validate_team(request: Request, body: TeamBody, teams: list[dict], team_id: Optional[str]) -> dict:
    name = body.name.strip()
    if not name or len(name) > 40:
        raise HTTPException(400, "team name must be 1-40 chars")
    if any(t["name"].lower() == name.lower() and t["id"] != team_id for t in teams):
        raise HTTPException(409, f"team '{name}' already exists")
    known = {p["name"] for p in _profiles(request)}
    members = [m for m in dict.fromkeys(body.members) if m]
    if body.lead and body.lead not in members:
        members.insert(0, body.lead)
    unknown = [m for m in members if m not in known]
    if unknown:
        raise HTTPException(400, f"unknown profiles: {', '.join(unknown)}")
    return {"name": name, "lead": body.lead or (members[0] if members else None), "members": members,
            "color": body.color or None, "description": (body.description or "").strip()}


def _save_teams(teams: list[dict], owner_id: str, members: list[str]) -> None:
    # One profile belongs to at most one team: the latest assignment wins.
    for t in teams:
        if t["id"] == owner_id:
            continue
        t["members"] = [m for m in t.get("members", []) if m not in members]
        if t.get("lead") in members:
            t["lead"] = t["members"][0] if t["members"] else None
    _write("teams.json", teams)
    _cache.drop("snapshot")


@router.get("/teams")
def list_teams():
    return {"teams": _teams()}


@router.post("/teams")
def create_team(request: Request, body: TeamBody):
    with _io_lock:
        teams = _teams()
        data = _validate_team(request, body, teams, None)
        team = {"id": uuid.uuid4().hex[:8], **data, "created_at": time.time()}
        teams.append(team)
        _save_teams(teams, team["id"], team["members"])
    return team


@router.patch("/teams/{team_id}")
def update_team(request: Request, team_id: str, body: TeamBody):
    with _io_lock:
        teams = _teams()
        team = next((t for t in teams if t["id"] == team_id), None)
        if not team:
            raise HTTPException(404, "team not found")
        team.update(_validate_team(request, body, teams, team_id))
        _save_teams(teams, team_id, team["members"])
    return team


@router.delete("/teams/{team_id}")
def delete_team(team_id: str):
    with _io_lock:
        teams = _teams()
        left = [t for t in teams if t["id"] != team_id]
        if len(left) == len(teams):
            raise HTTPException(404, "team not found")
        _write("teams.json", left)
    _cache.drop("snapshot")
    return {"ok": True}


# ── agents (Hermes profiles) ─────────────────────────────────────────────────

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class AgentCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    clone_from: Optional[str] = None
    soul: Optional[str] = None
    team_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None


@router.post("/agents")
def create_agent(request: Request, body: AgentCreate):
    """Disabled (office.v2): creating Hermes profiles from the office has no authorized policy.

    Profiles are production agent identity (SOUL, model, approvals). Creating one from a web button bypassed
    review. Use `hermes profile create` from a terminal. The route stays registered so callers get an
    explicit 403 instead of a silent 404, and every attempt is audited.
    """
    _ledger.audit({"action": "profile.create", "result": "refused", "reason": "profile management disabled",
                   "name": (body.name or "")[:40]})
    raise HTTPException(403, "profile creation from the office is disabled (no authorized profile-management policy); "
                             "use `hermes profile create` in a terminal")


@router.get("/agents/{name}")
def agent_detail(request: Request, name: str):
    prof = next((p for p in _profiles(request) if p["name"] == name), None)
    if not prof:
        raise HTTPException(404, "profile not found")
    a = activity.profile_activity(prof["path"])
    tasks = _board_counts(request).get(name, {})
    soul = None
    try:
        s = _upstream(request, "GET", f"/api/profiles/{urllib.parse.quote(name)}/soul", timeout=5)
        soul = (s.get("content") or "")[:1500] if isinstance(s, dict) else None
    except HTTPException:
        pass
    team = _team_index(_teams()).get(name)
    return {"profile": prof, "activity": a, "tasks": tasks, "soul_excerpt": soul, "team": team,
            "workers": [w for w in _workers(request) if (w.get("profile") or w.get("task_assignee")) == name]}


class TaskBody(BaseModel):
    title: str
    body: Optional[str] = ""
    priority: int = 0
    board: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=80)


def _create_task(request: Request, assignee: str, title: str, body: str, priority: int, board: Optional[str],
                 idempotency_key: Optional[str] = None) -> dict:
    if not title.strip():
        raise HTTPException(400, "title required")
    if board and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", board):
        raise HTTPException(400, "bad board")
    q = f"?board={urllib.parse.quote(board)}" if board else ""
    payload = {"title": title.strip(), "body": body or None, "assignee": assignee, "priority": priority}
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    res = _upstream(request, "POST", f"/api/plugins/kanban/tasks{q}", payload)
    _cache.drop("board", "snapshot", f"office:{board}")
    return res


@router.post("/agents/{name}/task")
def agent_task(request: Request, name: str, body: TaskBody):
    if name not in {p["name"] for p in _profiles(request)}:
        raise HTTPException(404, "profile not found")
    return _create_task(request, name, body.title, body.body or "", body.priority, body.board, body.idempotency_key)


@router.post("/teams/{team_id}/task")
def team_task(request: Request, team_id: str, body: TaskBody):
    team = next((t for t in _teams() if t["id"] == team_id), None)
    if not team:
        raise HTTPException(404, "team not found")
    lead = team.get("lead") or (team.get("members") or [None])[0]
    if not lead:
        raise HTTPException(400, "team has no members")
    roster = "\n".join(f"- {m}{' (lead)' if m == lead else ''}" for m in team.get("members", []))
    text = (body.body or "").strip()
    text += (
        f"\n\n---\nTeam: {team['name']}\nLead: {lead}\nMembers:\n{roster}\n\n"
        "Lead coordinates. Split work into Kanban subtasks assigned to team members as needed."
    )
    return _create_task(request, lead, body.title, text.strip(), body.priority, body.board, body.idempotency_key)


# ══ Operator Agent Office: control-plane layer (office.v1) ═══════════════════
# Read-mostly projection. One mutation family: operator decisions on Kanban
# review tasks, bound to an HMAC'd ApprovalRequest, idempotent, audited.
# Task creation stays on /agents/{name}/task (Kanban REST, idempotency_key).

import subprocess  # noqa: E402

import office_control as oc  # noqa: E402
import lanes  # noqa: E402

# Approval store (shared with the gateway plugin). Imported lazily because the
# dashboard hot path must not block on a missing hermes home in tests.
try:
    import approval_store  # noqa: E402
    import approval_kanban  # noqa: E402
except Exception:  # noqa: BLE001
    approval_store = None  # type: ignore
    approval_kanban = None  # type: ignore

MISSIONS_ROOT = Path(os.environ.get("OPERATOR_OFFICE_MISSIONS_ROOT")
                     or (Path(os.environ.get("HOME") or str(Path.home())) / "projects" / ".project-state"))
KANBAN_ROOT = _hermes_home() / "kanban"
_ledger = oc.DecisionLedger(STATE_DIR)


def _board_q(board: Optional[str]) -> str:
    if not board or not oc.valid_board(board):
        raise HTTPException(400, "board query param is required (explicit board selection)")
    return board


def _rooms_cfg() -> tuple[list[dict], list[str]]:
    return oc.load_rooms(_read("rooms.json", None))


def _try(fn, default):
    try:
        return fn(), True
    except HTTPException:
        return default, False


def _tailnet_exposure() -> list[dict]:
    """Detect raw TCP forwards from the tailnet to the dashboard port (the Host-spoof token leak path).

    Cached 30 s. Reads `tailscale serve status --json` only (no root needed). Unknown != safe: a failed
    probe is reported as a warning, never as 'not exposed'.
    """
    def probe() -> list[dict]:
        port = os.environ.get("HERMES_DASHBOARD_PORT", "9119")
        try:
            p = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=4)
            cfg = json.loads(p.stdout or "{}") if p.returncode == 0 else None
        except Exception:  # noqa: BLE001
            cfg = None
        if cfg is None:
            return [{"kind": "Incident", "id": "exposure:tailscale-unknown", "source": "office.exposure", "severity": "warning",
                     "title": "tailnet exposure unknown (tailscale serve status unreadable)", "task_id": None, "board": None,
                     "detail": None}]
        out = []
        for tport, h in (cfg.get("TCP") or {}).items():
            fwd = (h or {}).get("TCPForward") or ""
            if fwd.endswith(":" + str(port)) and fwd.split(":")[0] in ("127.0.0.1", "localhost"):
                out.append({"kind": "Incident", "id": f"exposure:tcp:{tport}", "source": "office.exposure", "severity": "critical",
                            "title": f"tailnet raw TCP {tport} -> dashboard: Host spoof leaks the session token",
                            "task_id": None, "board": None,
                            "detail": "Use the identity gateway: scripts/deploy-gateway.sh switch (tailscale serve --http -> 127.0.0.1:9120)."})
        if cfg.get("AllowFunnel"):
            out.append({"kind": "Incident", "id": "exposure:funnel", "source": "office.exposure", "severity": "critical",
                        "title": "Tailscale Funnel enabled (public internet)", "task_id": None, "board": None, "detail": None})
        return out
    return _cache.get("exposure", 30.0, probe)


def _office_state(request: Request, board: str) -> dict:
    now = time.time()
    rooms, markers = _rooms_cfg()
    settings = _read("settings.json", {})
    client_mode = oc.is_client_board(board, settings)
    qb = urllib.parse.quote(board)
    profiles, ok_prof = _try(lambda: _profiles(request), [])
    board_payload, ok_board = _try(lambda: _upstream(request, "GET", f"/api/plugins/kanban/board?board={qb}", timeout=10), {})
    workers, ok_workers = _try(lambda: _upstream(request, "GET", f"/api/plugins/kanban/workers/active?board={qb}", timeout=5).get("workers", []), [])
    diags, _ = _try(lambda: _upstream(request, "GET", f"/api/plugins/kanban/diagnostics?board={qb}", timeout=10).get("diagnostics", []), [])
    act = activity.all_activity(profiles, now) if profiles else {}

    def all_board_workers() -> list[dict]:
        # Presence must not call a Kanban worker on another board "inferred": collect live workers on every board.
        out = []
        boards = _upstream(request, "GET", "/api/plugins/kanban/boards", timeout=8).get("boards", [])
        for bd in boards:
            slug = bd.get("slug")
            if not slug or bd.get("archived"):
                continue
            ws = workers if slug == board else _upstream(
                request, "GET", f"/api/plugins/kanban/workers/active?board={urllib.parse.quote(slug)}", timeout=5).get("workers", [])
            out += [{**w, "board": slug} for w in ws]
        return out
    presence_workers, _ = _try(lambda: _cache.get("workers:all", 5.0, all_board_workers), workers)

    raw_tasks: list[dict] = []
    for col in board_payload.get("columns", []):
        for t in col.get("tasks", []):
            t.setdefault("status", col.get("name"))
            raw_tasks.append(t)
    # On a client board every task is client by definition (D9): no is_client filtering,
    # no client-marker withholding. On personal boards the existing behaviour is unchanged.
    client_ids: set[str] = set()
    if client_mode:
        visible = list(raw_tasks)
    else:
        client_ids = {t["id"] for t in raw_tasks if oc.is_client(t, markers)}
        visible = [t for t in raw_tasks if t["id"] not in client_ids]
    recent_cut = now - 48 * 3600
    shown = [t for t in visible if t.get("status") in oc.OPEN_STATUSES
             or (t.get("status") == "done" and (t.get("completed_at") or 0) >= recent_cut)]
    shown.sort(key=lambda t: (-(t.get("priority") or 0), -(t.get("created_at") or 0)))

    room_of_profile = {}
    for r in rooms:
        for p in r.get("residents", []):
            room_of_profile.setdefault(p, r["id"])
    tasks = []
    for t in shown[:400]:
        rid, why = oc.route_task(t, rooms, markers)
        tasks.append(oc.task_ref(t, board, rid, why))

    names = {p["name"] for p in profiles}
    agents = []
    for p in profiles:
        agents.append({**oc.agent_profile(p, room_of_profile),
                       "presence": oc.agent_presence(p, act.get(p["name"]), presence_workers, visible, now),
                       "character_id": _ids_for([p["name"]]).get(p["name"])})
    orphan_assignees = sorted({t.get("assignee") for t in visible
                               if t.get("assignee") and t.get("assignee") not in names and t.get("status") in oc.OPEN_STATUSES})

    approvals = []
    for t in [x for x in visible if oc.approval_kind(x, names)][:30]:
        detail, ok = _try(lambda: _upstream(request, "GET", f"/api/plugins/kanban/tasks/{urllib.parse.quote(t['id'])}?board={qb}", timeout=8), None)
        if not (ok and detail):
            continue
        kind = oc.approval_kind(detail["task"], names)  # board cards may omit block_kind: decide on full task
        if kind:
            fp = oc.task_fingerprint(detail["task"], detail.get("events", []))
            approvals.append(oc.approval_request(detail["task"], board, fp, now, kind))

    health = {"hermes.profiles": ok_prof, "hermes.kanban": ok_board, "hermes.kanban.workers": ok_workers}
    human = set(orphan_assignees) | {"taras"}
    incidents = _tailnet_exposure() + oc.incidents_from(board_payload, diags, board, client_ids, health, human)
    operator_queue = [oc.task_ref(t, board, *oc.route_task(t, rooms, markers)) for t in visible
                      if t.get("assignee") in human and t.get("status") in ("ready", "todo", "triage")][:60]

    room_out = []
    for r in rooms:
        rt = [t for t in tasks if t["room"] == r["id"]]
        counts: dict[str, int] = {}
        for t in rt:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        locked = r["sensitivity"] == "client"
        # On a client board the locked Client Work room is redundant (this board IS the
        # client work surface). Suppress it so the floor plan matches the actual surface.
        if locked and client_mode:
            continue
        room_out.append({
            "kind": "OfficeRoom", "id": r["id"], "name": r["name"], "glyph": r["glyph"], "color": r["color"],
            "purpose": r["purpose"], "sensitivity": r["sensitivity"], "locked": locked,
            "residents": [p for p in r.get("residents", []) if p in names],
            "missing_residents": [p for p in r.get("residents", []) if p not in names],
            "counts": counts if not locked else {},
            "withheld": len(client_ids) if locked else 0,
            "incidents": len([i for i in incidents if any(t["id"] == i.get("task_id") for t in rt)]),
            "approvals": len([a for a in approvals if any(t["id"] == a["target"]["task_id"] for t in rt)]),
        })
    return {
        "contract": oc.CONTRACT_VERSION, "plugin": PLUGIN, "version": VERSION,
        "board": board, "generated_at": now,
        "client_mode": client_mode,
        "cursor": board_payload.get("latest_event_id"),
        "sources": health,
        "rooms": room_out, "agents": agents, "tasks": [t for t in tasks if t["room"] != "client"],
        "orphan_assignees": orphan_assignees,
        "approvals": approvals, "incidents": incidents, "operator_queue": operator_queue,
        "withheld_client_tasks": len(client_ids),
        "withheld_client_tasks_note": (
            "move manually if desired" if (not client_mode and len(client_ids) > 0) else None
        ),
        "totals": {"open": len([t for t in visible if t.get("status") in oc.OPEN_STATUSES]),
                   "done_total": len([t for t in visible if t.get("status") == "done"]),
                   "shown": len(tasks)},
        "cost": {"status": "unknown", "reason": "no per-task cost source wired"},
        "lanes": _lane_runs(visible, board),
        "policy": {"profile_management": "disabled", "external_adapters": "disabled",
                   "lane_boards": _lane_boards(), "client_boards": _client_boards(),
                   "presence": "kanban worker = confirmed execution; session = inferred"},
    }


@router.get("/office/boards")
def office_boards(request: Request):
    data = _upstream(request, "GET", "/api/plugins/kanban/boards", timeout=8)
    clients = set(_client_boards())
    return {"boards": [{"slug": b.get("slug"), "name": b.get("name"), "counts": b.get("counts"),
                        "is_current": b.get("is_current"),
                        "is_client": b.get("slug") in clients}
                       for b in data.get("boards", []) if not b.get("archived")],
            "selected": _read("settings.json", {}).get("officeBoard"),
            "client_boards": sorted(clients)}


@router.get("/office/state")
def office_state(request: Request, board: Optional[str] = None):
    b = _board_q(board)
    return _cache.get(f"office:{b}", 1.5, lambda: _office_state(request, b))


@router.get("/office/tasks/{task_id}")
def office_task(request: Request, task_id: str, board: Optional[str] = None, log_tail: int = 6000):
    b = _board_q(board)
    if not oc.valid_task_id(task_id):
        raise HTTPException(400, "bad task id")
    rooms, markers = _rooms_cfg()
    settings = _read("settings.json", {})
    client_mode = oc.is_client_board(b, settings)
    qb, qt = urllib.parse.quote(b), urllib.parse.quote(task_id)
    d = _upstream(request, "GET", f"/api/plugins/kanban/tasks/{qt}?board={qb}", timeout=10)
    t = d["task"]
    if not client_mode and oc.is_client(t, markers):
        raise HTTPException(403, "client task withheld from the personal office")
    rid, why = oc.route_task(t, rooms, markers)
    log, _ = _try(lambda: _upstream(request, "GET", f"/api/plugins/kanban/tasks/{qt}/log?board={qb}&tail={max(500, min(log_tail, 60000))}", timeout=8), {})
    names = {p["name"] for p in _profiles(request)}
    kind = oc.approval_kind(t, names)
    approval = oc.approval_request(t, b, oc.task_fingerprint(t, d.get("events", [])), time.time(), kind) if kind else None
    declared = []
    for e in d.get("events", []):
        pl = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        meta = pl.get("metadata") if isinstance(pl.get("metadata"), dict) else {}
        for a in (pl.get("artifacts") or meta.get("artifacts") or []):
            if isinstance(a, str) and a not in declared:
                declared.append(a)
    return {
        "contract": oc.CONTRACT_VERSION,
        "task": {**oc.task_ref(t, b, rid, why),
                 "body": oc.redact(t.get("body") or ""), "result": oc.redact(t.get("result") or ""),
                 "latest_summary": oc.redact(t.get("latest_summary") or ""),
                 "workspace_kind": t.get("workspace_kind"), "workspace_path": t.get("workspace_path"),
                 "session_id": t.get("session_id"), "last_failure_error": oc.redact(t.get("last_failure_error") or "")},
        "events": [oc.office_event(e, b) for e in d.get("events", [])],
        "runs": [{"kind": "RunReference", "id": r.get("id"), "profile": r.get("profile"), "status": r.get("status"),
                  "outcome": r.get("outcome"), "started_at": r.get("started_at"), "ended_at": r.get("ended_at"),
                  "summary": oc.redact((r.get("summary") or "")[:1200]), "error": oc.redact(r.get("error") or "")}
                 for r in d.get("runs", [])],
        "comments": [{"id": c.get("id"), "author": c.get("author"), "created_at": c.get("created_at"),
                      "body": oc.redact((c.get("body") or "")[:4000])} for c in d.get("comments", [])],
        "attachments": [{"kind": "ArtifactReference", "source": "hermes.kanban.attachment", "id": a.get("id"),
                         "name": a.get("filename") or a.get("name"), "size_bytes": a.get("size_bytes") or a.get("size"),
                         "readable": Path(a.get("filename") or "").suffix.lower() in oc.TEXT_SUFFIXES}
                        for a in d.get("attachments", [])],
        "declared_artifacts": [{"kind": "ArtifactReference", "source": "hermes.kanban.declared", "name": Path(a).name,
                                "path": a, "preserved": str(KANBAN_ROOT) in a} for a in declared[:20]],
        "workspace_artifacts": oc.workspace_artifacts(t.get("workspace_path"), KANBAN_ROOT),
        "missions": oc.missions_linked(t.get("body") or ""),
        "log": {"exists": bool(log.get("exists")), "size_bytes": log.get("size_bytes"), "truncated": log.get("truncated"),
                "content": oc.redact(log.get("content") or "")},
        "approval": approval,
    }


@router.get("/office/tasks/{task_id}/artifact")
def office_task_artifact(request: Request, task_id: str, path: str, board: Optional[str] = None):
    b = _board_q(board)
    if not oc.valid_task_id(task_id):
        raise HTTPException(400, "bad task id")
    _, markers = _rooms_cfg()
    settings = _read("settings.json", {})
    client_mode = oc.is_client_board(b, settings)
    d = _upstream(request, "GET", f"/api/plugins/kanban/tasks/{urllib.parse.quote(task_id)}?board={urllib.parse.quote(b)}", timeout=10)
    if not client_mode and oc.is_client(d["task"], markers):
        raise HTTPException(403, "client task withheld from the personal office")
    try:
        return oc.read_workspace_artifact(d["task"].get("workspace_path") or "", path, KANBAN_ROOT)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/office/tasks/{task_id}/attachments/{att_id}")
def office_task_attachment(request: Request, task_id: str, att_id: int, board: Optional[str] = None):
    b = _board_q(board)
    if not oc.valid_task_id(task_id):
        raise HTTPException(400, "bad task id")
    _, markers = _rooms_cfg()
    settings = _read("settings.json", {})
    client_mode = oc.is_client_board(b, settings)
    qb, qt = urllib.parse.quote(b), urllib.parse.quote(task_id)
    d = _upstream(request, "GET", f"/api/plugins/kanban/tasks/{qt}?board={qb}", timeout=10)
    if not client_mode and oc.is_client(d["task"], markers):
        raise HTTPException(403, "client task withheld from the personal office")
    att = next((a for a in d.get("attachments", []) if a.get("id") == att_id), None)
    if not att:
        raise HTTPException(404, "attachment does not belong to this task")
    name = att.get("filename") or ""
    if Path(name).suffix.lower() not in oc.TEXT_SUFFIXES:
        raise HTTPException(400, "binary attachment: open it in Kanban")
    req = urllib.request.Request(_base(request) + f"/api/plugins/kanban/attachments/{att_id}?board={qb}", headers=_headers(request))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read(oc.MAX_TEXT_BYTES + 1)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"attachment fetch failed: {exc.__class__.__name__}")
    return {"kind": "ArtifactReference", "source": "hermes.kanban.attachment", "name": name, "id": att_id,
            "truncated": len(data) > oc.MAX_TEXT_BYTES,
            "content": oc.redact(data[: oc.MAX_TEXT_BYTES].decode("utf-8", errors="replace"))}


@router.get("/office/missions")
def office_missions():
    _, markers = _rooms_cfg()
    return {"root": str(MISSIONS_ROOT), "missions": oc.list_missions(MISSIONS_ROOT, markers)}


@router.get("/office/missions/{slug}/{name}")
def office_mission_artifact(slug: str, name: str):
    _, markers = _rooms_cfg()
    try:
        return oc.read_mission_artifact(MISSIONS_ROOT, slug, name, markers)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404 if isinstance(exc, FileNotFoundError) else 400, str(exc))


class DecisionBody(BaseModel):
    board: str
    task_id: str
    request_id: str
    decision: str
    token: str
    fingerprint: str
    expires_at: int
    idempotency_key: str = Field(min_length=8, max_length=80)
    note: Optional[str] = Field(default="", max_length=2000)
    confirm: bool = False


class RerequestBody(BaseModel):
    board: str
    task_id: str
    kind: str
    episode: Optional[str] = None
    fingerprint: Optional[str] = None
    action: Optional[dict] = None
    summary: Optional[dict] = None
    ttl_s: Optional[int] = Field(default=None, ge=60, le=86400)
    confirm: bool = False


def _cli_request_changes(board: str, task_id: str, reason: str) -> tuple[bool, str]:
    # Human-lane review has no active reviewer run, so `request-changes` (reviewer
    # verdict on a live review run) is refused by Hermes. `reopen-review` is the
    # verb for "send a review task back for changes"; it records the reason as a comment.
    env = {**os.environ, "HERMES_HOME": str(_hermes_home())}
    cmd = [sys.executable, "-m", "hermes_cli.main", "kanban", "--board", board, "reopen-review", task_id,
           "--reason", "[operator decision via Operator Office] CHANGES REQUESTED: " + reason]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env, cwd=str(_hermes_home()))
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return p.returncode == 0, oc.redact((p.stdout or "") + (p.stderr or ""))[-800:]


def _store_or_none():
    """Return the approval Store singleton, or None when the hermes home is unreadable."""
    if approval_store is None:
        return None
    try:
        return approval_store.open_store()
    except Exception:  # noqa: BLE001
        return None


def _store_row_for_request(board: str, task_id: str, kind: str, fingerprint: str) -> Optional[dict]:
    """Look up the current pending store row for an Office decision request.

    Used so the Office + Telegram channels coordinate through the same CAS.
    Returns the row dict or None when no store row exists (legacy behaviour
    keeps applying directly).
    """
    store = _store_or_none()
    if store is None:
        return None
    qb = (board or "").lower()
    pending = store.queue(board=None, limit=200)["pending"]
    for r in pending:
        if r["board"].lower() != qb or r["task_id"] != task_id:
            continue
        if r["kind"] != kind:
            continue
        if r["fingerprint"] == fingerprint or r["action_digest"] is None:
            return r
    return None


@router.get("/office/approvals/queue")
def office_approvals_queue(request: Request, board: Optional[str] = None, limit: int = 100):
    """Return pending + last 50 decided approvals from the shared store.

    `board` filters pending to that board (decided is shown across all boards).
    Falls back to the legacy projection when the store is unreachable (in
    which case it still derives pending rows from the live Kanban).
    """
    b = _board_q(board)
    store = _store_or_none()
    if store is None:
        raise HTTPException(503, "approval store unreachable")
    lim = max(1, min(int(limit), 200))
    payload = store.queue(board=b, limit=lim)
    # Augment each row with a small 'via' badge + the request id (for the UI).
    for r in payload["pending"] + payload["decided"]:
        r["via_badge"] = (r.get("decided_via") or "office").split(":", 1)[0]
        r["decided_by_label"] = r.get("decided_by") or ""
    return {"contract": "office.approvals.queue.v1", "board": b,
            "pending": payload["pending"], "decided": payload["decided"]}


@router.post("/office/approvals/decision")
def office_decision(request: Request, body: DecisionBody):
    if not body.confirm:
        raise HTTPException(400, "explicit confirm=true required")
    if not oc.valid_board(body.board) or not oc.valid_task_id(body.task_id):
        raise HTTPException(400, "bad board or task id")
    parsed = oc.parse_request_id(body.request_id)
    if not parsed or parsed[1] != body.board or parsed[2] != body.task_id:
        raise HTTPException(400, "request_id does not match target")
    kind = parsed[0]
    if body.decision not in oc.APPROVAL_KINDS[kind]:
        raise HTTPException(400, f"decision for {kind} must be one of {list(oc.APPROVAL_KINDS[kind])}")
    _, markers = _rooms_cfg()
    settings = _read("settings.json", {})
    client_mode = oc.is_client_board(body.board, settings)
    key = f"{body.idempotency_key}:{body.request_id}:{body.decision}"
    if client_mode:
        # D9: client boards record ids + board + action only. No title, no body, no note text.
        audit_base = oc.client_audit_record(kind + ".decide", body.board, body.task_id)
        audit_base = {**audit_base, "decision": body.decision, "request_id": body.request_id,
                      "idempotency_key": body.idempotency_key}
    else:
        audit_base = {"action": kind + ".decide", "decision": body.decision, "board": body.board,
                      "task_id": body.task_id, "request_id": body.request_id, "idempotency_key": body.idempotency_key}

    def apply() -> dict:
        qb, qt = urllib.parse.quote(body.board), urllib.parse.quote(body.task_id)
        d = _upstream(request, "GET", f"/api/plugins/kanban/tasks/{qt}?board={qb}", timeout=10)
        t = d["task"]

        def refuse(code: int, reason: str):
            _ledger.audit({**audit_base, "result": "refused", "reason": reason})
            raise HTTPException(code, reason)

        if oc.is_client(t, markers) and not client_mode:
            refuse(403, "client task: decisions not allowed from the personal office")
        names = {p["name"] for p in _profiles(request)}
        if oc.approval_kind(t, names) != kind:
            refuse(409, f"task is '{t.get('status')}' (block_kind={t.get('block_kind')}): no longer a {kind} gate")
        fp_now = oc.task_fingerprint(t, d.get("events", []))
        if fp_now != body.fingerprint:
            refuse(409, "task changed since the approval was issued; refresh and review again")
        why = oc.verify_approval(body.token, body.request_id, body.decision, body.task_id, body.board,
                                 body.fingerprint, body.expires_at, time.time())
        if why:
            refuse(403, why)
        note = (body.note or "").strip()
        if kind == "lane.gate":
            outcome = _apply_lane_gate(request, body, t, refuse)
        elif kind == "kanban.needs_input":
            if body.decision == "reject" and not note:
                refuse(400, "reject needs a note for the worker")
            verdict = "APPROVED" if body.decision == "approve" else "REJECTED (do not proceed)"
            _upstream(request, "POST", f"/api/plugins/kanban/tasks/{qt}/comments?board={qb}",
                      {"body": f"[operator decision via Operator Office] {verdict}" + (f": {note}" if note else "")
                               + f"\nrequest={body.request_id}", "author": "operator"})
            _upstream(request, "PATCH", f"/api/plugins/kanban/tasks/{qt}?board={qb}", {"status": "ready"})
            # WP-12 H3 defect 4: client boards keep only the decision verb in
            # the audit dict (no separate "verdict" field). The non-client path
            # keeps the human-readable verdict for the operator's audit trail.
            if client_mode:
                outcome = {"status": "ready"}
            else:
                outcome = {"status": "ready", "verdict": verdict}
        elif body.decision == "approve":
            summary = "Approved by operator via Operator Office." + (f" Note: {note}" if note else "")
            _upstream(request, "PATCH", f"/api/plugins/kanban/tasks/{qt}?board={qb}",
                      {"status": "done", "summary": summary,
                       "metadata": {"approved_via": "operator-office", "request_id": body.request_id}})
            outcome = {"status": "done"}
        else:
            if not note:
                refuse(400, "request_changes needs a note for the implementer")
            ok, out = _cli_request_changes(body.board, body.task_id, note)
            if not ok:
                _ledger.audit({**audit_base, "result": "failed", "reason": out})
                raise HTTPException(409, f"kanban refused reopen-review: {out}")
            outcome = {"status": "reopened_for_changes", "cli": out}
        res = {"ok": True, "decision": body.decision, "task_id": body.task_id, "board": body.board,
               "applied_at": time.time(), **outcome}
        _ledger.audit({**audit_base, "result": "applied", **outcome})
        return res

    # ── store CAS: when the gateway plugin upserted a pending row for this
    # (board, task_id, kind, fingerprint) we MUST win the store CAS before
    # applying. Telegram would otherwise race the Office path.
    store_row = _store_row_for_request(body.board, body.task_id, kind, body.fingerprint)
    if store_row is not None and approval_kanban is not None:
        store = _store_or_none()
        if store is not None:
            def _verify(rec: dict) -> tuple[str, Optional[str]]:
                # Re-read via kanban_db for an authoritative fingerprint/digest.
                try:
                    task, events = approval_kanban.read_task(body.board, body.task_id)
                except Exception as exc:  # noqa: BLE001
                    return "stale", f"read_task failed: {exc}"
                if not task:
                    return "stale", "task no longer exists"
                fp = oc.task_fingerprint(task, events)
                if fp != rec.get("fingerprint"):
                    return "stale", f"fingerprint changed ({rec.get('fingerprint')} -> {fp})"
                live_action = approval_kanban.build_action(task, body.board, kind)
                if approval_store.action_digest(live_action) != rec.get("action_digest"):
                    return "stale", "action digest changed"
                return "ok", None

            # HMAC still binds the operator's button to (request_id, decision,
            # task_id, board, fingerprint, expiry) — independent of the store CAS.
            why = oc.verify_approval(body.token, body.request_id, body.decision, body.task_id,
                                     body.board, body.fingerprint, body.expires_at, time.time())
            if why:
                _ledger.audit({**audit_base, "result": "refused", "reason": f"hmac: {why}"})
                raise HTTPException(403, why)

            res = store.decide(store_row["id"], body.decision, actor=f"office:{body.idempotency_key}",
                               via="office", now=time.time(), verify=_verify)
            if res.outcome in ("unknown",):
                raise HTTPException(404, res.detail or "approval not found")
            if res.outcome in ("already_decided", "cas_lost"):
                _ledger.audit({**audit_base, "result": res.outcome,
                               "who": res.row.get("decided_by") if res.row else None,
                               "when_via": res.row.get("decided_via") if res.row else None,
                               "detail": res.detail})
                raise HTTPException(409, {"error": res.outcome, "detail": res.detail,
                                          "row": res.row})
            if res.outcome == "expired":
                raise HTTPException(410, res.detail or "approval expired")
            if res.outcome == "stale":
                raise HTTPException(409, {"error": "stale", "detail": res.detail,
                                          "row": res.row})
            if res.outcome != "decided":
                raise HTTPException(500, f"store.decide returned unexpected outcome: {res.outcome}")
            # CAS winner: apply via approval_kanban.apply (single source of truth).
            apply_res = approval_kanban.apply(res.row, body.decision, "office", body.request_id)
            store.record_apply(res.row["id"], bool(apply_res.get("ok")), apply_res)
            _cache.drop(f"office:{body.board}", "board", "snapshot")
            return {**apply_res, "decision": body.decision, "task_id": body.task_id,
                    "board": body.board, "approval_id": res.row["id"],
                    "applied_at": time.time(), "via_store": True,
                    "idempotent_replay": False}

    # ── legacy path (no store row): keep prior behaviour unchanged. ────────
    result, replayed = _ledger.run_once(key, apply)
    _cache.drop(f"office:{body.board}", "board", "snapshot")
    return {**result, "idempotent_replay": replayed}


@router.post("/office/approvals/rerequest")
def office_rerequest(request: Request, body: RerequestBody):
    """Create a new pending approval row for an expired episode (F3).

    Same auth/origin guards as ``/office/approvals/decision`` (operator
    session, valid board+task id). Allowed only when the gate is still
    open in Kanban and the latest store row for the episode is
    expired/rejected/stale/superseded/absent. When a pending row is
    still live, returns 409 ``gate_already_pending``.
    """
    if not body.confirm:
        raise HTTPException(400, "explicit confirm=true required")
    if not oc.valid_board(body.board) or not oc.valid_task_id(body.task_id):
        raise HTTPException(400, "bad board or task id")
    if body.kind not in oc.APPROVAL_KINDS:
        raise HTTPException(400, f"unknown kind: {body.kind!r}")
    if not body.episode and not body.fingerprint:
        raise HTTPException(400, "episode or fingerprint required")
    # Refuse for client tasks on personal boards (matches the decision route).
    qb, qt = urllib.parse.quote(body.board), urllib.parse.quote(body.task_id)
    d = _upstream(request, "GET", f"/api/plugins/kanban/tasks/{qt}?board={qb}", timeout=10)
    t = d.get("task") or {}
    _, markers = _rooms_cfg()
    settings = _read("settings.json", {})
    client_mode = oc.is_client_board(body.board, settings)
    if not client_mode and oc.is_client(t, markers):
        raise HTTPException(403, "client task: re-request not allowed from the personal office")
    names = {p["name"] for p in _profiles(request)}
    current_kind = oc.approval_kind(t, names)
    if current_kind != body.kind:
        raise HTTPException(409, f"task is '{t.get('status')}' (block_kind={t.get('block_kind')}): "
                                f"no longer a {body.kind} gate")
    store = _store_or_none()
    if store is None:
        raise HTTPException(503, "approval store unreachable")
    if approval_kanban is None:
        raise HTTPException(503, "approval bridge unavailable")
    # Read live events for an authoritative episode id when the caller
    # didn't pin one.
    try:
        _, events = approval_kanban.read_task(body.board, body.task_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"read_task failed: {exc}")
    ep = body.episode or ("fp:" + (body.fingerprint or oc.task_fingerprint(t, events)))
    ttl = int(body.ttl_s) if body.ttl_s else oc.APPROVAL_TTL_S
    row = store.rerequest(body.board, body.task_id, body.kind,
                          fingerprint=body.fingerprint, action=body.action,
                          summary=body.summary, ttl_s=ttl, actor="office",
                          episode=ep)
    if row is None:
        # Either the episode still has a pending row, or we have no
        # binding to recreate. Distinguish the two for the operator UI.
        live = store._conn.execute(  # noqa: SLF001 - test seam, cheap
            "SELECT id, status FROM approvals "
            "WHERE board = ? AND task_id = ? AND kind = ? AND episode = ? "
            "AND status = 'pending' LIMIT 1",
            (body.board, body.task_id, body.kind, ep),
        ).fetchone()
        if live:
            raise HTTPException(409, {"error": "gate_already_pending",
                                      "episode": ep, "approval_id": live["id"]})
        raise HTTPException(404, {"error": "episode_not_found",
                                 "episode": ep,
                                 "board": body.board, "task_id": body.task_id})
    _cache.drop(f"office:{body.board}", "board", "snapshot")
    return {"contract": "office.approvals.rerequest.v1",
            "row": row, "episode": ep, "board": body.board,
            "task_id": body.task_id, "kind": body.kind}


@router.get("/office/audit")
def office_audit(limit: int = 50):
    return {"records": _ledger.recent_audit(max(1, min(limit, 200)))}


@router.put("/office/board")
def office_select_board(body: dict):
    b = body.get("board")
    if not oc.valid_board(b or ""):
        raise HTTPException(400, "bad board")
    with _io_lock:
        cur = _read("settings.json", {})
        cur["officeBoard"] = b
        _write("settings.json", cur)
    return {"ok": True, "board": b}


# ══ Governed lanes (office.lanes.v1) ═════════════════════════════════════════
# Templates expand into ordinary Kanban task chains (parents = dependencies) executed by the existing
# dispatcher. The office keeps no lane state: runs are projected from Kanban titles. The gate decision is
# the only place an execution adapter can run, and every adapter is disabled (see lanes.py).

DEFAULT_LANE_BOARDS = ["operator-office-lab"]


def _lane_boards() -> list[str]:
    """Boards where lanes may be launched. Production boards need an explicit operator policy (blockers.md)."""
    v = _read("settings.json", {}).get("laneBoards")
    return [b for b in v if isinstance(b, str) and oc.valid_board(b)] if isinstance(v, list) else list(DEFAULT_LANE_BOARDS)


def _client_boards() -> list[str]:
    """Boards treated as confidential client work. On these every task is client by definition.

    Settings key ``clientBoards`` (default ``[\"client-work\"]``). Personal boards (default, lab)
    keep the existing ``is_client`` 403 lock unchanged.
    """
    return oc.client_boards(_read("settings.json", {}))


def _settings() -> dict:
    """Raw settings.json (defaults do NOT apply here)."""
    return _read("settings.json", {})


def _approved_gates() -> dict:
    return _read("lane-approvals.json", {})


def _lane_runs(tasks: list[dict], board: str) -> list[dict]:
    approved = _approved_gates()
    out = []
    for run, r in lanes.group_runs(tasks).items():
        st = lanes.run_status(r["template"], r["steps"], {k for k, v in approved.items() if v.get("board") == board})
        tpl = lanes.TEMPLATES[r["template"]]
        out.append({"kind": "LaneRun", "run": run, "template": r["template"], "name": tpl["name"], "room": tpl["room"],
                    "adapter": lanes.adapter_public(tpl["adapter"]), **st,
                    "steps": [{"key": s["key"], "kind": s["kind"], "assignee": s["assignee"],
                               "task_id": (r["steps"].get(s["key"]) or {}).get("id"),
                               "status": (r["steps"].get(s["key"]) or {}).get("status")} for s in tpl["steps"]],
                    "created_at": min((t.get("created_at") or 0) for t in r["steps"].values())})
    out.sort(key=lambda x: -(x["created_at"] or 0))
    return out[:50]


@router.get("/office/lanes/templates")
def lane_templates(request: Request):
    names = {p["name"] for p in _profiles(request)}
    return {"contract": lanes.LANES_CONTRACT, "lane_boards": _lane_boards(),
            "templates": [lanes.template_public(t, names) for t in lanes.TEMPLATES],
            "adapters": [lanes.adapter_public(k) for k in lanes.ADAPTERS]}


@router.get("/office/lanes/runs")
def lane_runs(request: Request, board: Optional[str] = None):
    b = _board_q(board)
    st = office_state(request, b)
    return {"board": b, "runs": st.get("lanes", [])}


class LaneRunBody(BaseModel):
    template: str
    board: str
    inputs: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=80)
    confirm: bool = False


@router.post("/office/lanes/runs")
def lane_start(request: Request, body: LaneRunBody):
    audit = {"action": "lane.start", "template": body.template, "board": body.board, "idempotency_key": body.idempotency_key}
    if not body.confirm:
        raise HTTPException(400, "explicit confirm=true required")
    if not oc.valid_board(body.board):
        raise HTTPException(400, "bad board")
    if body.board not in _lane_boards():
        _ledger.audit({**audit, "result": "refused", "reason": "board not enabled for lanes"})
        raise HTTPException(403, f"lanes are enabled only on {_lane_boards()}; production boards need an operator policy decision")
    if body.template not in lanes.TEMPLATES:
        raise HTTPException(404, "unknown template")
    names = {p["name"] for p in _profiles(request)}
    probs = lanes.template_problems(body.template, names)
    if probs:
        raise HTTPException(409, {"template_problems": probs})
    clean, errs = lanes.validate_inputs(body.template, body.inputs)
    if errs:
        raise HTTPException(400, {"input_errors": errs})
    _, markers = _rooms_cfg()
    if oc.is_client({"title": " ".join(clean.values())}, markers):
        _ledger.audit({**audit, "result": "refused", "reason": "client marker in inputs"})
        raise HTTPException(403, "client work cannot run in personal lanes")
    run = lanes.run_id_for(body.board + ":" + body.idempotency_key)
    qb = urllib.parse.quote(body.board)
    ids: dict[str, str] = {}
    for spec in lanes.build_chain(body.template, run, clean, body.board):
        payload = {"title": spec["title"], "body": spec["body"], "assignee": spec["assignee"], "tenant": spec["tenant"],
                   "priority": 0, "parents": [ids[a] for a in spec["after"]], "idempotency_key": spec["idempotency_key"]}
        try:
            res = _upstream(request, "POST", f"/api/plugins/kanban/tasks?board={qb}", payload)
        except HTTPException as exc:
            # Partial chain: idempotency keys make a retry with the same key complete it without duplicates.
            _ledger.audit({**audit, "result": "failed", "run": run, "created": ids, "failed_step": spec["step"],
                           "reason": str(exc.detail)[:300]})
            raise HTTPException(502, {"error": "chain creation failed; retry with the same idempotency_key",
                                      "run": run, "created": ids, "failed_step": spec["step"]})
        ids[spec["step"]] = (res.get("task") or {}).get("id")
    _cache.drop(f"office:{body.board}", "board", "snapshot")
    _ledger.audit({**audit, "result": "applied", "run": run, "tasks": ids})
    return {"ok": True, "run": run, "template": body.template, "board": body.board, "tasks": ids}


def _lane_gate_context(request: Request, board: str, gate_task: dict) -> tuple[str, str, dict]:
    tid, run, _ = lanes.parse_marker(gate_task.get("title") or "")  # type: ignore[misc]
    qb = urllib.parse.quote(board)
    bp = _upstream(request, "GET", f"/api/plugins/kanban/board?board={qb}", timeout=45)  # host load can be high
    run_tasks = [t for col in bp.get("columns", []) for t in col.get("tasks", [])
                 if (lanes.parse_marker(t.get("title") or "") or ("", "", ""))[1] == run]
    details = {}
    for t in run_tasks:
        step = lanes.parse_marker(t["title"])[2]  # type: ignore[index]
        if step == lanes.TEMPLATES[tid]["steps"][-1]["key"]:
            continue
        d = _upstream(request, "GET", f"/api/plugins/kanban/tasks/{urllib.parse.quote(t['id'])}?board={qb}", timeout=30)
        details[step] = {"task": d["task"], "runs": d.get("runs", []), "attachments": d.get("attachments", []),
                         "events": d.get("events", [])}
    return tid, run, details


def _upload_attachment(request: Request, board: str, task_id: str, filename: str, text: str) -> Optional[dict]:
    boundary = "----office" + uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"uploaded_by\"\r\n\r\noperator-office\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: text/markdown\r\n\r\n").encode() + text.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode()
    h = {k: v for k, v in _headers(request).items() if k != "Content-Type"}
    h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(_base(request) + f"/api/plugins/kanban/tasks/{urllib.parse.quote(task_id)}/attachments?board={urllib.parse.quote(board)}",
                                 data=body, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8")).get("attachment")


def _lane_record(tid: str, run: str, board: str, gate_id: str, decision: str, note: str, policy: dict,
                 execution: Optional[dict], details: dict, request_id: str) -> str:
    lines = [f"# Lane record: {lanes.TEMPLATES[tid]['name']} ({tid}) run {run}", "",
             f"- board: {board}", f"- gate task: {gate_id}", f"- decision: {decision} (operator, Operator Office)",
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


def _apply_lane_gate(request: Request, body: "DecisionBody", t: dict, refuse) -> dict:
    note = (body.note or "").strip()
    if body.decision == "reject" and not note:
        refuse(400, "reject needs a note")  # cheap validation before any upstream read
    tid, run, details = _lane_gate_context(request, body.board, t)
    qb, qt = urllib.parse.quote(body.board), urllib.parse.quote(body.task_id)
    # WP-5 3a / WP-8: refuse with 409 if any reviewer's profile is in the author set of the steps it
    # reviews. This is the early, focused refusal. The same invariant is also enforced inside
    # ``lanes.gate_policy`` as defense in depth (its ``review.independent`` check will still fire).
    bind = lanes.lane_review_binding_ok(tid, details)
    if not bind["ok"]:
        reasons = "; ".join(p["detail"] for p in bind["problems"])
        refuse(409, "lane review binding not satisfied: " + reasons)
    if body.decision == "reject":
        if not note:
            refuse(400, "reject needs a note")
        policy = lanes.gate_policy(tid, details)
        rec = _lane_record(tid, run, body.board, body.task_id, "reject", note, policy, None, details, body.request_id)
        att = _upload_attachment(request, body.board, body.task_id, f"lane-record-{run}.md", rec)
        _upstream(request, "PATCH", f"/api/plugins/kanban/tasks/{qt}?board={qb}",
                  {"status": "blocked", "block_reason": f"REJECTED by operator via Operator Office: {note}"})
        return {"status": "blocked", "verdict": "rejected", "run": run, "record_attachment": (att or {}).get("id")}
    policy = lanes.gate_policy(tid, details)
    if not policy["ok"]:
        failed = [c for c in policy["checks"] if not c["ok"]]
        refuse(409, "lane policy not satisfied: " + "; ".join(f"{c['name']} ({c['detail']})" for c in failed))
    execution = lanes.execute_adapter(lanes.TEMPLATES[tid]["adapter"])
    rec = _lane_record(tid, run, body.board, body.task_id, "approve", note, policy, execution, details, body.request_id)
    att = _upload_attachment(request, body.board, body.task_id, f"lane-record-{run}.md", rec)
    with _io_lock:
        approved = _approved_gates()
        approved[body.task_id] = {"board": body.board, "run": run, "template": tid, "request_id": body.request_id,
                                  "at": time.time(), "execution": execution}
        _write("lane-approvals.json", approved)
    _upstream(request, "PATCH", f"/api/plugins/kanban/tasks/{qt}?board={qb}",
              {"status": "done", "summary": f"Approved by operator via Operator Office. Execution: {execution['status']} "
                                            f"({execution.get('reason', '')})"[:400],
               "metadata": {"approved_via": "operator-office", "request_id": body.request_id, "run": run,
                            "policy": policy["checks"], "execution": execution}})
    return {"status": "done", "verdict": "approved", "run": run, "policy": policy, "execution": execution,
            "record_attachment": (att or {}).get("id")}


# ══ Room definitions (rooms.json) ════════════════════════════════════════════
# Presentation + routing only. Validation keeps the mapping honest: residents must be installed profiles,
# a profile lives in one room, Client Work can never get residents, ids/sensitivity are not editable.

class RoomsBody(BaseModel):
    rooms: list[dict]
    confirm: bool = False


def validate_rooms(patches: list[dict], profiles: set[str]) -> tuple[list[dict], list[str]]:
    ids = {r["id"]: r for r in oc.DEFAULT_ROOMS}
    errs, clean, seen_res = [], [], {}
    for p in patches:
        rid = p.get("id")
        if rid not in ids:
            errs.append(f"unknown room id {rid!r}"); continue
        out = {"id": rid}
        if "name" in p:
            v = str(p["name"]).strip()
            if not (1 <= len(v) <= 40):
                errs.append(f"{rid}.name: 1..40 chars")
            out["name"] = v
        if "purpose" in p:
            out["purpose"] = str(p["purpose"]).strip()[:300]
        for k in ("tenant_prefixes", "keywords"):
            if k in p:
                v = p[k]
                if not isinstance(v, list) or len(v) > 40 or any(not isinstance(x, str) or not (1 <= len(x.strip()) <= 60) for x in v):
                    errs.append(f"{rid}.{k}: up to 40 strings of 1..60 chars")
                else:
                    out[k] = [x.strip().lower() for x in v]
        if "residents" in p:
            v = p["residents"]
            if ids[rid]["sensitivity"] == "client" and v:
                errs.append("client room cannot have residents (locked)")
            elif not isinstance(v, list) or any(x not in profiles for x in v):
                errs.append(f"{rid}.residents: only installed profiles {sorted(profiles)}")
            else:
                for x in v:
                    if x in seen_res:
                        errs.append(f"profile {x} is resident in both {seen_res[x]} and {rid}")
                    seen_res[x] = rid
                out["residents"] = list(dict.fromkeys(v))
        if "color" in p:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(p["color"])):
                errs.append(f"{rid}.color: #rrggbb")
            out["color"] = p["color"]
        clean.append(out)
    return clean, errs


@router.get("/office/rooms")
def office_rooms(request: Request):
    rooms, markers = _rooms_cfg()
    names = {p["name"] for p in _profiles(request)}
    placed = {x for r in rooms for x in r.get("residents", [])}
    return {"rooms": [{k: r.get(k) for k in ("id", "name", "glyph", "color", "sensitivity", "purpose", "residents",
                                              "tenant_prefixes", "keywords")} for r in rooms],
            "profiles": sorted(names), "unplaced_profiles": sorted(names - placed),
            "override": _read("rooms.json", None) is not None, "client_markers_count": len(markers)}


@router.put("/office/rooms")
def office_rooms_put(request: Request, body: RoomsBody):
    if not body.confirm:
        raise HTTPException(400, "explicit confirm=true required")
    names = {p["name"] for p in _profiles(request)}
    clean, errs = validate_rooms(body.rooms, names)
    if errs:
        raise HTTPException(400, {"room_errors": errs})
    with _io_lock:
        cur = _read("rooms.json", {}) or {}
        by_id = {r["id"]: r for r in cur.get("rooms", []) if isinstance(r, dict)}
        for c in clean:
            by_id.setdefault(c["id"], {"id": c["id"]}).update(c)
        merged = {**cur, "rooms": list(by_id.values())}
        # final check across the merged result (one room per profile)
        rooms, _ = oc.load_rooms(merged)
        owners: dict[str, str] = {}
        for r in rooms:
            for x in r.get("residents", []):
                if x in owners:
                    raise HTTPException(400, {"room_errors": [f"profile {x} is resident in both {owners[x]} and {r['id']}"]})
                owners[x] = r["id"]
        if (STATE_DIR / "rooms.json").is_file():
            (STATE_DIR / "backups").mkdir(parents=True, exist_ok=True)
            (STATE_DIR / "backups" / f"rooms.json.{time.strftime('%Y%m%dT%H%M%S')}").write_bytes((STATE_DIR / "rooms.json").read_bytes())
        _write("rooms.json", merged)
    _ledger.audit({"action": "rooms.update", "result": "applied", "rooms": [c["id"] for c in clean]})
    _cache.items.clear()
    return {"ok": True, "rooms": [c["id"] for c in clean]}
