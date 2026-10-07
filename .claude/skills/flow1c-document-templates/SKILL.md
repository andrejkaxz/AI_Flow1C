---
name: flow1c-document-templates
description: Manage project document templates and create pinned DOCX/Markdown drafts using the shared library contract.
---

Read and follow .agents/skills/flow1c-document-templates/SKILL.md as the canonical
instructions. Use the same flow1c.py template/document CLI, schemas and gates.
Do not duplicate policy or create a second template library.

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
| template-management | explore → flow1c-document-templates / —; formal → flow1c-document-templates / — | Добавь приложенный шаблон ТЗ в библиотеку проекта.; Обнови правило заполнения существующего шаблона. | Обновление Flow1C; генерация документа; автоматическая замена пользовательских данных | chat, attachment, template_library / schemas/document-template-operation.schema.json |
| template-document | draft → flow1c-document-templates / —; formal → flow1c-document-templates / — | Создай ТЗ technical-assignment по нашему шаблону.; Подготовь инструкцию пользователя по библиотечному шаблону. | Технический дизайн; formal ФС/тестирование сохраняют специализированный primary | chat, attachment, template_library, redmine / schemas/document-template-validation.schema.json |
<!-- flow1c:routes:end -->
