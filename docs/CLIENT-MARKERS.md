# Client Markers

The Operator Office withholds tasks, events, and missions for known client slugs from every view. This is a **projection filter**, not isolation: the data still exists in Kanban and Workspace OS, the office just does not show it to the operator on the office tab.

## Why

The office is the operator's ambient view. If it surfaced client work in the same place as the operator's own work, the operator could misroute a decision, copy a task into a public board by accident, or leak a client marker in a screenshot. The filter keeps client work out of the office tab without removing it from Kanban.

## Markers

The plugin recognizes these case-insensitive substrings in task titles, task bodies, mission slugs, mission names, profile names, and event payloads:

- `insidedynamic`
- `idstar`
- `founderos`
- `founder-os`
- `fos-neu`

A task whose title contains any of these substrings (case-insensitive) is withheld from the office tab. A mission whose slug contains any of them is withheld from the Missions panel. An event whose payload contains any of them is withheld from the live event stream.

The list is closed. Adding a new marker is a code change in `plugin/office_control.py` and a new release.

## What "withheld" means in practice

| View | Withheld behavior |
|---|---|
| Floor plan characters | Profile hidden if its name contains a marker. |
| Kanban board projection | Task not shown. |
| Mission list | Mission not shown. |
| Live event stream | Event filtered out before reaching the host shell. |
| Approvals queue | Approval not shown (this is the load-bearing one — see below). |
| Audit log | Audit entries are NEVER withheld. They are append-only and complete. |

The audit log is the only place the filter is not applied. The office records the fact that it filtered an item, but it never hides the record of the filter from the audit log.

## Why approvals must be filtered too

The approvals queue is the list of decisions the operator owes. If a client approval is shown in the office, the operator might approve it from the office tab instead of through the client-specific flow. The office is for the operator's own work; client approvals are routed through the client-specific flow. Withholding them from the office is what keeps the two channels separate.

## Edge cases

- A team with a marker in its name is hidden from the teams panel. Its members are not hidden (they are real profiles with their own names); only the team grouping is hidden.
- A Kanban board whose name contains a marker is hidden from the office. The board still exists; the operator can still see it in the regular Kanban view.
- A profile that was previously visible but whose name was changed to contain a marker is hidden on the next snapshot.
- A marker substring inside a longer word is matched (`founderos` matches `myfounderostask`).

## How to debug a "where did my task go" complaint

1. Open the task in regular Kanban. Confirm the title and body.
2. Run `grep -iE 'insidedynamic|idstar|founderos|founder-os|fos-neu' <task-title-or-body>`. If it matches, the office is correctly withholding it.
3. To override (one-off): rename the task in Kanban. The office will show it on the next snapshot.
4. Permanent: do not put client markers in task titles. Use a client-tag field instead, and the office will not see it.

## What this filter is NOT

- It is not access control. Any operator with the dashboard session token can hit `/api/plugins/pixel-office/office/tasks/{task_id}` directly and see the task. The filter only applies to the host shell and the projection endpoints.
- It is not encryption. The data is in plaintext in Kanban and Workspace OS.
- It is not a substitute for proper client confidentiality policies. It is a UI guard.
