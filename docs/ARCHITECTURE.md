# Architecture

## What the plugin is

A **dashboard tab plugin** for Hermes Agent. It adds a side tab labeled "Operator Office" that opens a control plane at `/pixel-office`. The tab uses the Hermes dashboard plugin SDK to mount into the existing shell (it is not a separate web app).

The tab is composed of two pieces glued together:

1. **Host shell** (`plugin/dist/index.js` + `plugin/dist/style.css`) — a small React IIFE that renders the side panel, the dialogs, the floor plan container, and talks to the backend via `fetch`.
2. **Pixel-art renderer** (`plugin/dist/app/`) — a Vite-built React app that draws the rooms, characters, and animations inside an iframe. It is the `pixel-agents` upstream, prebuilt and bundled.

The host shell and the renderer are two different React trees in two different frames. They communicate over `postMessage`.

## Where the data comes from

The plugin does not own most of the data it shows. It reads from Hermes core APIs and projects them into the office view.

| Source of truth | What the office reads | API path |
|---|---|---|
| Hermes profiles | agents / characters | `/api/profiles` |
| Hermes Kanban | tasks, boards, lanes, workers | `/api/plugins/kanban/*` |
| Profile session store | typing/reading activity for character animation | `<profile>/state.db` (read-only) |
| Workspace OS | missions and their 8 artifacts | `.project-state/<slug>/` (read-only) |
| Plugin-owned JSON | layout, seats, teams, area mappings, ids, hidden, settings, decisions | `~/.hermes/state/pixel-office/*.json` |
| Plugin-owned JSONL | audit trail for approvals | `~/.hermes/state/pixel-office/office-audit.jsonl` |

Hermes Kanban is authoritative for tasks. The office never mutates tasks directly; it only forwards a narrow set of operator decisions to Kanban-bound approval endpoints.

## How a request flows

A user opens the office tab:

1. Dashboard mounts `plugin/dist/index.js` via the plugin SDK.
2. The host shell issues `GET /api/plugins/pixel-office/bootstrap` to the plugin router.
3. `plugin_api.py` calls `office_control.py` to assemble the bootstrap payload (rooms, agents, teams, boards, settings).
4. The host shell renders the side panel and the floor plan container.
5. The iframe loads `plugin/dist/app/index.html` (Vite-built pixel-agents).
6. The host shell and the iframe exchange `postMessage` for character state and live events.
7. As Kanban workers come and go, the office polls `/api/plugins/kanban/workers/active` and projects workers into characters.

For a write (e.g. assigning a profile to a seat, or applying a layout preset):

1. Host shell issues `PUT /api/plugins/pixel-office/<resource>`.
2. `plugin_api.py` runs the origin guard. Non-loopback writes are rejected.
3. The handler validates input, calls into the relevant module, persists to `~/.hermes/state/pixel-office/<file>.json`.
4. The handler returns the updated resource; the host shell re-renders.

## Why the split into 7 Python files

The seven Python files are not arbitrary. They are split by concern and so that an agent can read them one at a time:

- `plugin_api.py` — the only file the dashboard talks to. The HTTP surface. Pure routing, validation, and call-through. Read this first.
- `office_control.py` — the office.v1 contracts (rooms, presence, events, approvals, incidents, missions). Pure functions and small classes. No FastAPI imports, so it unit-tests.
- `lanes.py` — governed lanes (independent QA reviewer lane, execution adapters disabled). The point of Mission Control.
- `review_guard.py` — the QA reviewer policy: when a lane run needs human review, this decides.
- `approval_store.py` — persisted approval queue with HMAC binding to action + target + task fingerprint + expiry.
- `approval_kanban.py` — Kanban-bound approval surface. Takes a kanban task and surfaces it as a decision.
- `pixel_office_activity.py` — the only file that touches session state (`<profile>/state.db`). Read-only, mode=ro.

## Why the host shell is IIFE and not built

The host shell is a single 200-ish-line `dist/index.js` that uses the dashboard's React via the SDK. It is intentionally not a Vite app:

- No build step means an agent can `cat` it and reason about it.
- Any change is a one-file edit.
- It loads only the SDK hooks it needs (`useState`, `useEffect`, `useCallback`, `useMemo`, `useRef`) and the SDK's `fetchJSON`.

If the renderer (`dist/app/`) ever changes, that is a real build (Vite + the upstream pixel-agents + the overlay patches). See `docs/UPSTREAM.md`.

## Defense in depth

- **Origin guard** (`plugin_api.py:_origin_guard`): only loopback origins may mutate. GET/HEAD/OPTIONS are exempt. The dashboard's session token is checked by Hermes core before the handler runs.
- **No money moves from the office.** Decisions are advisory or routing-only. Money paths go through the normal Kanban API and require their own approvals.
- **Audit log**: every approval decision lands in `office-audit.jsonl` with the HMAC binding and the decision outcome. Idempotency key is the binding fingerprint.
- **Client markers** withhold tasks/events/missions for known client slugs from every view. This is a projection filter, not isolation — the data still exists in Kanban / Workspace OS.

## File map (plugin/)

```
manifest.json              plugin descriptor (version, tab path, entry, css, api, sdk_version)
plugin_api.py              FastAPI router; entire HTTP surface
office_control.py          office.v1 contracts; pure logic
pixel_office_activity.py   session -> character state
lanes.py                   governed lanes
review_guard.py            QA reviewer policy
approval_store.py          persisted approval queue
approval_kanban.py         kanban-bound approvals
dist/index.js              host shell (IIFE, dashboard React via SDK)
dist/style.css             host shell styles
dist/app/                  built pixel-agents webview (iframe)
presets/                   4 layout/seats JSON presets
```
