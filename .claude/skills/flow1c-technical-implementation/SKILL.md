---
name: flow1c-technical-implementation
description: Implement an approved user-referenced technical design in the configured 1C extension, or document the exact implemented diff. Do not use for independent code review.
---

Read and apply the canonical instructions in `../../../.agents/skills/flow1c-technical-implementation/SKILL.md`.

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
| development | formal → flow1c-technical-implementation / technical-architect | Измени расширение по согласованному дизайну SYNTHETIC-FS-1.; Реализуй проверку ввода в текущей ветке расширения. | Read-only ревью; описание технической реализации в документе | chat, work_item, configuration, extension, registry, redmine / schemas/evidence.schema.json |
| technical-implementation | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-implementation / technical-architect | Документируй выполненную реализацию SYNTHETIC-FS-1 формально.; Объясни выполненные изменения по приложенному описанию. | Поручение изменить расширение; отдельный раздел ФС | chat, attachment, work_item, configuration, extension, git_history, git_snapshot, redmine / schemas/evidence.schema.json |
<!-- flow1c:routes:end -->
