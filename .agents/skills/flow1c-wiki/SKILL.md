---
name: flow1c-wiki
description: Report project status from Git-tracked manifests or update the Markdown project wiki with approved decisions and delivered 1C behavior. Use for status questions, chronology, or wiki synchronization.
---

# FLOW1C wiki and status

For a status question, read manifests first and query Gitea only when open PR state is needed. Do not infer completion from draft files.

Run `scripts/flow1c.py status` for a read-only view and `scripts/flow1c.py status --write` when the user asks to update the repository.

Update wiki content only from merged or explicitly approved decisions. Each entry identifies the original user-supplied work reference, requirement IDs, functional area, key behavior, important constraints and approval/delivery state. Never invent a reference or require `G-xxx`. Avoid duplicating the full FS.

<!-- flow1c:routes:start -->
Generated from `config/intent-routes.json` and `config/stages.json`.

Interpret the user's goal, then check a RouteProposal before begin. A route grants no permissions.
CLI: `route-catalog --json`, `route-check --json-stdin` (direct proposal), then `agent-begin --json-stdin` (nested `route_proposal`).
OpenCode: `flow1c_route_catalog`, `flow1c_route_check(proposal_json)`, then `flow1c_begin(route_proposal_json)`; operation/mode/summary must match.
Minimal proposal shape: `{"schema_version":1,"expected_outcome":"user goal","operation":"consultation","mode":"explore","sources":[{"kind":"chat","version":"provided"}]}`. Replace operation/mode/sources for the actual request; `summary` and `route_id` are not proposal fields.
The pre-gate catalog returns `proposal_schema`; use it to correct invalid inputs without read/grep/bash. Do not retry the same invalid proposal unchanged.
Reuse saved answers; clarify one unresolved choice before gate. Do not launch subagents or a next formal stage automatically.
For large accepted documents use `flow1c_context(view=compact)` / `agent-context --view compact`. Read entries with `flow1c_context(action=read,entry_id=...,cursor=...)` / `context-read`; `scope` contains exact saved decisions and `index` lists every source/part. Continue cursors until mandatory coverage is complete. Summaries grant no evidence or permissions; changed sources require rebuilding on the same gate. Legacy full view remains the default.

| Operation | Mode → primary skill / role | Apply when | Exclude | Sources / output |
|---|---|---|---|---|
| status | formal → flow1c-wiki / — | Покажи состояние рабочих элементов проекта.; Обнови отчёт статуса по существующим manifests. | Публикация; изменение approvals; статус внешней базы | chat, work_item, registry / schemas/manifest.schema.json |
<!-- flow1c:routes:end -->
