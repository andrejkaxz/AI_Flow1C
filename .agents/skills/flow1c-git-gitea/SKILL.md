---
name: flow1c-git-gitea
description: Safely create Flow1C branches and commits and publish pull requests to Gitea for one user-referenced work item. Use when the user asks to start a branch, commit, push, publish, or open a PR.
---

# FLOW1C Git and Gitea

Read `docs/lifecycle.md`. Before mutation, identify the repository, branch, opaque user-supplied work reference, changed files and target branch. Never invent a reference or require `G-xxx`. Preserve unrelated changes. Use `scripts/flow1c.py git-commit` to stage only the safely resolved work-item directory, status page and explicitly requested registry changes.

Branch names:

- `fs/<safe-reference-slug>/specification` for initial documentation;
- `feature/<safe-reference-slug>` in the extension repository;
- `fs/<safe-reference-slug>/post-development` after implementation;
- `fs/<safe-reference-slug>/acceptance` for testing and delivery.

Create Gitea PRs with `scripts/flow1c.py pr-create`. The token comes from the configured environment variable and must never be printed or committed. Include requirement IDs, phase, validation results and open questions. Assign human reviewers. Never merge or approve unless the user explicitly requests that distinct action and has authority.

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
| publish | formal → flow1c-git-gitea / — | Опубликуй проверенный результат SYNTHETIC-FS-1 через PR.; Подготовь PR утверждённой версии спецификации. | Черновик не публикуется; подтверждение не отменяет validation и approvals | chat, work_item, registry / schemas/evidence.schema.json |
<!-- flow1c:routes:end -->
