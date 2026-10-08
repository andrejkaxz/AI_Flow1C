@AGENTS.md

# Claude Code — Flow1C

Apply the product contract in AGENTS.md before selecting the operation, mode,
source and one primary flow1c-* skill. Canonical skills are in `.agents/skills/`;
wrappers in `.claude/skills/` refer to them. Resume an existing request rather
than creating a new gate for each answer. Source facts require gated evidence.
For maintenance of Flow1C source code use CONTRIBUTING.md and repository tools.

Recover a completed result with `agent-handoff --action recover` or complete on
the same saved gate. Never replay its completed action. Read the handoff through
`agent-handoff --action read`; transfer text and next proposals grant no permissions.
See [recovery and compatibility](docs/handoff.md).

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
| interview-preparation | explore → flow1c-interview-preparation / —; draft → flow1c-interview-preparation / — | Подготовь Excel-реестр процессов и вопросов для обследования закупок.; Выдели требования из ответов приложенного интервью. | Собеседование кандидата на работу; formal импорт готового реестра требований | chat, attachment, configuration, extension / schemas/interview-register.schema.json |
| consultation | explore → flow1c-consultation / —; draft → flow1c-consultation / — | Объясни подход к проектированию учёта заказов.; Проверь реквизиты заказа в нашей текущей конфигурации. | Конкретный запрос 1С; историческая ветка; mutation расширения | chat, attachment, configuration, extension, redmine / docs/dialogue.md |
| query-analysis | explore → flow1c-query-analysis / —; draft → flow1c-query-analysis / — | Напиши запрос 1С для подсчёта остатков по складам.; Проверь приложенный текст запроса 1С без базы и XML. | Общее объяснение языка запросов; исполнение запроса в живой базе | chat, attachment, configuration, extension, git_snapshot / schemas/query-check.schema.json |
| workflow-review | explore → flow1c-consultation / —; draft → flow1c-consultation / — | Проверь правила Flow1C на противоречия.; Подготовь черновой отчёт о структуре workflow. | Обновление установленного продукта; анализ конфигурации 1С | chat, attachment, workflow / docs/dialogue.md |
| setup | formal → flow1c-project-setup / — | Настрой Flow1C для проекта с выбранными путями.; Продолжи прерванную настройку проекта. | Обновление установленной версии; замена шаблона документа | chat, workflow, template_library / schemas/agent-gate.schema.json |
| update | formal → flow1c-project-update / — | Обнови Flow1C с сохранением локальных настроек.; Продолжи незавершённое обновление продукта. | Изменение шаблона; изменение расширения 1С | chat, workflow / schemas/update-checkpoint.schema.json |
| registry | formal → flow1c-registry / — | Импортируй приложенный Excel-реестр требований.; Проверь структуру реестра перед нормализацией. | Создание вопросов интервью; выдумывание ID требований | chat, attachment, registry, redmine / schemas/registry-status.schema.json |
| functional-spec | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-functional-spec / analyst | Подготовь независимый черновик ФС по моему описанию.; Подготовь формальную ФС по рабочему элементу SYNTHETIC-FS-1. | Согласование готовой ФС; изменение исходников расширения | chat, attachment, configuration, extension, work_item, registry, template_library, redmine / schemas/manifest.schema.json |
| functional-section | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-consultation / analyst | Опиши только раздел технической реализации по моему тексту.; Подготовь раздел настроек системы независимого черновика. | Mutation расширения; полная ФС; approval не является поручением записи | chat, attachment, work_item, configuration, extension / schemas/functional-spec-section-state.schema.json |
| functional-review | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-functional-review / functional-architect | Выполни формальное функциональное ревью ФС SYNTHETIC-FS-1.; Обсудим полноту приложенной ФС без формального согласования. | Разработка новой ФС; approval без точной версии | chat, attachment, work_item, registry, redmine / schemas/evidence.schema.json |
| technical-design | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-review / technical-architect | Подготовь формальный технический дизайн для SYNTHETIC-FS-1.; Объясни варианты технического решения по этому описанию. | ТЗ technical-assignment по шаблону; изменение расширения | chat, attachment, work_item, configuration, extension, redmine / schemas/evidence.schema.json |
| development | formal → flow1c-technical-implementation / technical-architect | Измени расширение по согласованному дизайну SYNTHETIC-FS-1.; Реализуй проверку ввода в текущей ветке расширения. | Read-only ревью; описание технической реализации в документе | chat, work_item, configuration, extension, registry, redmine / schemas/evidence.schema.json |
| technical-implementation | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-implementation / technical-architect | Документируй выполненную реализацию SYNTHETIC-FS-1 формально.; Объясни выполненные изменения по приложенному описанию. | Поручение изменить расширение; отдельный раздел ФС | chat, attachment, work_item, configuration, extension, git_history, git_snapshot, redmine / schemas/evidence.schema.json |
| code-review | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-technical-review / technical-architect | Проверь ветку synthetic-feature и покажи изменения.; Выполни формальное code review рабочего элемента SYNTHETIC-FS-1. | Ревью не разрешает исправление кода; номер задачи не является Git ref | chat, attachment, work_item, configuration, extension, git_history, git_snapshot, redmine / schemas/git-analysis.schema.json |
| testing | explore → flow1c-consultation / —; draft → flow1c-consultation / —; formal → flow1c-testing / tester | Подготовь формальный план тестирования SYNTHETIC-FS-1.; Обсудим тестовые сценарии по этому описанию. | Создание документа произвольного типа по шаблону; выдуманные runtime результаты | chat, attachment, work_item, registry, template_library, redmine / schemas/evidence.schema.json |
| status | formal → flow1c-wiki / — | Покажи состояние рабочих элементов проекта.; Обнови отчёт статуса по существующим manifests.; Найди результаты по согласованию платежей в документации проекта.; Обнови карточку функции в базе знаний и покажи историю изменений. | Публикация формальных ФС и согласования; изменение approvals; статус внешней базы | chat, work_item, registry / schemas/project-knowledge-response.schema.json |
| publish | formal → flow1c-git-gitea / — | Опубликуй проверенный результат SYNTHETIC-FS-1 через PR.; Подготовь PR утверждённой версии спецификации. | Черновик не публикуется; подтверждение не отменяет validation и approvals | chat, work_item, registry / schemas/evidence.schema.json |
| template-management | explore → flow1c-document-templates / —; formal → flow1c-document-templates / — | Добавь приложенный шаблон ТЗ в библиотеку проекта.; Обнови правило заполнения существующего шаблона. | Обновление Flow1C; генерация документа; автоматическая замена пользовательских данных | chat, attachment, template_library / schemas/document-template-operation.schema.json |
| template-document | draft → flow1c-document-templates / —; formal → flow1c-document-templates / — | Создай ТЗ technical-assignment по нашему шаблону.; Подготовь инструкцию пользователя по библиотечному шаблону. | Технический дизайн; formal ФС/тестирование сохраняют специализированный primary | chat, attachment, template_library, redmine / schemas/document-template-validation.schema.json |
<!-- flow1c:routes:end -->
