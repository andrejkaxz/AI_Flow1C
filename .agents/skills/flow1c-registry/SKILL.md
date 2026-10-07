---
name: flow1c-registry
description: Import, validate, reconcile, and assess impact from the project Excel registers of processes, requirements, and functional specifications. Use when a register is added or updated, or when requirement status is requested.
---

# FLOW1C registry

If validation is structurally impossible, keep the structured `errors`, `warnings`, and `report_path` available to the user. Local row and relation errors produce `partial`: use the scoped index for the selected requirement or FS and offer ordinary `fs-start` when that scope is usable. Offer a typed `registry_bypass` only when the selected scope cannot be resolved, or an independent draft. Never interpret a generic deviation as permission to bypass the registry.

Use `registry-reconcile` for a corrected workbook. Apply only exact or user-confirmed mappings, preserve old evidence, and leave the work item provisional when any mapping is disputed.

Read `docs/lifecycle.md` when assessing changes to an existing work item.

Accept a new workbook from any user-selected location or from `<documentation_path>/inbox/`. Run `scripts/flow1c.py registry-import --file <xlsx>` from the workflow repository; the CLI writes results to the external documentation repository configured in `.flow1c.local.json`. `--allow-errors` remains a compatibility alias; partial indexes are written by default and never replace the last fully verified normalized indexes.

MVP invariants:

- column J is the authoritative requirement ID;
- the user assigns every project/task reference; it is opaque and may use the legacy `G-xxx` format but Flow1C never requires or invents it;
- one FS may contain several requirements;
- one requirement may belong to at most one FS;
- source rows are never silently deleted from project history;
- a changed requirement produces an impact warning, not an automatic PR.

Read `<documentation_path>/registry/import-report.md`. Resolve errors in the selected requirement/FS scope before creating a work item; unrelated local errors remain visible but do not block scoped work. Treat generated JSON as an index, not as a replacement for the versioned source workbook.

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
| registry | formal → flow1c-registry / — | Импортируй приложенный Excel-реестр требований.; Проверь структуру реестра перед нормализацией. | Создание вопросов интервью; выдумывание ID требований | chat, attachment, registry, redmine / schemas/registry-status.schema.json |
<!-- flow1c:routes:end -->
