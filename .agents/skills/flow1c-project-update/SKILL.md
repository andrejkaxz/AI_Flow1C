---
name: flow1c-project-update
description: Safely update an existing Flow1C Git checkout while preserving validated machine-local settings. Use for update, upgrade, or pull requests after initial setup; do not use for first-time installation or repairing missing configuration.
---

# FLOW1C project update

Read `docs/update.md`.

In OpenCode, open a formal update gate with `flow1c_begin`, then run `flow1c_action action=update` with `parameters_json` containing `{"confirmed":true}` and the returned `gate_id`. A direct shell invocation from the update agent is blocked by the guard. For manual use, run `scripts/update.ps1 -Json` from the Flow1C repository root. The updater's JSON is authoritative: it checks the tracked worktree, creates an ignored backup, fetches the configured upstream, accepts only a fast-forward update, migrates `.flow1c.local.json`, synchronizes changed dependencies, starts RLM, and runs `scripts/flow1c.py doctor --json`.

Do not run `/setup`, `setup-state.ps1`, or `configure-project.ps1` during an ordinary update. Do not ask the user to reconfirm existing paths, URLs, repository coordinates, or Git identity when the updater and doctor validate them. Ask only for genuinely missing new data, permission required by a new dependency, resolution of tracked local changes or Git divergence, or repair of an invalid/missing local configuration.

If the updater returns `NEEDS_CONFIRMATION` for an incompatible or missing project Python, ask once and then continue the same update with `-RepairPrerequisites`. The recovery installs an approved Python >=3.10 when necessary, preserves the old `.venv` under `.workspace/backups`, recreates it, and resumes migration, external tools, indexes, and doctor without creating a setup gate. In `flow1c_action`, pass `repair_prerequisites: true`.

Per-run external-tool choices are passed through `flow1c_action` as `allow_external_updates: true` or `skip_external_tool_updates: true`. Approval must reach `update.ps1` as `-AllowExternalUpdates`; refusal may continue with the explicit skip and must be reported as a deviation. Do not require a persistent `policy.apply_updates` edit for a one-time approval.

`WAITING_BACKGROUND` is a pending index job, not failure or completion. Preserve
the same gate_id and update_id. Continue with `flow1c_action action=update`,
`parameters_json={"confirmed":true,"wait_seconds":30}`; the CLI restores the ID
and approved options. Manual clients use `update.ps1 -Json -UpdateId <returned-ID>
-WaitSeconds 30`. Never start zero-wait retry loops or complete a pending gate.
After a restart resume the saved gate and checkpoint. On a real failed job inspect
detail/logs first; `retry_failed_indexes:true` / `-RetryFailedIndexes` preserves
its history and enables a deliberate retry after the cause has been corrected.

Index freshness is verified by the shared index runtime and doctor. Never patch
RLM's database, globally disable age checks, or report READY from process exit
code alone. Source, database and tool changes invalidate validation receipts.

Report `READY` only from the updater's successful machine-readable result. On `BLOCKED`, preserve the local configuration and backup, state the exact failing step, and do not discard commits or files automatically.

If `agent_runtime.restart_required` is true, complete the successful update gate
first, then fully quit and reopen OpenCode in the same project before using new
tools. Resume the saved gate_id/setup_id/operation_id. Installation readiness
does not verify the tool set of an already running OpenCode instance.

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
| update | formal → flow1c-project-update / — | Обнови Flow1C с сохранением локальных настроек.; Продолжи незавершённое обновление продукта. | Изменение шаблона; изменение расширения 1С | chat, workflow / schemas/update-checkpoint.schema.json |
<!-- flow1c:routes:end -->
