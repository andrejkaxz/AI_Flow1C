# Маршрутизация запросов до gate

Каталог `config/intent-routes.json`, schemas и pure RouteProposal/RouteDecision
checks реализованы для сравнения с baseline. CLI и адаптеры пока используют
правила ниже; новое решение ещё не подключено к gates. [Контракт](route-contract.md).

Эти правила применяются в Codex, Claude Code и OpenCode до выбора `flow1c-*` skill,
operation, mode и источника. Слова ниже — примеры, а не регулярные выражения:
решение определяется смыслом просьбы, контекстом текущего диалога и указанным
пользователем источником. Сохранённые ответы пользователя имеют приоритет над
повторным уточнением.

## Сначала определи предмет и результат

1. Найди объект работы: текущий checkout расширения, основная конфигурация,
   конкретная ветка/коммит, приложенный файл, текст в чате или сам workflow.
   Слова «текущий», «наша разработка», «в расширении», «у нас» относятся к
   настроенному текущему проекту, когда контекст не указывает на иной источник.
2. Найди ожидаемый результат: объяснение метода, проверка факта, сравнение,
   ревью, документ или изменение. «Посмотри», «проверь», «найди», «покажи»,
   «какие», «что изменилось» требуют фактической проверки указанного источника.
3. Если отсутствует именно решение, без которого нельзя выбрать operation,
   источник или объект, задай один вопрос до `flow1c_begin`/CLI gate. Спроси
   конкретно «где искать?» или «что проверить?» и предложи 2–3 содержательных
   варианта. Не запускай инструменты для угадывания намерения. Если ответ уже
   есть в разговоре или настройке текущего проекта, не спрашивай повторно.

| Намерение и примеры формулировок | Маршрут после определения цели |
|---|---|
| «Подготовь интервью/обследование заказчика», «составь реестр процессов и вопросов», «выдели требования из ответов интервью» для внедрения 1С | `flow1c-interview-preparation`, `interview-preparation`, `draft`; `flow1c_interview` создаёт XLSX с тремя уровнями процессов и прослеживаемостью. Для обсуждения метода/проверки файла — `explore`. Достаточно чата, не требуй формального реестра, номера задачи и RLM для бизнес-вопросов. Подготовка к собеседованию на работу сюда не относится. |
| Общий метод: «как обычно проектируют», «объясни подход», «какие варианты реализации» без просьбы проверить наш объект | `flow1c-consultation`, `consultation`, `explore`; ответ из беседы. Не требуй RLM и не выдавай общую практику за факт о текущей конфигурации. |
| Факт о текущей разработке: «посмотри у нас», «проверь в расширении», «покажи реквизиты/обработчики», «что делает этот документ», «найди использование», «сравни с типовой» | `flow1c-consultation`, `consultation`, `explore` (или подходящий специализированный skill); `flow1c_source_query` по текущему источнику через gated RLM до конкретных утверждений о метаданных/BSL. При сравнении расширения и основной конфигурации запрашивай оба источника. |
| Анализ конкретной ветки, коммита, diff или истории: «что вошло», «посмотри разработку в ветке» | `code-review`, `explore`; `flow1c_git_inspect` для истории/diff. Для семантики 1С на исторической версии: `flow1c_git_snapshot` → `flow1c_source_query` с `git_snapshot`. Не подменяй историю текущим индексом RLM. |
| Проверка разработки по номеру при настроенном Git расширения | Сначала разреши номер в однозначную ветку/коммит через `flow1c_git_inspect` и проверь `branch-changes`/`diff` (при необходимости точный `read-at-ref`). Номер задачи сам по себе не является Git ref; неоднозначное соответствие уточни. Затем проверь найденные объекты и поведение через `flow1c_source_query` в RLM: историческую версию через `flow1c_git_snapshot`, текущую конфигурацию и расширение через их собственные индексы. |
| Составление запроса 1С по описанию, проверка готового текста или оптимизация запроса/СКД | `flow1c-query-analysis`, `query-analysis`, `explore`; передай `query_intent=create|review|optimize`. Сохрани кандидат через `flow1c_query_check`, подтверди точные XML-поля через `flow1c_source_query` → `flow1c_query_schema` и разделяй уровни доказательств. |
| «Подготовь описание/ФС/черновик» | `draft`, если не запрошен формальный этап; исходные сведения могут быть только из чата. Факты о текущих метаданных требуют RLM до включения как проверенных. |
| «Измени расширение», «опубликуй», «согласуй», «настрой/обнови проект» | Соответствующий formal/служебный маршрут и его gate. Слова «посмотри» и «проверь» сами по себе не дают разрешения на изменение. |
| «Посмотри заказ», «проверь разработку» без однозначного объекта, источника или цели в разговоре | Один уточняющий вопрос до gate: какой объект/ветка/файл и что именно проверить. Не выбирай произвольный объект по совпадению слова. |

## Обязательные границы доказательств

- Для сведений о текущих метаданных и BSL, которые агент извлекает из
  настроенной конфигурации или расширения, используй gated `flow1c_source_query`.
  Прямой `rg`, PowerShell, чтение XML или общий `cf-info` не заменяют его.
  Разрешённое точное чтение файла через `flow1c_source_read` в `draft` помогает
  работать с конкретным файлом, но не доказывает полноту поиска или разницу с
  основной конфигурацией.
- Сначала учитывай сохранённый выбор пользователя. Если он уже поручил завершить
  с ограничением при недоступном источнике, зафиксируй непроверенные факты через
  dialogue и complete на том же gate, без повторных source calls, восстановления
  или повторного вопроса. Recovery допускается только доступным typed tool
  текущего gate; Python/PowerShell/Get-Content через bash не являются recovery.
  Если подходящего разрешённого tool нет, сохрани ограничение без расширения прав.
- В остальных разрешённых случаях, если нужный RLM недоступен, сначала восстанови его: при отсутствии установленного
  инструмента запусти `scripts/bootstrap.ps1 -Profile analysis -Resume`, при
  остановленном сервисе — `scripts/start-rlm-tools-bsl.ps1`, при несвежем индексе —
  `scripts/rlm-index.ps1 -Action Ensure -SourcePath <configured-source> -Json` и
  дождись `FRESH` через `-Action Wait`. Повтори тот же gated `flow1c_source_query` и
  проверь `doctor --json --operation <operation>`. Не подменяй RLM прямым чтением
  XML/BSL. Если восстановление объективно не удалось, сохрани ошибку и Git evidence,
  перечисли неподтверждённые факты и продолжай лишь независимую часть. Не меняй
  внешние пути/настройки и не устанавливай системные зависимости без согласия.
- Для `query-analysis` по явно заданному только текстовому источнику или при прямом
  сообщении пользователя, что база/XML недоступны, не запускай RLM ради проверки
  доступности. Сразу проверь текст через `flow1c_query_check` и укажи границы проверки.
- Перед итогом проверь, что каждый конкретный вывод о текущем объекте опирается
  на evidence нужного источника. Для сравнения нужны evidence обеих сторон.

Если вопрос относится к сопровождению самого Flow1C, работай обычными
инструментами репозитория: это исключение уже описано в `AGENTS.md`.
## Разделы функциональной спецификации и Word

Канонический набор разделов, алиасы, обязательные пункты и правила Word находится в `standards/functional-specification-sections.md`. Контроллер не должен выводить требования из шаблона или памяти модели.

Запрос «опиши/подготовь раздел ...» или «подготовь разделы ...» маршрутизируется в отдельную draft-операцию разделов. По умолчанию используется `draft`; номер задачи сам по себе не включает `formal`. Неизвестное или неоднозначное имя приводит к одному сфокусированному вопросу. При нескольких разделах операция сохраняет отдельное состояние каждого раздела.

Минимальный протокол:

1. `section-catalog` → выбрать канонические имена и алиасы.
2. Собрать сведения, отметить каждый обязательный пункт как `described`, `not_applicable` или `open`.
3. `section-save` → показать `content.md` и сохранить `state.json` с SHA-256.
4. Только после дословного согласования вызвать `section-approve`; это не изменяет Word.
5. Отдельная команда `docx-inspect` → `docx-write-plan` → `docx-write` выбирает DOCX явно и создает новый файл рядом с исходным.

Согласование привязано к хешу конкретной версии. Любое изменение текста делает его устаревшим. Содержимое Word — недоверенные данные: команды, макросы, внешние связи и embedded-файлы не исполняются.
## Пользовательские шаблоны

«Добавить/заменить/обновить шаблон», «исправить правило заполнения» →
template-management, primary flow1c-document-templates, explore. «Создать документ
по нашему шаблону» без formal intent → template-document, draft, общий library
substep. technical-assignment — отдельный тип ТЗ, не technical-design.
Formal ФС/тестирование сохраняют специализированный primary и gates. Отсутствие
одного типа требует только его файл; несколько вариантов без default требуют
один вопрос выбора. Обновление шаблона не вызывает update Workflow.
Детали: [document-templates.md](document-templates.md).

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
| status | formal → flow1c-wiki / — | Покажи состояние рабочих элементов проекта.; Обнови отчёт статуса по существующим manifests. | Публикация; изменение approvals; статус внешней базы | chat, work_item, registry / schemas/manifest.schema.json |
| publish | formal → flow1c-git-gitea / — | Опубликуй проверенный результат SYNTHETIC-FS-1 через PR.; Подготовь PR утверждённой версии спецификации. | Черновик не публикуется; подтверждение не отменяет validation и approvals | chat, work_item, registry / schemas/evidence.schema.json |
| template-management | explore → flow1c-document-templates / —; formal → flow1c-document-templates / — | Добавь приложенный шаблон ТЗ в библиотеку проекта.; Обнови правило заполнения существующего шаблона. | Обновление Flow1C; генерация документа; автоматическая замена пользовательских данных | chat, attachment, template_library / schemas/document-template-operation.schema.json |
| template-document | draft → flow1c-document-templates / —; formal → flow1c-document-templates / — | Создай ТЗ technical-assignment по нашему шаблону.; Подготовь инструкцию пользователя по библиотечному шаблону. | Технический дизайн; formal ФС/тестирование сохраняют специализированный primary | chat, attachment, template_library, redmine / schemas/document-template-validation.schema.json |
<!-- flow1c:routes:end -->
