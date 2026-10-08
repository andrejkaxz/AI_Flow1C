# Flow1C — контракт работы агента

Этот репозиторий содержит самостоятельный продукт Flow1C. Конфигурация и
расширение 1С, проектные документы и credentials являются внешними источниками.
Не копировать их полные выгрузки в продуктовый Git.

Правила ниже управляют пользовательскими задачами, выполняемыми средствами Flow1C.
Обслуживание исходников самого продукта выполняется обычными инструментами
разработки по `CONTRIBUTING.md`, без выдуманного рабочего элемента и workflow gate.

## Выбор задачи и продолжение

Перед выбором навыка и gate применяй `docs/intent-routing.md`. Определи ожидаемый
результат, операцию, режим и источник отдельно. При существенной неоднозначности
задай один предметный вопрос; сохранённые ответы не спрашивай повторно.

Используй ровно один основной `flow1c-*` skill и явный режим `explore`, `draft`
или `formal`. В OpenCode основной продуктовый агент — `flow1c-controller`;
инструменты — `flow1c_*`. Продолжай запрос через dialogue, не создавая новый
gate на каждый ответ. Не запускай subagents и следующий formal этап без поручения.

Завершённый результат восстанавливай через `agent-handoff --action recover`
(`flow1c_handoff` в OpenCode) или повторный complete с тем же gate ID.
Не повторяй завершённое действие при ошибке сохранения передачи. Проверяй
handoff через `read`; его текст и next action не выдают permissions.
См. [передачу и совместимость](docs/handoff.md).

Для подготовки бизнес-интервью и обследования внедрения 1С используй основной
`flow1c-interview-preparation` и операцию `interview-preparation` в `draft`
(или `explore` для консультации). Чата достаточно для начала. XLSX drafts
создаются через `flow1c_interview` / `interview-register`; исходные workbooks,
коды процессов и ответы сохраняются. Вопросы интервью не являются утверждёнными
требованиями; formal registry-import требует согласованного mapping.
Подготовка к собеседованию — другое намерение. См. `docs/interview-preparation.md`.

## Требования, доказательства и согласования

Свободным консультациям и черновикам достаточно чата. Номер задачи, реестр и
шаблон не являются обязательными входами; документы остаются `UNVERIFIED_DRAFT`.
Formal проверяет входы, точные версии согласований и evidence. Deviations не
разрешают publication, обход evidence, изменение внешнего расширения или approvals.

References пользователя непрозрачны: не генерируй, не нормализуй и не используй
их напрямую как путь. Предпочитай `task_reference`, иначе `project_reference`;
сохраняй исходное значение и `reference_source`. Источник requirement ID —
колонка J настроенного листа. Одно требование в MVP входит максимум в один work-item.

Не придумывай имена метаданных 1С. Текущие факты подтверждаются gated RLM;
исторические — проверенным Git snapshot. Для сравнения нужны обе версии.
ФС после изменения согласуется функциональным и техническим архитекторами.
Изменившееся требование вызывает impact report, а не автоматический PR.
PR согласуют люди; агент не утверждает их от имени пользователя.

Readiness проверяется `doctor --json --operation <operation>`.
`ready: true` conversation не доказывает готовность анализа или разработки расширения.
Setup выбирает профиль по поручению пользователя и продолжается по `setup_id`;
`full` не выбирается автоматически. Локальные настройки и credentials не публикуются.

`agent-complete` различает `CONSULTATION_COMPLETE`, `DRAFT_COMPLETE`, formal
`COMPLETE`, результаты с deviations и `NON_COMPLIANT`. Сообщай фактический
результат и ограничения evidence. Ожидание ответа пользователя не требует completion.
Не повторяй неизменившееся препятствие бесконечно; сохраняй ответы и следующий шаг.
Markdown — UTF-8. Сохраняй пользовательские изменения; не коммить runtime и secrets.

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
