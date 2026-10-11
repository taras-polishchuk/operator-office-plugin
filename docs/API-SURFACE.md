# API Surface

All endpoints are mounted under `/api/plugins/pixel-office/`. The full list, taken from `plugin/plugin_api.py` decorators.

## Top-level

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe. Returns 200 + version. |
| GET | `/snapshot` | Full snapshot of office state. Heavy; prefer the targeted endpoints below. |
| GET | `/bootstrap` | Initial payload the host shell loads on tab open. Includes agents, teams, rooms, boards, settings. |
| GET | `/layout/presets` | List of layout presets shipped with the plugin. |
| POST | `/layout/preset/{name}` | Apply a layout preset (backs up the current layout first). |

## Layout and seats

| Method | Path | Purpose |
|---|---|---|
| PUT | `/layout` | Replace the full layout. Heavy; usually only called by the layout editor. |
| PUT | `/seats` | Replace the seat-to-profile mapping. |
| POST | `/seats/apply-named` | Apply a named seat preset. |
| GET | `/seats/drift` | Compare current seats against the named preset. |

## Settings and mappings

| Method | Path | Purpose |
|---|---|---|
| PUT | `/settings` | Update office settings. |
| PUT | `/area-mappings` | Update area-to-function mapping. |

## Agents and teams

| Method | Path | Purpose |
|---|---|---|
| POST | `/agents` | Create a profile (Hermes core POST /api/profiles under the hood, with optional clone source, SOUL, team). |
| GET | `/agents/{name}` | Profile detail. |
| POST | `/agents/{name}/task` | Give the agent a Kanban task. |
| POST | `/agents/{name}/hide` | Hide the agent from the office. |
| GET | `/teams` | List teams. |
| POST | `/teams` | Create team. |
| PATCH | `/teams/{team_id}` | Update team. |
| DELETE | `/teams/{team_id}` | Delete team. |
| POST | `/teams/{team_id}/task` | Give a team a task (sent to the team lead with the roster attached). |

## Office namespace

| Method | Path | Purpose |
|---|---|---|
| GET | `/office/boards` | List Kanban boards visible to the office. |
| GET | `/office/state` | Office projection state (used by the host shell for incremental updates). |
| GET | `/office/tasks/{task_id}` | Single task detail. |
| GET | `/office/tasks/{task_id}/artifact` | The artifact attached to a task. |
| GET | `/office/tasks/{task_id}/attachments/{att_id}` | A preserved attachment on a task. |
| GET | `/office/missions` | List missions (from Workspace OS). |
| GET | `/office/missions/{slug}/{name}` | One artifact from one mission (e.g. `slug=video-factory/decision-rationale`, `name=final-report.md`). |
| GET | `/office/approvals/queue` | List of approval decisions owed. |
| POST | `/office/approvals/decision` | Submit an approval decision (HMAC-bound, idempotent, audited). |
| POST | `/office/approvals/rerequest` | Re-arm an approval after it expired. |
| GET | `/office/audit` | Read the audit log (paginated). |
| PUT | `/office/board` | Switch the active board. |
| GET | `/office/lanes/templates` | Lane policy templates. |
| GET | `/office/lanes/runs` | Recent lane runs. |
| POST | `/office/lanes/runs` | Schedule a new lane run for a task. |
| GET | `/office/rooms` | List rooms and their current state. |
| PUT | `/office/rooms` | Update rooms (used by the room editor). |

## Authentication

All endpoints require a valid Hermes dashboard session token. The plugin does not introduce its own auth. The dashboard session token is checked by Hermes core before the plugin handler runs.

## Origin guard

All non-safe-method requests (anything that is not `GET`, `HEAD`, `OPTIONS`) are subject to an origin guard in `plugin_api.py:_origin_guard`. Only loopback origins (`127.0.0.1`, `localhost`, `::1`) are accepted. If the operator accesses the dashboard via a public hostname, the origin guard will reject the write with a 403.

If you need to expose the office on a public hostname, you must:

1. Run the dashboard behind a reverse proxy that rewrites the `Origin` header to `http://127.0.0.1:9119`, OR
2. Modify the origin guard to allow your public origin (read the comment block before changing it).

## Error responses

Errors are FastAPI's default: `{ "detail": "..." }`. The host shell parses the `detail` field and shows it to the operator. Idempotent failures (replay of a decision) return 200 with the original outcome, not an error.
