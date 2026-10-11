# Upstream Diff: What This Plugin Is, What Is pixel-agents

This file answers one question plainly: **what is upstream pixel-agents, and what is custom code by Taras Polishchuk on top of it.** Valery asked for the diff; here it is.

## TL;DR

- **Upstream `pixel-agents` v1.4.1** (https://github.com/pixel-agents-hq/pixel-agents) is a **VS Code extension** + **standalone CLI**. It ships a webview UI (`webview-ui/`), a Node/TypeScript server, a VS Code adapter, and an AsyncAPI contract. The pixel-art office is one piece of it.
- **This plugin** (Operator Office) is a **Hermes dashboard plugin** that embeds **only the pixel-art webview** and adds a **custom Python backend** that is the actual mission-control logic. Everything else from upstream is dropped.

The pixel-art rendering is upstream. Every dashboard, lane, approval, mission, agent-team, incident, and event logic is custom. Read on for the per-file breakdown.

## What upstream pixel-agents actually is

```
pixel-agents/
├── webview-ui/        the pixel-art office (React + Vite + TypeScript)
│   └── src/office/    floor, walls, characters, pets, furniture, layout editor
├── core/              shared TypeScript types + AsyncAPI contract
├── server/            Node/TypeScript hook event server (standalone CLI backend)
├── adapters/vscode/   the VS Code extension glue (terminal hooks, panel, commands)
├── scripts/           build, e2e, asyncapi generation
├── e2e/               Playwright e2e tests
├── package.json       npm package
└── ...                esbuild, eslint, knip, vercel deploy, etc.
```

It is a full product: a VS Code extension on the Marketplace, plus a CLI (`npx pixel-agents`) that serves the same office for non-VS-Code workflows. The webview is the office UI; the server feeds it events; the VS Code adapter hooks the editor.

## What this plugin uses from upstream

**Only the webview UI**, prebuilt into `plugin/dist/app/`. The webview is the canvas that draws the rooms, the characters, and the animation. The plugin loads it inside an iframe and talks to it over `postMessage`.

The prebuilt webview was produced from the upstream `webview-ui/` directory at some pinned commit. The license text is in `plugin/dist/app/third-party-license.json`. The exact pinned commit SHA is not in this repo (it was recorded in the original build pipeline that produced `dist/app/`; the build pipeline itself is not in this repo).

The `prebuilt` choice is deliberate. It means:
- No Node toolchain needed in a Hermes install.
- The plugin is a single `cp -r` and is up.
- Upstream bumps require a manual rebuild (or a new `dist/app/` drop in this repo).

The full upstream webview source is not bundled here. To inspect it, clone upstream at the relevant commit and read `webview-ui/`.

## What is custom (Taras Polishchuk)

Everything outside the iframe.

### Backend (Python, FastAPI + pydantic, 4766 LOC across 7 files)

| File | LOC | What it does |
|---|---|---|
| `plugin/dashboard/plugin_api.py` | 1765 | The entire HTTP surface. 50+ endpoints. Origin guard. Proxies to Hermes core APIs. |
| `plugin/dashboard/office_control.py` | 609 | The `office.v1` contracts: rooms, presence, events, approvals, incidents, missions. The Mission Control logic. |
| `plugin/dashboard/pixel_office_activity.py` | 319 | Reads `<profile>/state.db` (mode=ro) to derive character activity. This is the bridge between Hermes session events and the pixel-agents `postMessage` protocol. |
| `plugin/dashboard/lanes.py` | 447 | Governed lanes. Independent QA reviewer lane, execution adapters disabled. |
| `plugin/dashboard/review_guard.py` | 391 | QA reviewer policy. Decides when a lane run needs human review. |
| `plugin/dashboard/approval_store.py` | 685 | Persisted approval queue. HMAC-bound to action + target + task fingerprint + expiry. Idempotent. |
| `plugin/dashboard/approval_kanban.py` | 550 | Kanban-bound approval surface. Forwards a decision to a Kanban task transition. |

None of these files exist in upstream. The upstream server is Node/TypeScript and lives at `pixel-agents/server/`. The plugin does not import or use any of it.

### Host shell (JavaScript, IIFE, ~600 LOC of `plugin/dist/index.js` + `plugin/dist/style.css`)

The host shell is a small React IIFE that:
- Mounts the side panel (Overview, Mission Control, Task, Approvals, Lanes, Incidents, Events, Missions, Agents, Teams, Rooms setup).
- Mounts the floor plan container with the iframe.
- Talks to the backend via `fetch`.
- Talks to the iframe over `postMessage`.

The host shell uses the Hermes dashboard plugin SDK's React. It is intentionally not a Vite app so an agent can `cat` it and reason about it. The upstream webview is a different React tree in a different frame; the two communicate only over `postMessage`.

This file is entirely custom. Upstream has no equivalent; the upstream side panel is VS Code's, not a custom one.

### Manifest and descriptor

`plugin/dashboard/manifest.json` declares the plugin to Hermes (name, version, tab path, entry, css, api, sdk_version). Upstream has no manifest; it is a VS Code extension, not a Hermes plugin.

### Presets (4 JSON files in `plugin/dashboard/presets/`)

- `hermes-hq-layout.json` — 44x32 layout with Gym, Game Room, Cafe, Open Space (16 desks), Server Room, Command, Meeting, Lounge, Library. 210 items, 51 seats, 2 pets. 28 procedurally-drawn furniture items in the Hermes pack (gym, games, kitchen, tech, decor). 9 rooms.
- `hermes-hq-seats.json` — seat-to-profile mapping.
- `operator-office-layout.json` — the default 9-room floor plan.
- `operator-office-seats.json` — seat mapping for the default layout.

The layouts are valid `webview-ui/src/office/types.ts` documents. They are **content** for the upstream webview, not custom code; the webview knows how to render any layout matching its types. The Hermes HQ layout is custom content; the rendering engine that draws it is upstream.

### Documentation in this repo

Everything under `docs/` is custom and was written for this repo:
- `AGENT-REPRODUCE.md` — deployment walkthrough.
- `ARCHITECTURE.md` — how the pieces talk.
- `MISSION-CONTROL.md` — the Mission Control view in plain words.
- `DATA-AND-STATE.md` — where data lives, who owns it.
- `API-SURFACE.md` — the endpoint map.
- `UPSTREAM.md` — pixel-agents pinned version, how to bump.
- `CLIENT-MARKERS.md` — how the office withholds client tasks.
- `UPSTREAM-DIFF.md` (this file) — what is upstream, what is custom.

## Side-by-side table

| Concern | Upstream pixel-agents | This plugin (Operator Office) |
|---|---|---|
| Editor integration | VS Code extension + standalone CLI | Hermes dashboard plugin |
| Backend language | TypeScript (Node) | Python (FastAPI) |
| Backend runtime | Node server (`pixel-agents/server/`) | Hermes dashboard process (FastAPI router) |
| Renderer | `webview-ui/` (React + Vite) | prebuilt from upstream webview-ui, embedded as iframe |
| Floor plan / characters / pets | upstream | upstream (renderer) + custom content (Hermes HQ layout) |
| Layout editor | upstream (in webview) | upstream (in webview) + 4 JSON presets |
| Activity tracking | terminal hooks via AsyncAPI | reads `<profile>/state.db` directly, mode=ro |
| Mission control | not present | custom (Python, `office_control.py`) |
| Governed lanes | not present | custom (Python, `lanes.py` + `review_guard.py`) |
| Approvals queue | not present | custom (Python, `approval_store.py` + `approval_kanban.py`) |
| Audit log | not present | custom (`~/.hermes/state/pixel-office/office-audit.jsonl`) |
| Client-marker filter | not present | custom (`office_control.py`) |
| State persistence | not present (upstream is stateless across sessions) | custom (`~/.hermes/state/pixel-office/*.json`) |
| Origin guard | not present (VS Code has its own auth) | custom (`plugin_api.py:_origin_guard`) |
| Multi-tenant board projection | not present | custom (board filter, room-area mapping) |
| Independent QA reviewer | not present | custom (`review_guard.py`) |
| Fonts | upstream uses system fonts | Google Fonts CDN (IBM Plex Sans/Mono, JetBrains Mono) |
| License | MIT (Pablo De Lucca, 2026) | MIT (Taras Polishchuk, 2026) for the plugin code; MIT for the upstream renderer |

## How to verify the diff yourself

1. Clone upstream:
   ```bash
   git clone --depth=1 https://github.com/pixel-agents-hq/pixel-agents.git
   ```
2. Look at `webview-ui/` — that is what is rendered inside the iframe in this plugin. Everything under `webview-ui/` is the **renderer**; the rest of upstream (`core/`, `server/`, `adapters/vscode/`, `scripts/`, `e2e/`) is **not used** by this plugin.
3. Open `plugin/dashboard/plugin_api.py` in this repo. Every line is custom. There is no `import` from upstream; no `require()`; no copy of the upstream server. The plugin talks to the upstream webview only over `postMessage` (defined in `pixel_office_activity.py`).
4. Open `plugin/dashboard/office_control.py`. This is the Mission Control logic. There is no upstream equivalent.
5. Open `plugin/dashboard/lanes.py`, `review_guard.py`, `approval_store.py`, `approval_kanban.py`. None of this exists upstream.

## What the iframe sees vs what the host shell sees

The iframe is the upstream webview. It knows how to:
- Draw tiles, walls, floors, carpets, furniture, characters, pets.
- Animate characters based on `postMessage` events.
- Edit the layout (drag, place, delete).
- Apply layout JSON.

The iframe does **not** know how to:
- Talk to Hermes core APIs.
- Read or write plugin-owned state.
- Read `<profile>/state.db`.
- Talk to Kanban.
- Talk to Workspace OS.
- Filter client markers.
- Render the side panel (Overview, Mission Control, Task, Approvals, Lanes, Incidents, Events, Missions, Agents, Teams, Rooms setup).
- Render the approvals queue.
- Render missions.
- Render incidents.

All of those are custom in the host shell + Python backend. The iframe is the dumb renderer; the host shell and Python backend are the brains.

## What you would need to do to remove upstream entirely

You would need to write a new renderer that:
- Implements the `postMessage` protocol documented in `plugin/dashboard/pixel_office_activity.py`.
- Renders the same kind of floor plan (tiles, walls, characters, pets, furniture).
- Supports the layout JSON format defined by `plugin/dashboard/presets/*.json`.

Until you do, the prebuilt `dist/app/` is the renderer. Replacing it is a real engineering project, not a one-file change.

## Summary

- **Upstream** = the pixel-art office renderer. MIT. Pinned. Prebuilt. Embedded as iframe.
- **Custom** = every dashboard, every mission, every approval, every lane, every audit, every projection, every state file, every endpoint, the host shell, the manifest, all 4 presets (content), and all docs.

If a future contributor wants to upstream anything back, the candidates are: the Hermes HQ layout (community furniture catalog contribution), the operator-friendly room mapping, the `Display Headless as Ghosts`-style setting for the operator (agents without a session as ghosts). The Python backend, the approval queue, the lane policy, and the audit log are Hermes-specific and do not belong upstream.
