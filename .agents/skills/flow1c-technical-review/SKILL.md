---
name: flow1c-technical-review
description: Review 1C technical design or extension code against an approved user-referenced specification, local configuration evidence, project policy, ITS/v8std, and static diagnostics. Use for technical architect approval and Git diff review.
---

# FLOW1C technical review

If the user explicitly accepts missing document inputs, record only the displayed condition IDs through `flow1c_dialogue action=deviate`. A deviated review remains `UNVERIFIED_DRAFT`, is not approval, and cannot authorize extension mutation or publication.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.

Read `standards/source-precedence.md`, then load only the relevant standard among `standards/bsl.md`, `standards/queries.md`, and `standards/customization.md`.

Build the `technical-architect` context. Review only the requested extension diff. Use rlm-tools-bsl to retrieve referenced configuration objects and procedures. Never infer metadata names from FS wording.

Evaluate implementation quality and functional conformity independently. When BSL Language Server is configured, run `flow1c_analyze_bsl` against the selected source and inspect its JSON or SARIF report before completing the review. This read-only diagnostic may run while formal prerequisites are blocked; it does not resolve those prerequisites. Cite findings from that report as `BSL-LS`; do not treat a clean static report as proof of functional conformity. Use mutating or build-oriented cc-1c-skills only for explicitly authorized XML/CFE artifact operations. Consultation-only query analysis is the narrow exception: route it to `flow1c-query-analysis`, which may use only read-only `meta-info` and `skd-info` through `flow1c_cc_inspect`. Record the source type for each finding and write the report under `reviews/technical-review.md` or `implementation/code-review.md`.

Do not change code during a review-only request. Do not approve a PR on behalf of the architect.

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
| technical-design | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-review / technical-architect | Подготовь формальный технический дизайн для SYNTHETIC-FS-1.; Объясни варианты технического решения по этому описанию. | ТЗ technical-assignment по шаблону; изменение расширения | chat, attachment, work_item, configuration, extension, redmine / schemas/evidence.schema.json |
| code-review | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-review / technical-architect | Проверь ветку synthetic-feature и покажи изменения.; Выполни формальное code review рабочего элемента SYNTHETIC-FS-1. | Ревью не разрешает исправление кода; номер задачи не является Git ref | chat, attachment, work_item, configuration, extension, git_history, git_snapshot, redmine / schemas/git-analysis.schema.json |
<!-- flow1c:routes:end -->
