---
name: flow1c-technical-implementation
description: Implement an approved user-referenced technical design in the configured 1C extension, or document the exact implemented diff. Do not use for independent code review.
---

# FLOW1C technical implementation

In a deviated formal gate, document only the permitted work-item output. `READY_WITH_DEVIATIONS` never authorizes extension checkout mutation, approval, status changes or publication; retain `UNVERIFIED_DRAFT` and use `NON_COMPLIANT` for missing output or integrity failures.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.
The independent `technical-implementation` section is governed by `standards/functional-specification-sections.md` and always checks exactly five categories. Each category must be `described`, `not_applicable` with a reason, or `open`; do not include code listings or unverified metadata names.

Start only from a valid `flow1c_begin` gate for `development` or `technical-implementation`. Use the injected technical-architect context, approved functional specification, approved technical design and bounded extension evidence.

For development, verify every configuration object and extension point with gated RLM queries before editing. Change only the configured extension checkout on the manifest branch. Preserve unrelated changes and do not edit the base configuration.

For implementation documentation, describe only facts visible in the validated diff or RLM evidence. Put unverified interpretations under `Неподтверждённые предположения` and label them `heuristic`. Do not call `ObjectModule.bsl` a manager module and do not infer why generated XML identifiers changed.

Write the customer-facing `Техническая реализация` as a summary of completed change facts, using exactly the five required categories from the canonical catalog as the top-level groups in catalog order. Write all statements in the past tense, including confirmed absence: «добавлен», «добавлены», «изменена», «не добавлялись», «не изменялись». Keep category headings nominal. Record only the fact of addition/change and verified module, procedure/function or property names; list procedures/functions of the same module together. Do not create per-object cards, a detailed narrative for each entity, algorithm/handler behavior descriptions or code listings. Category 5 covers attribute/dimension/resource properties changed by the developer outside the design, such as «Индексирование». Consolidate successive edits to the same handler into one final change fact. Do not narrate stages, dates, test iterations or superseded intermediate behavior. Do not put task IDs, issue numbers, Git branches, commits, PR/MR numbers, evidence limitations or source-provenance notes in the heading or body. Keep traceability in gate evidence, `sources` and review artifacts outside the section. If evidence is insufficient, preserve `open`/`heuristic` in checklist/evidence without inventing facts; use «Сведения об изменениях не были подтверждены» in the body instead of claiming no changes. Check every statement for past tense, summary scope, category coverage and forbidden details before saving or presenting it.
When preparing Word content, number the five top-level categories as `1.` through `5.`. Number subordinate points within each category as `1.1.`, `1.2.`, `2.1.` and so on. Do not use `a.`, `b.`, `c.` or a generic Word numbered-list style for the categories: a template can render that style as letters. The DOCX writer converts Markdown headings and list markers to Word paragraphs, and converts inline backtick-delimited identifiers to «елочки». Check the written DOCX text and paragraph styles; visible `###`, `**`, backticks or escaped underscores mean the output is defective and must be regenerated before delivery.

Run BSL Language Server after code changes or before documenting a completed implementation. Finish only through `flow1c_complete`; a draft produced with missing mandatory inputs remains `UNVERIFIED_DRAFT` and cannot advance status or be published.

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
