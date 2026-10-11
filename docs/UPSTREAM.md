# Upstream: pixel-agents

The only external repository this plugin depends on is the pixel-art renderer that draws the rooms and characters.

- **Repo**: `https://github.com/pixel-agents-hq/pixel-agents`
- **License**: MIT
- **Author**: Pablo De Lucca, 2026
- **License text in repo**: `plugin/dist/app/third-party-license.json`

## What we use from upstream

Everything that draws pixels. That is:

- The Vite + React + TypeScript app shell.
- The canvas renderer for tiles, walls, floors, carpets, characters, pets, furniture.
- The animation loop (typing, reading, speech bubbles).
- The `postMessage` protocol between the iframe and the host shell.
- The default character sprites (6 variants).
- The default pet sprites (claudio, gitcat).
- The 25 bundled furniture items in the upstream furniture catalog.
- The default floor, wall, and carpet tile sets.

## What we add on top (the Hermes-specific layer)

Everything that is not pixels. That is:

- The Hermes-side host shell that talks to the dashboard SDK.
- The `postMessage` adapter that turns upstream's character events into Hermes session reads.
- The 28 procedurally-drawn furniture items in the Hermes pack (gym, games, kitchen, tech, decor) — see `plugin/dist/app/assets/furniture/`.
- The Hermes HQ layout (44x32, 9 rooms, 210 items, 51 seats, 2 pets).
- The Operator Office layout (the default 9-room floor plan).
- The Mission Control projection.
- The approvals, lanes, incidents, audit, decisions.
- The Office `office.v1` contracts.

## How the pinned version is recorded

`plugin/dist/app/third-party-license.json` carries the upstream URL and license text. The actual build is **not** reproducible from this repo alone — the prebuilt artifacts in `plugin/dist/app/` were built against a specific upstream SHA. The SHA is not in the repo (the build pipeline records it, but it is not checked in here because we have no build pipeline to verify against).

## How to bump the upstream version

1. Clone upstream at the desired SHA into a scratch directory.
2. Apply the patches in `overlay/` (if this repo had an `overlay/` directory — it does not, because the patches were applied at build time and the result is what ships in `dist/app/`). To restore that workflow, fork upstream and apply the Hermes transport patches into a new branch.
3. Run `npm install && npm run build` in the upstream checkout, output to `dist/`.
4. Copy the build output into `plugin/dist/app/`.
5. Verify the new build with `GET /api/plugins/pixel-office/health` and a manual visit to `/pixel-office`.
6. Update `plugin/dist/app/third-party-license.json` if the license text changed.

## What is NOT in this repo

- The upstream source tree (clone it from GitHub if you need to bump).
- The `overlay/` patches (they were applied at build time; the result is the prebuilt `dist/app/`).
- The `scripts/build.sh`, `scripts/deploy.sh`, `scripts/decode-assets.ts`, `scripts/patch_upstream.py` build scripts (they existed in the original repo; this drop ships the prebuilt artifacts only).

If you need to rebuild from scratch, see `docs/AGENT-REPRODUCE.md` for the deployment walkthrough, then either accept the prebuilt artifacts or set up a build pipeline of your own. The prebuilt artifacts are what Hermes loads; if they work, no rebuild is required.

## Why prebuilt

The plugin is deployed into a Hermes install. Hermes does not include a Node toolchain. Shipping prebuilt artifacts keeps the install dependency-free: a single `cp -r` and the plugin is up. The trade-off is that upstream bumps require either a manual rebuild or waiting for an updated `dist/app/` drop in this repo.
