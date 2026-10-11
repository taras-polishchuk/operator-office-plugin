# Data and State

The plugin follows a strict rule: **it owns only what it writes**. Everything else is read from Hermes core or from upstream sources. This file is the inventory.

## Plugin-owned state (the only state this plugin writes)

Location: `~/.hermes/state/pixel-office/`

Permissions: directory mode `0700`, files mode `0600`.

| File | What it holds | Lifecycle |
|---|---|---|
| `layout.json` | The current floor plan: tiles, areas, furniture, seats, pets, walls. | Created on first layout change. Safe to delete to reset. |
| `seats.json` | The seat-to-profile mapping. | Created on first seat assignment. |
| `settings.json` | Office-level settings (which board is selected, panel visibility, etc). | Created on first settings change. |
| `area-mappings.json` | Which area (room) is mapped to which function (Mission Control section). | Created on first mapping change. |
| `teams.json` | Teams: name, color, lead profile, member profiles. | Created on first team creation. |
| `ids.json` | Stable character id per profile. | Created on first profile render. Survives profile rename. |
| `hidden.json` | Profiles the operator has hidden from the office. | Created on first hide. |
| `office-audit.jsonl` | Append-only audit log for approvals. | Created on first decision. Never truncated by the plugin. |
| `decisions.json` | Idempotency keys for approval decisions. | Created on first decision. |

All files are JSON, all files are owned by the plugin, all files are safe to delete to reset state (with the obvious caveat that deleting `office-audit.jsonl` loses the audit trail).

## Read-only sources (the plugin does not own these)

### Hermes profiles

Path: `~/.hermes/profiles/<name>/` (where the profile is its own directory).

What the office reads:
- The profile name and metadata.
- The SOUL file (for inspector display).
- The session store (`<profile>/state.db`, mode=ro) for activity.

The office never writes to the profile directory. Profile creation goes through `POST /api/profiles` (Hermes core), which handles SOUL cloning, memory, etc.

### Hermes Kanban

Path: `~/.hermes/state/kanban/` (sqlite, owned by the Kanban plugin).

What the office reads:
- `GET /api/plugins/kanban/boards` — list of boards.
- `GET /api/plugins/kanban/board` — full board with tasks.
- `GET /api/plugins/kanban/workers/active` — currently active workers.

What the office writes (forwarded to Kanban):
- Approval decisions (Kanban transitions for tasks in a human lane).
- Lane-run results (via Kanban task status updates).

The office does not write directly to the Kanban sqlite. It always goes through the Kanban plugin API.

### Workspace OS missions

Path: `~/.project-state/<slug>/` (8 artifact filenames only).

What the office reads:
- `mission.md`
- `source-task.md`
- `plan.md`
- `context.md`
- `decisions.md`
- `findings.md`
- `lessons.md`
- `final-report.md`

Anything else in the mission directory is ignored. The office never writes to `.project-state/`.

### Session activity

Path: `<profile>/state.db` (sqlite, opened mode=ro).

What the office reads:
- Recent messages to derive character state (typing on write/edit/terminal, reading on read/search/web).
- Speech-bubble triggers ("turn done", "waiting for you", "needs you").

The office never writes to the session store.

## What the plugin does NOT read

- Memory stores (`memory.db`, Honcho). The office does not care about long-term memory; it cares about the current session.
- Channel / webhook config.
- Plugin keys / API keys.
- Cron schedules.
- Skills. (Skills live in the profile's skills directory; the office does not enumerate them.)

## Concurrency

The plugin is single-writer per file (the FastAPI request handler is synchronous, and the office state is small enough that contention is not a problem). If you run the dashboard behind multiple workers, you must serialize writes to `~/.hermes/state/pixel-office/` yourself (this plugin does not do it; in practice Hermes runs a single dashboard process).

The audit log (`office-audit.jsonl`) is append-only. Each line is one decision. The line format is JSON; see `office_control.py` for the exact keys. The plugin never rewrites past lines.

## Backup and reset

To back up: copy `~/.hermes/state/pixel-office/` somewhere safe. The audit log is the only file that, if lost, loses data the operator might need.

To reset (keep audit, drop everything else):

```bash
cd ~/.hermes/state/pixel-office/
for f in layout.json seats.json settings.json area-mappings.json teams.json ids.json hidden.json decisions.json; do
  rm -f "$f"
done
```

To fully reset including audit (irreversible):

```bash
rm -rf ~/.hermes/state/pixel-office/
mkdir -p ~/.hermes/state/pixel-office
chmod 700 ~/.hermes/state/pixel-office
```

The next request to the office will recreate the files.
