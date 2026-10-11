# Agent Reproduce — Deploy Operator Office into a Target Hermes

This file is for an agent (or an attentive human) that wants to drop the Operator Office plugin into a Hermes install that does not have it yet. It is written so you can follow it without asking the operator any questions, as long as you have shell access to the target machine and the Hermes install is reachable.

## 0. Assumptions

- The target machine runs a working Hermes Agent (the dashboard is up on `http://127.0.0.1:9119` by default, with kanban plugin enabled).
- You have shell access and can read the Hermes home (`~/.hermes/`).
- The operator has at least one profile beyond the default (Mission Control looks empty with only `default`).

## 1. Inspect the target

Before you change anything, confirm the target shape:

```bash
ls -la ~/.hermes/plugins/                          # other plugins present?
ls -la ~/.hermes/state/                            # does state dir exist?
curl -s http://127.0.0.1:9119/api/plugins | jq .   # what does the dashboard see?
curl -s http://127.0.0.1:9119/api/profiles | jq .  # how many profiles?
```

If the dashboard is not on 9119, find it with `ss -tlnp | grep hermes` or by reading `~/.hermes/config.yaml`.

## 2. Drop the plugin into the Hermes plugins dir

The repo ships a `plugin/` directory. It must land at `~/.hermes/plugins/pixel-office/`.

```bash
mkdir -p ~/.hermes/plugins
cp -r plugin/ ~/.hermes/plugins/pixel-office/
chmod -R u+rwX,go-rwx ~/.hermes/plugins/pixel-office/
```

Verify the layout matches what `manifest.json` declares:

```bash
test -f ~/.hermes/plugins/pixel-office/manifest.json
test -f ~/.hermes/plugins/pixel-office/plugin_api.py
test -f ~/.hermes/plugins/pixel-office/dist/index.js
test -f ~/.hermes/plugins/pixel-office/dist/app/index.html
```

The dashboard plugin loader expects exactly that directory tree. Do not nest it deeper.

## 3. Pre-create the plugin-owned state directory

`office_control.py` writes to `~/.hermes/state/pixel-office/` on first mutation. Pre-create with strict perms to avoid one later race:

```bash
mkdir -p ~/.hermes/state/pixel-office
chmod 700 ~/.hermes/state/pixel-office
```

Files this directory will eventually contain (all plugin-owned, all safe to delete to reset):

- `layout.json`, `seats.json`, `settings.json`, `area-mappings.json`, `teams.json`, `ids.json`, `hidden.json`
- `office-audit.jsonl` (append-only audit log)
- `decisions.json` (idempotency for approval decisions)

## 4. Python dependencies

The plugin imports only `fastapi` and `pydantic` at request time. Hermes already pulls these in; you do not need to add anything. If you want to be paranoid:

```bash
~/.hermes/hermes-agent/venv/bin/python -c "import fastapi, pydantic; print(fastapi.__version__, pydantic.__version__)"
```

## 5. Bring the dashboard up (or reload it)

If the dashboard auto-reloads plugins, you are done. If not, restart it. With Hermes installed system-wide:

```bash
systemctl --user restart hermes-dashboard.service
# or whatever the operator's process supervisor uses
```

Then verify the plugin is loaded:

```bash
curl -s http://127.0.0.1:9119/api/plugins | jq '.[] | select(.name == "pixel-office")'
```

You should see the manifest echo back (name, label, version, tab path).

## 6. Smoke test from the browser

Open `http://127.0.0.1:9119/pixel-office?profile=orchestrator` (substitute the operator profile name if different). You should see:

- The 9-room floor plan (Gym, Game Room, Cafe, Open Space, Server Room, Command, Meeting, Lounge, Library).
- The left-side panel with sections: Overview, Mission Control, Task, Approvals, Lanes, Incidents, Events, Missions, Agents, Teams, Rooms setup.
- An empty board selector at the top. Pick a Kanban board (start with the operator's main one).

If the floor plan is blank, profiles have not been mapped to characters yet. Open the Agents panel and assign profiles to seats. The `+ Agent` button creates a real Hermes profile via `POST /api/profiles`.

## 7. Wire up the optional but recommended pieces

These are not required for the plugin to render, but the Mission Control view becomes empty without them.

**Profiles (characters):** at least 2-3 profiles beyond the default. The Office treats each profile as a character. Without them, the room is just furniture.

**Kanban boards:** the operator must have at least one kanban board with tasks; the office reads tasks from `/api/plugins/kanban/board`. The `office/boards` endpoint returns the list of boards the operator can see; one of them is the operator-office-lab board used for governance.

**Workspace OS missions:** if the operator uses `.project-state/<slug>/` for missions, the office surfaces them under Missions via read of those 8 artifact names. Without missions, the panel is empty.

## 8. Layout preset

Two presets are shipped in `plugin/presets/`:

- `hermes-hq` — 44x32 layout with Gym, Game Room, Cafe, Open Space (16 desks), Server Room, Command, Meeting, Lounge, Library. Backs up the current layout before applying.
- `operator-office` — the default 9-room floor plan used by Operator Office.

Apply from the UI (HQ layout button) or via:

```bash
curl -X POST http://127.0.0.1:9119/api/plugins/pixel-office/layout/preset/hermes-hq
```

## 9. What to verify before declaring success

A working deployment answers "yes" to all of these:

1. `GET /api/plugins/pixel-office/health` returns 200.
2. `GET /api/plugins/pixel-office/bootstrap` returns a JSON with non-empty `agents`, `teams`, `rooms`, `boards`.
3. The browser shows all 9 rooms, the side panel is populated, and selecting a board shows tasks.
4. Approvals queue (left panel) shows items whenever a Kanban task is in a human lane (e.g. `review`, `blocked`).
5. Client-marker tokens (`insidedynamic`, `idstar`, `founderos`, `founder-os`, `fos-neu`) are recognized and withhold matching content. See `docs/CLIENT-MARKERS.md`.

## 10. What can go wrong

- **Plugin not in the side menu.** The dashboard loads plugins at start. Check `~/.hermes/plugins/pixel-office/manifest.json` parses (`jq . manifest.json`).
- **dist/app/ iframe is empty.** The prebuilt assets ship in the repo. If they were stripped during copy (for example by a `.gitignore` that matched them), restore them. The `dist/app/index.html` references `assets/index-*.js` and `assets/index-*.css`; both must exist relative to `dist/app/`.
- **Origin guard refuses requests.** All non-loopback mutations are blocked. The dashboard runs on `127.0.0.1` so this is fine; if the operator accesses it via a public hostname, the loopback origin guard will reject writes. The `plugin_api.py` `_origin_guard` is the authority; read it before changing the bind address.
- **Manifest version skew.** If the operator's Hermes is older than v0.21, the plugin SDK 1.1.0 may not exist. Either upgrade Hermes, or pin the plugin to an older SDK version by editing `manifest.json` and adjusting imports in `plugin_api.py`.
- **Layout presets too big.** They are JSON. If you see 500 on `GET /layout/presets`, the JSON parser is choking on trailing whitespace; check the file with `jq . plugin/presets/<name>.json`.
