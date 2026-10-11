# Hermes Pixel Office

Pixel-art office tab for the Hermes dashboard. Built on
[pixel-agents](https://github.com/pixel-agents-hq/pixel-agents) (MIT, pinned in `UPSTREAM.json`).

Each Hermes profile is a character. Live sessions drive the animation:
typing on write/edit/terminal, reading on read/search/web, speech bubbles on
"turn done" / "waiting for you" / "needs you" (pending `clarify`).
`delegate_task` children and extra concurrent sessions appear as sub-agent
characters next to their owner.

## What it adds

- Tab `/pixel-office` (plugin `pixel-office`, after Workspace Office).
- `+ Agent`: creates a real Hermes profile (`POST /api/profiles`, optional clone source, SOUL, team).
- Teams: name, color, lead, members. Members sit together (map an Area to the team in Layout).
  "Team task" = Kanban task to the lead with the roster attached.
- Inspector: status, current tools, sub-agents, open Kanban tasks, recent sessions, SOUL. "Give task" = Kanban task.
- Full pixel-agents layout editor; layout, seats, areas persist.

Orchestration is untouched: tasks go through the normal Kanban API and dispatcher.

## Data

| What | Where | Access |
|---|---|---|
| agents | Hermes profiles | `/api/profiles` |
| activity | `<profile>/state.db` sessions+messages | read-only (`mode=ro`) |
| work | Kanban | `/api/plugins/kanban/*` |
| missions | Workspace OS `.project-state/<slug>/` | read-only, 8 artifact names |
| decisions audit + idempotency | `~/.hermes/state/pixel-office/{office-audit.jsonl,decisions.json}` | plugin-owned |
| presentation (layout, seats, teams, ids, hidden, settings) | `~/.hermes/state/pixel-office/*.json` | plugin-owned |

## Layout

```
plugin/dashboard/            deployed as ~/.hermes/plugins/pixel-office/dashboard/
  manifest.json
  plugin_api.py              FastAPI router  -> /api/plugins/pixel-office/*  (incl. /office/*)
  office_control.py          office.v1 contracts: rooms, presence, events, approvals, incidents, missions
  pixel_office_activity.py   session -> character state
  dist/index.js, style.css   host tab (side panel, dialogs)
  dist/app/                  built pixel-agents webview (iframe)
overlay/                     files dropped into the upstream checkout (Hermes transport)
scripts/patch_upstream.py    exact-match patches (fail loud on upstream drift)
scripts/decode-assets.ts     PNG -> decoded sprite JSON at build time
scripts/build.sh             clone pinned upstream, patch, vite build -> dist/app
scripts/deploy.sh            install, enable, restart dashboard, verify
tests/                       pytest for activity derivation
```

## Commands

```
scripts/build.sh            # only needed after changing overlay/ or bumping UPSTREAM.json
scripts/deploy.sh           # install + enable + restart + verify
scripts/deploy.sh --verify  # health check only
python3 -m pytest tests -q
```

## Operator Agent Office (v1.2, office.v1)

Control-plane layer in the same tab (label "Operator Office"). Hermes Kanban stays authoritative; the plugin
reads, renders, and forwards a narrow set of operator decisions. Docs: `docs/operator-office/`
(`ADR-OAO-001.md`, `ARCHITECTURE.md`, `CONTRACTS.md`).

- Explicit board selection (persisted). Nothing renders until a board is picked.
- Floor plan: 9 domain rooms (Command Center, Client Work [locked], Product Lab, Research Lab, Content Factory,
  Communications, Career Desk, Knowledge Vault, Ops & Approvals). Residents = profiles, tasks routed by tenant/keyword/assignee.
- Room board, task inspector (timeline, runs, worker log tail, comments, artifacts: workspace files, preserved
  attachments, linked missions), Workspace OS mission viewer (8 artifacts, read-only).
- Approvals: Hermes-native human gates only (`blocked/needs_input`, `review` on a human lane), HMAC-bound to
  action + target + task fingerprint + expiry, explicit confirm, idempotent, audited (`state/pixel-office/office-audit.jsonl`).
- Live: Kanban WS with cursor replay after reconnect, snapshot reconciliation, LIVE/RECONNECTING/OFFLINE pill, last-good snapshot banner.
- Client markers (`insidedynamic`, `idstar`, `founderos`, `founder-os`, `fos-neu`) withhold tasks, events and missions (projection filter, not isolation).
- Keys: `1-9` rooms, arrows on the floor plan, `/` search, `Esc` close.

```
python3 -m pytest tests -q                                   # unit (contracts, redaction, routing, binding, traversal)
~/.hermes/hermes-agent/venv/bin/python3 tests/e2e_office_live.py   # 28 live checks, mutates only board operator-office-lab
```

## Hermes HQ layout + furniture pack

Research (2026-10-09): the community gallery Pixel Index (pixel-index.nntin.xyz) has 4 layouts,
all 21-25 tiles wide, built only from the 25 bundled pieces. No gym, no game room. So the
pack and the layout are made here:

- `art/pack.py`: 28 procedurally drawn items in the bundled art style. Gym (treadmill, bike,
  bench press, dumbbell rack, punching bag, kettlebells, yoga mat, mirror), games (pool,
  ping pong, foosball, arcade, basketball hoop), kitchen (counter, fridge, vending, espresso,
  microwave, bar stool), tech (server rack, wall dashboard), decor (aquarium, beanbags,
  neon sign, trophy shelf, water cooler). Animated ones loop all the time (patch 6).
- `art/layout.py`: "Hermes HQ", 44x32, 9 rooms: Gym, Game Room, Cafe, Open Space
  (16 desks), Server Room, Command (orchestrator office), Meeting, Lounge, Library.
  210 items, 51 seats, rugs, areas, 2 pets. The script validates overlaps, wall and surface
  placement, and reachability of every room and seat.
- Shipped as the bundled default layout and as preset `hermes-hq`
  (`POST /api/plugins/pixel-office/layout/preset/hermes-hq`, button "HQ layout").
  The preset backs up the current layout, puts orchestrator in Command and research in
  the Library, and fills the desk pods with the rest.
- Patch 7 caches the static floor layer in view mode. Big offices otherwise redraw ~1400
  tiles per frame.

## Hermes updates

The plugin lives in `~/.hermes/plugins/`, outside the `hermes-agent` checkout, and ships
prebuilt assets (no node at runtime). `hermes update` does not touch it. The only
dependencies on Hermes are public dashboard APIs (`/api/profiles`, Kanban plugin API,
plugin SDK 1.1.0) and the session store schema (`sessions`, `messages`). If an update
breaks one of them, `scripts/deploy.sh --verify` shows where.

## Upstream bump

Edit `UPSTREAM.json` commit, run `scripts/build.sh`. A patch that no longer matches stops the build.
