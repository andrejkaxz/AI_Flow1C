---
name: flow1c-consultation
description: Discuss an FLOW1C task, review the workflow product, or prepare a free document draft from chat and optional materials without requiring a project or task reference.
---

Read and apply the canonical instructions in `../../../.agents/skills/flow1c-consultation/SKILL.md`.

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
| consultation | explore → flow1c-consultation / —; draft → flow1c-consultation / — | Объясни подход к проектированию учёта заказов.; Проверь реквизиты заказа в нашей текущей конфигурации. | Конкретный запрос 1С; историческая ветка; mutation расширения | chat, attachment, configuration, extension, redmine / docs/dialogue.md |
| workflow-review | explore → flow1c-consultation / —; draft → flow1c-consultation / — | Проверь правила Flow1C на противоречия.; Подготовь черновой отчёт о структуре workflow. | Обновление установленного продукта; анализ конфигурации 1С | chat, attachment, workflow / docs/dialogue.md |
| functional-spec | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-functional-spec / analyst | Подготовь независимый черновик ФС по моему описанию.; Подготовь формальную ФС по рабочему элементу SYNTHETIC-FS-1. | Согласование готовой ФС; изменение исходников расширения | chat, attachment, configuration, extension, work_item, registry, template_library, redmine / schemas/manifest.schema.json |
| functional-section | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-consultation / analyst | Опиши только раздел технической реализации по моему тексту.; Подготовь раздел настроек системы независимого черновика. | Mutation расширения; полная ФС; approval не является поручением записи | chat, attachment, work_item, configuration, extension / schemas/functional-spec-section-state.schema.json |
| functional-review | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-functional-review / functional-architect | Выполни формальное функциональное ревью ФС SYNTHETIC-FS-1.; Обсудим полноту приложенной ФС без формального согласования. | Разработка новой ФС; approval без точной версии | chat, attachment, work_item, registry, redmine / schemas/evidence.schema.json |
| technical-design | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-review / technical-architect | Подготовь формальный технический дизайн для SYNTHETIC-FS-1.; Объясни варианты технического решения по этому описанию. | ТЗ technical-assignment по шаблону; изменение расширения | chat, attachment, work_item, configuration, extension, redmine / schemas/evidence.schema.json |
| technical-implementation | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-implementation / technical-architect | Документируй выполненную реализацию SYNTHETIC-FS-1 формально.; Объясни выполненные изменения по приложенному описанию. | Поручение изменить расширение; отдельный раздел ФС | chat, attachment, work_item, configuration, extension, git_history, git_snapshot, redmine / schemas/evidence.schema.json |
| code-review | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-review / technical-architect | Проверь ветку synthetic-feature и покажи изменения.; Выполни формальное code review рабочего элемента SYNTHETIC-FS-1. | Ревью не разрешает исправление кода; номер задачи не является Git ref | chat, attachment, work_item, configuration, extension, git_history, git_snapshot, redmine / schemas/git-analysis.schema.json |
| testing | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-testing / tester | Подготовь формальный план тестирования SYNTHETIC-FS-1.; Обсудим тестовые сценарии по этому описанию. | Создание документа произвольного типа по шаблону; выдуманные runtime результаты | chat, attachment, work_item, registry, template_library, redmine / schemas/evidence.schema.json |
<!-- flow1c:routes:end -->
