---
name: flow1c-functional-spec
description: Elicit missing information and author or update a 1C functional specification for one user-referenced work item from registered requirements, meeting artifacts, local configuration evidence, and the approved Word template.
---

# FLOW1C functional specification

Formal document gates may continue in `READY_WITH_DEVIATIONS` only after an explicit user decision over named waivable `conditions`. Record `flow1c_dialogue action=deviate` with `deviation_type`, `scope`, already shown `condition_ids` and the exact `user_statement`. Preserve `mode=formal` and `UNVERIFIED_DRAFT`; this never creates approval or authorizes status, publication or extension mutation. Missing output and integrity failures remain `NON_COMPLIANT`.

When the registry is absent or invalid, a formal specification may continue as provisional only after a typed `registry_bypass`. Create the work item with `provisional-start`, preserve the user's description in `input/user-brief.md`, and treat any supplied requirement IDs as unconfirmed. Every provisional output must retain `UNVERIFIED_DRAFT`; it cannot authorize publication or extension changes.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode as the primary skill. Formal instructions do not block independent UNVERIFIED_DRAFT documents. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.
When the user requests only a named section, route to `functional-section` and use the canonical section catalog. Do not create the full functional specification. Approval and Word writeback are separate actions; never treat the message «согласовано» as permission to edit DOCX.

Work inside the safe slug directory resolved by the CLI for one opaque user reference. If it does not exist, use `flow1c-registry` and `scripts/flow1c.py fs-start --task-reference "<user value>"` first.

Build the analyst context with `scripts/flow1c.py context-build --code "<user reference>" --role analyst`. Read only the generated context and directly relevant meeting artifacts. Convert DOCX/PDF inputs with MarkItDown when useful, but use the structured importer for the Excel register.

Before drafting:

1. Separate source facts, assumptions, open questions and proposed decisions.
2. Check actors, trigger, preconditions, target behavior, exceptions, permissions and acceptance outcomes.
3. Add unresolved items to `analysis/questions.md` and ask concise grouped questions.
4. Record answers and decisions with their source; never present an inference as an approved fact.
5. Verify all 1C metadata names from local XML/BSL. Use rlm-tools-bsl for narrow retrieval on large sources.

Maintain `analysis/traceability.md`. Draft in `specification/functional-spec.md`, then populate a copy of the configured DOCX template without changing approved styles, numbering, tables or headers. Deliver only a clean customer version; keep review discussion in Git and review files.

For `Техническая реализация` within the full specification, apply the same canonical contract in `standards/functional-specification-sections.md` as for an independent section: five plan categories, all statements in the past tense (including confirmed absence), summary facts of additions/changes with procedures/functions of the same module listed together. Do not create per-object cards, detailed algorithm/handler narratives or code listings. Include developer changes to attribute/dimension/resource properties outside the design in category 5. Keep unknowns `open` and traceability outside the customer section; verify the text against this contract before delivery.

Do not inspect a human-approved comparison document during a blind prototype run unless the user ends the blind phase.
When using a project library template, call template list/resolve in this same
formal gate and load the saved guide. The library satisfies the template condition;
otherwise retain functional_spec_template. Use the shared contract in
docs/document-templates.md, not a second primary skill. Map canonical sections,
save and explicitly approve their exact content with section-save/section-approve,
then document-plan/write. The writer rechecks those approvals and emits a new
copy plus Markdown content; use the existing flow1c_write route for the canonical
stage artifact. Keep evidence and publication checks. A Markdown template never
waives an explicit project requirement for Word.

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
| functional-spec | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-functional-spec / analyst | Подготовь независимый черновик ФС по моему описанию.; Подготовь формальную ФС по рабочему элементу SYNTHETIC-FS-1. | Согласование готовой ФС; изменение исходников расширения | chat, attachment, configuration, extension, work_item, registry, template_library, redmine / schemas/manifest.schema.json |
<!-- flow1c:routes:end -->
