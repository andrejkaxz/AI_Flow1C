---
name: flow1c-functional-review
description: Review a functional specification against registered business requirements, meeting evidence, traceability, and related project decisions. Use for functional architect review or cross-specification conflict checks.
---

# FLOW1C functional review

A document-only formal review may use `READY_WITH_DEVIATIONS` only after an explicit, scoped user decision over currently shown waivable conditions. Preserve `UNVERIFIED_DRAFT` and report every limitation; this state is not functional approval and does not authorize publication.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.

Build the context with role `functional-architect`. Review the clean specification, requirements snapshot, traceability and only related wiki decisions.

Check that every requirement is represented without changing its meaning; actors, states, validations, exceptions, permissions and acceptance criteria are explicit; unsupported behavior is marked as a question or proposal; rules do not conflict with approved work items; and the FS is implementable and testable without guessing.

Write findings to `reviews/functional-review.md`, ordered by business impact. Cite the requirement and FS section for each finding. Edit the analyst's files only when the user asks to apply the review. Human approval remains a Gitea review action.

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
| functional-review | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-functional-review / functional-architect | Выполни формальное функциональное ревью ФС SYNTHETIC-FS-1.; Обсудим полноту приложенной ФС без формального согласования. | Разработка новой ФС; approval без точной версии | chat, attachment, work_item, registry, redmine / schemas/evidence.schema.json |
<!-- flow1c:routes:end -->
