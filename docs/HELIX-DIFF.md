# Hermes Diff vs Upstream pixel-agents

This file is the actual `diff` between what is in this repo and what is in
upstream `pixel-agents` v1.4.1 (`https://github.com/pixel-agents-hq/pixel-agents`).
It is intentionally concrete: file names, sizes, and counts. No hand-waving.

This file answers Valery's question: **"what did you actually add on top of upstream?"**

The comparison was done by cloning upstream at HEAD `d1e007a` (v1.4.1, 2026) and
diffing against `plugin/` in this repo. Upstream lives in `/tmp/pixel-agents-upstream`;
this repo's `plugin/` is what an agent deploys.

## What is upstream (unchanged, used as-is)

Upstream ships a single source tree: a VS Code extension with a Node/TypeScript server,
a React/Vite webview, AsyncAPI contract, and a CLI wrapper. It has 381 source files
totalling 5.5 MB (excluding `node_modules`).

The webview UI is the part that draws the office. Upstream source for it is in
`webview-ui/`. The compiled output would land in `webview-ui/dist/`. We do not ship
that compiled output; we ship **our own compiled output** (prebuilt once at a pinned
upstream SHA) inside `plugin/dashboard/dist/app/`.

The renderer contract (what the iframe expects) is upstream. The Hermes plugin does
not change the contract. It just embeds the prebuilt renderer and uses the
`postMessage` protocol defined in `webview-ui/src/transport/`.

## What Hermes added (this is the diff)

### 1. 28 new furniture sprites (procedurally drawn)

Upstream `webview-ui/public/assets/furniture/` has 25 items. Hermes added **28 more**,
all procedurally drawn by `art/pack.py` in the original build pipeline:

**Gym (8):**
- BENCH_PRESS
- DUMBBELL_RACK
- EXERCISE_BIKE
- GYM_MIRROR
- KETTLEBELLS
- PUNCHING_BAG (6 frames)
- TREADMILL (3 frames)
- YOGA_MAT

**Games (5):**
- ARCADE (3 frames)
- BASKETBALL_HOOP
- FOOSBALL
- PING_PONG
- POOL_TABLE

**Kitchen (6):**
- BAR_STOOL
- ESPRESSO (2 frames)
- FRIDGE
- KITCHEN_COUNTER
- MICROWAVE
- VENDING_MACHINE (2 frames)

**Tech (3):**
- BIG_TV (3 frames)
- NEON_SIGN (4 frames)
- SERVER_RACK (4 frames)

**Decor (6):**
- AQUARIUM (6 frames)
- BEANBAG_ORANGE / BEANBAG_PURPLE / BEANBAG_TEAL
- TROPHY_SHELF
- WATER_COOLER (3 frames)

Each has a `manifest.json` next to the PNGs, matching the upstream convention.

### 2. 2 new layout presets (custom content for the upstream renderer)

Upstream ships one default layout (`default-layout-1.json`, 14 425 bytes). Hermes
ships three:

- `default-layout-1.json` — 14 425 bytes — **identical** to upstream.
- `default-layout-2.json` — 119 790 bytes — new.
- `default-layout.json` — 99 643 bytes — new.

Plus 4 named presets in `plugin/dashboard/presets/`:

- `hermes-hq-layout.json` — 14 425 bytes (note: a Hermes pack layout) — the 44x32,
  9-room Hermes HQ with Gym, Game Room, Cafe, Open Space (16 desks), Server Room,
  Command, Meeting, Lounge, Library. 210 items, 51 seats, 2 pets.
- `hermes-hq-seats.json` — seat-to-profile mapping for Hermes HQ.
- `operator-office-layout.json` — 111 674 bytes — the default 9-room Operator Office
  floor plan.
- `operator-office-seats.json` — seat mapping for Operator Office.

These are pure **content** for the upstream renderer. The upstream layout editor
knows how to render and edit any layout matching its type. Hermes just ships more
content.

### 3. `decoded/*.json` (6 files, new)

Upstream does not ship decoded sprite JSON. It encodes sprites at build time into
runtime cache. Hermes added a separate `decoded/` directory with pre-decoded JSON
for:

- `carpets.json`
- `characters.json`
- `floors.json`
- `furniture.json`
- `pets.json`
- `walls.json`

This is a Hermes-specific build artifact produced by `scripts/decode-assets.ts`
in the original build pipeline. Upstream does not have an equivalent; the upstream
renderer decodes sprites on demand.

### 4. `asset-index.json` (new)

A Hermes-side index of all assets, used by the host shell to know which assets are
available without scanning the directory tree at runtime. Upstream does not have
this; upstream uses a hard-coded asset list in `webview-ui/src/office/sprites/`.

### 5. `furniture-catalog.json` (replaces upstream `furnitureCatalog.ts`)

Upstream has `furnitureCatalog.ts` (TypeScript source). Hermes compiles the
expanded catalog to JSON at build time. Same data, different format, expanded to
include the 28 new items above.

### 6. `plugin/dashboard/dist/app/fonts/FSPixelSansUnicode-Regular.ttf` (new)

Upstream uses system fonts for the office UI. Hermes added a custom pixel font
(`FSPixelSansUnicode`, OFL licensed) for in-office typography. The font lives at
`dist/app/fonts/` and is referenced by the prebuilt webview CSS.

### 7. `plugin/dashboard/` — the entire Hermes plugin backend (new)

This is the big one. Upstream has no equivalent. The directory contains:

| File | LOC | Notes |
|---|---|---|
| `manifest.json` | 20 | Hermes plugin descriptor. Upstream has no manifest. |
| `plugin_api.py` | 1765 | FastAPI router; entire HTTP surface. 50+ endpoints. |
| `office_control.py` | 609 | The `office.v1` contracts: rooms, presence, events, approvals, incidents, missions. Mission Control. |
| `pixel_office_activity.py` | 319 | Reads `<profile>/state.db` (mode=ro) to derive character activity. Bridge to the upstream `postMessage` protocol. |
| `lanes.py` | 447 | Governed lanes. Independent QA reviewer lane, execution adapters disabled. |
| `review_guard.py` | 391 | QA reviewer policy. |
| `approval_store.py` | 685 | Persisted approval queue. HMAC-bound, idempotent, audited. |
| `approval_kanban.py` | 550 | Kanban-bound approvals. |
| `dist/index.js` | 1281 | Host shell (IIFE, Hermes dashboard React via SDK). Not in upstream. |
| `dist/style.css` | ~5 KB | Host shell styles. Not in upstream. |
| `dist/app/` | 4.0 MB | Prebuilt webview (iframe). Renderer is upstream; the build that produced it is the Hermes build pipeline. |
| `presets/*.json` | 4 files | 4 named layout/seats presets (content for the upstream renderer). |

Total: 4766 lines of Python plus 1281 lines of JavaScript (host shell) plus 4
preset JSON files plus a manifest plus 4.0 MB of prebuilt webview. None of this
exists in upstream.

### 8. `docs/` — the entire documentation tree (new)

8 markdown files, ~43 KB total, all custom:

- `README.md` — top-level repo readme.
- `AGENT-REPRODUCE.md` — deployment walkthrough for an agent.
- `ARCHITECTURE.md` — how the pieces talk.
- `MISSION-CONTROL.md` — Mission Control in plain words.
- `DATA-AND-STATE.md` — where data lives, who owns it.
- `API-SURFACE.md` — endpoint map.
- `UPSTREAM.md` — pixel-agents pinned, how to bump.
- `UPSTREAM-DIFF.md` — this file's sibling; the conceptual diff.
- `CLIENT-MARKERS.md` — client task filter.
- `HELIX-DIFF.md` — this file; the concrete diff.

None of these are upstream concepts. They describe the Hermes layer.

## Summary table

| Concern | Upstream | Hermes plugin | Notes |
|---|---|---|---|
| Renderer source | `webview-ui/` (TypeScript + React + Vite) | embedded as prebuilt `dist/app/` | prebuilt once at a pinned upstream SHA |
| Renderer build output | `webview-ui/dist/` | `plugin/dashboard/dist/app/` | Hermes output is what is shipped |
| Furniture sprites | 25 PNG | 53 PNG (25 + 28 new) | 28 procedurally drawn |
| Default layouts | 1 (`default-layout-1.json`) | 3 (one identical, two new) | content for the upstream renderer |
| Named presets | 0 | 4 | content for the upstream renderer |
| Decoded sprite JSON | none (decoded at runtime) | 6 files in `decoded/` | Hermes pre-decodes at build time |
| Custom font | none | `FSPixelSansUnicode-Regular.ttf` (OFL) | Hermes-specific |
| Backend | Node/TypeScript (`server/`) | Python (FastAPI, 4766 LOC across 7 files) | entirely different language, entirely different runtime |
| Side panel UI | VS Code panel | custom React IIFE in `dist/index.js` | entirely different |
| State persistence | not present | 9 JSON files in `~/.hermes/state/pixel-office/` | new |
| Approvals queue | not present | 1235 LOC across `approval_store.py` + `approval_kanban.py` | new |
| Governed lanes | not present | 838 LOC across `lanes.py` + `review_guard.py` | new |
| Mission Control | not present | `office_control.py` (609 LOC) + side panel | new |
| Audit log | not present | `office-audit.jsonl` (append-only) | new |
| Client-marker filter | not present | `office_control.py` | new |
| Origin guard | not present (VS Code has its own auth) | `plugin_api.py:_origin_guard` | new |
| Plugin manifest | not present (VS Code extension) | `manifest.json` | new |
| Documentation | 1 README, 1 CHANGELOG | 10 markdown files, 43 KB | new |

## How to reproduce this diff

```bash
# Clone upstream
git clone --depth=1 https://github.com/pixel-agents-hq/pixel-agents.git /tmp/upstream
cd /tmp/upstream
# Look at what is shipped:
ls webview-ui/public/assets/furniture/         # 25 folders
ls webview-ui/public/assets/default-layout-*.json   # 1 file
ls server/src/                                 # backend in TypeScript
ls adapters/vscode/                            # VS Code extension glue
ls scripts/                                    # build pipeline
```

```bash
# Clone this repo
git clone https://github.com/taras-polishchuk/operator-office-plugin.git /tmp/hermes
cd /tmp/hermes
# Compare furniture count
ls plugin/dashboard/dist/app/assets/furniture/ | wc -l   # 53
# Compare layouts
ls plugin/dashboard/dist/app/assets/default-layout*.json   # 3 files
ls plugin/dashboard/presets/                              # 4 presets
# Compare backend
wc -l plugin/dashboard/*.py                              # 4766 lines
ls plugin/dashboard/                                      # 7 Python + manifest + dist
```

The numbers above are reproducible from these commands. No magic.

## What was NOT changed

- The pixel-art renderer source (we ship it prebuilt; we do not edit it).
- The AsyncAPI contract (it lives upstream at `core/asyncapi.yaml`; Hermes does not
  use it because Hermes is not a hook-event producer).
- The upstream CLI (`pixel-agents/server/cli.ts`). Hermes has its own dispatch
  via the Kanban API; the CLI is irrelevant.
- The upstream VS Code extension entry point. Hermes has no VS Code integration.

## What was changed in the prebuilt webview (when rebuilt)

The original build pipeline applied Hermes-specific overlay patches to the upstream
`webview-ui/` source before building. Those patches lived in `overlay/` in the
original repo (not in this drop). The patches:

- Adjusted the `postMessage` transport to accept Hermes session events instead of
  upstream hook events. (The upstream protocol is hook-event-shaped; Hermes shapes
  its events into the same wire format so the prebuilt renderer accepts both.)
- Wired the 28 new furniture sprites into the catalog.
- Wired the 4 presets into the layout picker.
- Pointed the asset loader at `decoded/*.json` instead of decoding on demand.

The exact patch text is not in this repo because `overlay/` was the source of
truth in the original build pipeline, and the original build pipeline is not in
this drop. If you need the patches, the original repo (the private one Taras
used to build `dist/app/`) has them. The patches are mechanical (text replace
on the upstream TypeScript); reproducing them is a few hours of work, not days.

## Closing the loop

If a future contributor wants to upstream the new furniture, the path is:

1. Fork `pixel-agents`.
2. Add the 28 procedurally-drawn furniture to `webview-ui/public/assets/furniture/`
   with manifests.
3. Update `furnitureCatalog.ts` to include them.
4. Open a PR upstream.

The 4 presets, the 2 new default layouts, the Hermes HQ layout, the custom font,
the Python backend, the host shell, the manifest, the state files, the approval
queue, the lane policy, the audit log, and the client-marker filter are
Hermes-specific. They do not belong upstream.
