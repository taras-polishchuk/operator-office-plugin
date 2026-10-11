# Mission Control

Mission Control is the control-plane view in the Operator Office. It is **not** a separate application, not a separate repo, and not even a separate Python file. It is a **view** assembled in `plugin/office_control.py` from the same data sources as the rest of the office, with a stricter contract.

## What it is for

The Office shows everything a Hermes user is doing. Mission Control shows only the work that matters for the operator's job:

- Tasks in governed lanes.
- Approvals waiting for a human.
- Incidents (lane runs that failed or diverged).
- Live events from the Kanban worker stream.
- The 8-artifact mission view from Workspace OS.
- Agents and teams.

The Mission Control view is the part of the office that the operator uses to make decisions. The pixel-art office is the same data, drawn as characters in rooms, for ambient awareness.

## How the room map maps to Mission Control

The default 9-room floor plan is the same 9 functions the Mission Control view surfaces:

| Room | Mission Control section |
|---|---|
| Command Center (room 1) | Overview, Mission Control, Approvals |
| Client Work (room, locked) | Withheld from this profile by default |
| Product Lab (room 2) | Tasks filtered to product work |
| Research Lab (room 3) | Tasks filtered to research work |
| Content Factory (room 6) | Tasks filtered to content work |
| Communications (room 7) | Tasks filtered to comms / career work |
| Knowledge Vault (room 8) | Missions and their 8 artifacts |
| Ops & Approvals (room 9) | Incidents, lane runs, audit log |
| Open Space (the big hall) | All workers, all events, all teams |

The mapping is **configurable**: a room can be re-mapped to a different function via `PUT /api/plugins/pixel-office/area-mappings`. The defaults are the operator-friendly ones above.

## Governed lanes

A **governed lane** is a Kanban lane that does not let workers execute freely. The office acts as a gate:

1. Worker claims a task in the governed lane.
2. The office schedules a `lane run` for the task.
3. The lane run does the work but with **execution adapters disabled** — no auto-deploys, no money-moving tools, no destructive writes.
4. An independent QA reviewer lane inspects the lane run.
5. The reviewer decides: pass, request changes, or block.
6. The decision is HMAC-bound to the action, target, task fingerprint, and an expiry.
7. The decision is appended to `office-audit.jsonl`.
8. Only then does the office forward the decision to the Kanban task as a normal Kanban transition.

The point: the office is not a parallel execution engine. It is a **gate** that has to be passed before the Kanban task can move forward. The audit log is the record.

## Live event stream

The office subscribes to the Hermes Kanban WebSocket and replays from the last cursor on reconnect. Three states the operator sees:

- `LIVE` — connected, receiving events.
- `RECONNECTING` — connection lost, replaying from last cursor.
- `OFFLINE` — connection lost for too long; last-good snapshot is shown with a banner.

Cursor replay is per-profile and persisted in plugin-owned state. The cursor advances only after the event is acknowledged by the host shell.

## Approvals queue

The approvals queue is the list of decisions the operator owes. Each entry has:

- The action to be approved (e.g. "rotate API key for tenant X").
- The target (the resource being acted on).
- The task fingerprint (HMAC of task ID + lane + age).
- An expiry (after which the approval is no longer valid).
- An idempotency key (the fingerprint, again).

`POST /api/plugins/pixel-office/approvals/decision` accepts the decision. The plugin checks the HMAC binding, the idempotency key (against `decisions.json`), and the expiry. The decision is appended to `office-audit.jsonl` and the underlying Kanban transition is forwarded.

## Incidents

An incident is any of:

- A lane run that failed.
- A lane run that diverged (a worker did something the lane policy did not authorize).
- A Kanban worker that disappeared mid-run.
- A decision that arrived after its expiry.
- A client-marker token that tried to leak through a view.

Incidents are surface in the left panel and on the floor plan as a red dot above the affected room. They never block the office; they require the operator to act.

## Missions (Workspace OS view)

Missions are not owned by the office. They live in Workspace OS at `.project-state/<slug>/`. The office reads **only** these 8 artifact names and renders them read-only:

- `mission.md`
- `source-task.md`
- `plan.md`
- `context.md`
- `decisions.md`
- `findings.md`
- `lessons.md`
- `final-report.md`

Anything else in the mission directory is ignored. This keeps the office from accidentally reading drafts, intermediate notes, or private artifacts.

## What Mission Control is not

- It is not a separate database. It is a projection.
- It is not a workflow engine. Workers do the work; Mission Control gates it.
- It is not a replacement for Kanban. Kanban stays authoritative.
- It is not a money-mover. Money paths still go through the normal Kanban API.

## What an agent should do if Mission Control is empty

Walk this list:

1. Are there profiles beyond the default? (`curl /api/profiles`)
2. Are there Kanban boards with tasks? (`curl /api/plugins/kanban/boards`)
3. Are there missions in `.project-state/`?
4. Is a profile selected on the office side panel?
5. Are client-marker tokens filtering everything? (check `docs/CLIENT-MARKERS.md`)

If all of those are non-empty, the Mission Control view should populate within a few seconds. If not, the office is correctly reporting an empty system.
