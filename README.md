# Operator Office Plugin (Hermes)

Pixel-art Operator Office for the **Hermes Agent** dashboard. Adds a `/pixel-office` tab with 9 themed rooms, mission control, governed lanes, task approvals, live event stream, missions, agents, teams, and incidents.

This repo is a **code-and-docs drop** for an agent to read and reproduce. It is not an installer. No one-command install script is shipped on purpose: an agent should walk through the code, understand it, and adapt it to the target Hermes install.

## What's here

```
plugin/                                  the deployed plugin code, verbatim from a working Hermes install
  README.md                              upstream-developer README
  manifest.json                          plugin descriptor (name, version, tab path, entry, css, api, sdk_version)
  plugin_api.py                          FastAPI router -> /api/plugins/pixel-office/* + /office/*
  office_control.py                      office.v1 contracts: rooms, presence, events, approvals, incidents, missions
  pixel_office_activity.py               session -> character state
  lanes.py                               governed lanes
  review_guard.py                        independent QA review
  approval_store.py                      persisted approval queue
  approval_kanban.py                     Kanban-bound approvals
  dist/index.js, dist/style.css          host shell (IIFE, dashboard React via SDK)
  dist/app/                              built pixel-agents webview (iframe, see third-party-license.json)
  presets/                               4 layout/seats presets: hermes-hq, operator-office (default 9-room)

docs/
  ARCHITECTURE.md                        what runs where, how pieces talk
  MISSION-CONTROL.md                     the Mission Control view in plain words
  DATA-AND-STATE.md                      where data lives, who owns it, who reads/writes
  API-SURFACE.md                         full API endpoint reference (from plugin_api.py decorators)
  AGENT-REPRODUCE.md                     step-by-step for an agent that wants to deploy this in another Hermes
  UPSTREAM.md                            pixel-agents pinned version, how to bump
  CLIENT-MARKERS.md                      how the office withholds client tasks/events/missions
```

## What it is NOT

- Not a pip / npm package. Plugins are loaded by Hermes from `~/.hermes/plugins/<name>/`.
- Not a CLI tool. It only runs inside a running Hermes dashboard.
- Not a fork of pixel-agents. pixel-agents is the renderer; everything else is custom code that lives on top.

## Upstream

The only external repository pulled in is the pixel-art renderer:

- **pixel-agents** — `https://github.com/pixel-agents-hq/pixel-agents` (MIT, Pablo De Lucca, 2026). Pre-built assets live in `plugin/dist/app/`. License text: `plugin/dist/app/third-party-license.json`.

Fonts loaded at runtime from Google Fonts CDN (IBM Plex Sans, IBM Plex Mono, JetBrains Mono). No other external repos, CDNs, PyPI or npm packages are required.

## Requirements (target environment)

- Hermes Agent v0.21+ (this plugin uses dashboard plugin SDK 1.1.0).
- Kanban plugin enabled.
- Python 3.10+ with `fastapi` and `pydantic` available in Hermes venv.
- Modern browser (the dashboard tab renders a pixel-art canvas in an iframe).

What it does **not** require:

- Node, npm, vite. The build artifacts are prebuilt and shipped in `plugin/dist/`.
- Internet access at runtime (except for the Google Fonts CDN call).

## Quick orientation for an agent

Start here in this order:

1. `docs/AGENT-REPRODUCE.md` — the deployment walkthrough.
2. `plugin/manifest.json` — what the plugin declares about itself.
3. `plugin/plugin_api.py` — the entire HTTP surface, in one file.
4. `plugin/office_control.py` — the office.v1 contracts and Mission Control logic.
5. `docs/API-SURFACE.md` — the endpoint map.
6. `docs/DATA-AND-STATE.md` — ownership and persistence.

## What an agent must do to deploy this

Minimum steps are documented in `docs/AGENT-REPRODUCE.md`. In short:

1. Drop `plugin/` into the target Hermes install at `~/.hermes/plugins/pixel-office/`.
2. Make sure the dashboard picks it up (Hermes scans `~/.hermes/plugins/` on start).
3. Create the plugin-owned state directory `~/.hermes/state/pixel-office/` (it will be created on first write, but pre-creating with mode 0700 is fine).
4. From the Hermes UI, open `/pixel-office`. The tab appears after Workspace Office.

No restart required for a freshly-extracted plugin if Hermes auto-reloads; otherwise a dashboard restart brings it up.

## Versioning

- Plugin version: `1.4.1` (in `plugin/manifest.json`).
- Hermes SDK: `1.1.0`.
- pixel-agents upstream: pinned via `plugin/dist/app/third-party-license.json` (see `docs/UPSTREAM.md`).

## License

Plugin code: MIT. See `LICENSE`.
Pixel-agents renderer: MIT, see `plugin/dist/app/third-party-license.json`.
