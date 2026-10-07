---
name: flow1c-testing
description: Create a 1C test plan, record a test protocol, or design Vanessa Automation scenarios for one user-referenced work item from requirements, the approved specification, and the implemented Git diff.
---

# FLOW1C testing

Testing documentation may continue in formal mode with a scoped user deviation for waivable conditions. Keep `UNVERIFIED_DRAFT` visible; `COMPLETE_WITH_DEVIATIONS` is not test approval or publication evidence, and output integrity remains non-waivable.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.

Build the `tester` context. When implementation exists, inspect the bounded extension diff to identify changed branches, validations, permissions and error paths. Code may add test cases but cannot replace approved expected behavior.

Maintain traceability between requirement IDs, acceptance criteria and test IDs. Include positive, negative, boundary, permission, migration and regression scenarios when relevant. Put planned cases in `testing/test-plan.md` and actual evidence in `testing/test-protocol.md`; never mark a case passed without execution evidence.

Generate Vanessa feature files under `testing/vanessa/` only when requested. Prefer existing steps verified from the project's Vanessa step library and connected Vanessa_for_AI guidance. Do not invent step phrases.
For a user test-protocol template, use template list/resolve and document tools
as a shared substep in this gate per docs/document-templates.md. Preserve the
existing work item/evidence requirements. Example test results, signatures and
approvals never become actual outcomes. Record missing execution evidence as
unknown and continue the same request after user answers.

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
| testing | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-testing / tester | Подготовь формальный план тестирования SYNTHETIC-FS-1.; Обсудим тестовые сценарии по этому описанию. | Создание документа произвольного типа по шаблону; выдуманные runtime результаты | chat, attachment, work_item, registry, template_library, redmine / schemas/evidence.schema.json |
<!-- flow1c:routes:end -->
