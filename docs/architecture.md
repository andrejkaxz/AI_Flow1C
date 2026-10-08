# Архитектура Flow1C

Визуальное представление: [схемы компонентов, пути запроса и границ
репозиториев](architecture-diagrams.md). Схемы отражают текущую реализацию;
контракты и подробности приведены ниже.

Документ описывает текущую реализацию самостоятельного продуктового репозитория.
`scripts/flow1c.py` — совместимый запускатель `flow1c.cli.main` из любого cwd.
Parser, представление результата и exit codes находятся в `flow1c.cli`;
бизнес-операции принадлежат сервисам пакета. Каталог и чистая проверка routes
реализованы отдельно. Read-only CLI/tools проверяют proposal до gate; begin
пересчитывает и сохраняет решение, gate сохраняет прежние проверки полномочий.

`flow1c.handoff_policy` владеет чистыми identities и контрактом передачи.
`flow1c.handoff` сохраняет completion receipt, проверяет sealed results и
authoritative records, пишет неизменяемые версии/журнал и восстанавливает
передачу. Completion фиксируется перед дописыванием evidence; повторный вызов
не повторяет завершённые действия. Терминальный producer остаётся историческими
данными после смены policy, а новые действия проверяются текущим gate.
[Хранение, восстановление и ограничения](handoff.md).

`flow1c.context_policy` выбирает управляемые документы и определяет identities,
обязательные диапазоны, coverage и бюджеты без I/O. `flow1c.context_manifest`
сохраняет immutable manifests/coverage, проверяет provenance и обслуживает
ограниченный reader по entry IDs. Один atomic evidence reference фиксирует
покрытие после каждой страницы. Контекст и completion разделяют существующую
OS writer lock; services возвращают данные без вызовов CLI. Legacy full view
сохранён, compact включается явно. [Контракт, полнота и resume](context.md).

`flow1c.routing_policy` проверяет структурированный RouteProposal и возвращает
детерминированный RouteDecision без I/O и permissions. `flow1c.routing` читает
только продуктовый каталог и stage policies. Formal skill/role принадлежат
`config/stages.json`; prerequisites и allowlists в routes не копируются.
Источник содержит запрошенную версию и resolver, а доказанные commit/freshness
появляются только в gated source tools. [Контракт и ограничения](route-contract.md).

## Границы Python-пакета

`flow1c/` содержит самостоятельные сервисы:

- `errors.py` — общий `WorkflowError` с прежним контрактом CLI-ошибок;
- `storage.py` — чтение и запись JSON/текста, атомарная замена, обработка Unicode,
  SHA-256, containment и проверка symlink/reparse points;
- `documentation_policy.py`, `documentation.py` — versioned layout, корень
  служебных данных и non-destructive scaffold документации.
- `context.py` — `RuntimeContext` с корнем checkout и загруженной конфигурацией,
  явная загрузка настроек и выбор корня пользовательской документации;
- `results.py` — данные операции и код результата; форматирование выполняет CLI.
- `system.py`, `git_runtime.py`, `sources.py` — проверки executable/source paths,
  Git документации и ограниченный RLM transport.
- `setup.py`, `readiness.py` — setup checkpoints, запуск setup/update и readiness.
- `update_policy.py` — идемпотентная миграция незавершённого update scope;
  `update_diagnostics.py` — ограниченное чтение настроенных Git metadata без
  секретов, fetch и mutation. Update gate не зависит от work-items.
- `registry.py`, `work_items.py` — импорт/разрешение реестра, безопасные пути,
  manifest, создание и reconciliation рабочих элементов.
- `intake.py`, `redmine.py` — intake/provenance и транзакции Redmine credentials.
- `documents.py`, `templates.py` — разделы, DOCX, интервью и lifecycle шаблонов.
- `publication.py` — status, validation snapshots, commit и PR.
- `workflow/state.py` — gate/request/evidence persistence и tool authorization.
- `workflow/begin.py`, `dialogue.py`, `complete.py` — assessment, ответы/resume,
  ролевой контекст и проверка завершения.
- `workflow/actions.py`, `git_actions.py`, `source_actions.py` — разрешённые
  действия, Git evidence и source/query/BSL evidence соответственно.

Импорт этих модулей не читает пользовательские файлы, не меняет потоки CLI и не
запускает процессы. Они работают без optional integrations. Сервисы не импортируют
CLI. `context-build` и `agent-context` используют одну операцию построения;
`agent-context` сохраняет отдельную копию с SHA-256 в evidence своего gate.
Сервисы получают `product_root` явно и возвращают данные или предметную ошибку.
Необязательные библиотеки загружаются при выполнении нужной операции.
Все специализированные модули импортируются через `scripts.<module>`.

Передачи `globals()`, внутренние вызовы `cmd_*` и обмен через stdout удалены.
Readiness, registry import, создание work-item и публикация используются напрямую
как сервисы. Шаблонные операции импортируют конкретных владельцев gates,
sections и setup checkpoints; контейнера API нет. Направления импортов и импорт
всех runtime-модулей без I/O/optional packages защищены регрессиями.
Форматы хранения, schemas, policy versions, CLI options и exit codes
не изменены. Контракты parser, draft lifecycle и ролевого контекста защищены
fixtures в `tests/fixtures/cli-baseline/`.

## Границы репозиториев

Flow1C содержит правила, навыки, схемы, скрипты и шаблоны. Документация проекта и исходники расширения живут в своих Git-репозиториях. Полная выгрузка конфигурации остаётся на компьютере пользователя.

```text
Flow1C repo
    ├─ rules, skills, CLI, schemas
    └─ bootstrap
          ├─ project documentation repo
          │     ├─ README.md (user entry point)
          │     ├─ Материалы встреч
          │     ├─ Шаблоны документов
          │     ├─ Реестр процессов и требований
          │     ├─ Документы для анализа
          │     └─ .flow1c (layout 2; прежний layout хранит области в корне)
          │           ├─ layout.json
          │           ├─ inbox, registry, document-templates
          │           ├─ work-items/<safe-reference-slug>
          │           └─ drafts, requests, wiki, .workspace
          ├─ extension source (Git clone or local XML/BSL export)
          └─ configuration XML/BSL (local path only)
```

Пользователь передаёт исходные материалы через четыре понятных входных папки;
их структура задана в `templates/project-documentation/`. Configure создаёт
недостающие файлы через `scripts/project-documentation.ps1` без замены
существующих документов. Intake, registry и template library используют
версионированный служебный корень после явного запроса пользователя; помещение файла
во входную папку не запускает обработку. [Входные материалы](project-materials.md),
[версии структуры и совместимость](documentation-layout.md).

## Модель данных MVP

- бизнес-процесс связан с нолём или несколькими требованиями;
- рабочий элемент с непрозрачным идентификатором пользователя объединяет одно или несколько требований;
- одно требование не может входить в две ФС;
- `project_reference` и `task_reference` задаёт пользователь; workflow их не генерирует и не ограничивает legacy-форматом;
- источник ID требования — колонка `J` листа реестра процессов и требований.

## Управление контекстом

Навигация по результатам и база знаний используют обычный Markdown в служебном
корне `wiki/`. `flow1c.navigation` читает существующие manifests/requests и
документы, обновляет только собственный каталог и управляемые ссылки README.
`flow1c.knowledge_policy` задаёт чистые правила paths, ранжирования, разделов,
ограничений и continuation; `flow1c.knowledge` читает ограниченные Git/local
snapshots и сохраняет проверенные Markdown. `workflow.knowledge_actions`
соединяет их с текущим gate и точной публикацией выбранных файлов. CLI и
OpenCode обращаются к одному сервису. Импорт не выполняет I/O, постоянного
индекса и дополнительного approval lifecycle нет. Поиск/чтение доступны ролям,
wiki mutations — готовому formal status gate по поручению пользователя.
Git транспорт PR общий с публикацией ФС, требования приёмки ФС не ослаблены.
[Контракт, версии источников и ограничения](project-knowledge.md).

Импортёр преобразует Excel в маленькие JSON-индексы. Для конкретной задачи CLI формирует ролевой context pack. Агент не должен читать весь Excel, всю ФС, всю конфигурацию и весь diff в один контекст.

В OpenCode запрос поступает в `flow1c-controller`. Уточняющий вопрос допустим до gate. Режимы explore и draft используют чат и необязательные материалы; formal применяет условия этапа. Чистая функция оценки и переходы диалога находятся в `scripts/flow1c_policy.py`, I/O — в `flow1c.workflow`, CLI — в `flow1c.cli`. Ограниченное чтение доступно через flow1c_inspect; конфигурация — через RLM. Подробности: [диалог и черновики](dialogue.md).

Следующие пути указаны относительно служебного корня документации. Материалы без назначенной задачи сохраняются в `inbox/<intake-id>/`. После назначения ссылки оригиналы попадают в `work-items/<safe-reference-slug>/input/meetings/` или `input/attachments/`, производный текст — в `input/derived/`, а хеши и категории — в `input/artifacts.json`. Исходная ссылка сохраняется в manifest и никогда не используется как путь напрямую.

| Роль | Минимальный контекст |
|---|---|
| Аналитик | снимок требований, артефакты встреч, решения, шаблон ФС |
| Функциональный архитектор | требования, трассировка, чистая ФС, связанные решения wiki |
| Технический архитектор | утверждённая ФС, техрешение, diff расширения, точечные выборки конфигурации |

## Внешние инструменты

- MarkItDown — первичное извлечение текста из Office/PDF. Excel-реестр импортируется структурно, не через Markdown.
- rlm-tools-bsl — обязательный MCP-сервис и индексы для точечного поиска в больших XML/BSL-выгрузках конфигурации и расширения.
- cc-1c-skills — операции над объектами 1С, формами, ролями, СКД и расширениями. В консультационном `query-analysis` контроллер может загрузить только информационные `meta-info`/`skd-info` и вызвать их через ограниченный `flow1c_cc_inspect`; локальная XML-выгрузка не считается проверкой базы. Чистая политика `flow1c_query_policy.py` анализирует только поддерживаемые признаки текста; `flow1c_query_schema.py` извлекает точные поля XML без поиска по совпадению строки. CLI сохраняет версию XML и хеш кандидата, а завершение сверяет их повторно.
- BSL Language Server — обязательный статический анализ для code review, если инструмент настроен.
- Redmine — необязательный read-only источник задач и вложений. Его API key хранится вне репозитория; полученные файлы проходят тот же контролируемый intake и сохраняют source provenance.
- SonarQube — отложен за пределы MVP.

Абсолютный путь к репозиторию документации хранится только в `.flow1c.local.json`. CLI использует его для Git и пользовательских материалов; registry, work-items, wiki и другие хранилища используют отдельный versioned data root; сам Flow1C остаётся неизменяемым набором правил и инструментов.

Evidence изолировано по gate_id. Состояние диалога и ответы хранятся на диске; guard восстанавливает их после перезапуска. Независимые запросы сохраняются отдельно от work-items и присоединяются с provenance без изменения согласований.

## Structured conditions и deviations

Formal gate хранит не только legacy `errors`/`missing_*`, но и детерминированный массив `conditions`. Решение пользователя ссылается на конкретные IDs, уже присутствовавшие в gate; будущие условия автоматически не покрываются. Policy этапа явно задаёт `allow_formal_documents` и `waivable_categories`. `path_safety`, `publication`, `external_confirmation` и mutation extension остаются hard-coded non-waivable контролями.

`READY_WITH_DEVIATIONS` означает продолжение только документной или read-only части formal этапа. Watermark `UNVERIFIED_DRAFT`, `compliance=DEVIATED` и `ready=false` обязательны. Integrity error закрывает gate как `NON_COMPLIANT`; допустимые недостатки остаются в `completion_errors`. Такой результат никогда не меняет status/approvals и не создаёт validation snapshot для публикации.
## Traceability modes

Registry-backed work-items используют нормализованный snapshot. Provisional work-items хранят исходное пользовательское описание и его SHA-256 в `requirement_basis`; registry имеет статус `bypassed` и запись явного согласия. Stage policy описывает альтернативные входы через `input_sets`, поэтому выбор источника выполняется по manifest и не позволяет молча подменить невалидный реестр пользовательским текстом.
## Reliable Git analysis boundary

Git domain policy is isolated in `scripts/flow1c_git_policy.py`; it owns statuses, evidence ranking, final-merge selection, semantic fingerprints, state transitions and v1-to-v2 compatibility without performing I/O. `scripts/flow1c_git.py` owns argument-array Git execution, full ref resolution, targeted refresh, topology/history/patch analysis, paginated diffs, historical blob reads and atomic tree export. `scripts/gitea_client.py` is a read-only, timeout-bounded PR evidence boundary. `flow1c.workflow.git_actions` connects these components to gates and persisted evidence; `flow1c.cli` presents their results.

The selected snapshot backend is Git tree export rather than a linked worktree. Files are copied from verified blobs into a temporary directory and the complete snapshot is atomically activated below `.workspace/git-analysis/<request>/<commit>/`. This keeps the analyzed checkout's HEAD, index, worktree and Git configuration untouched and avoids worktree administration state. Snapshot identity includes repository identity, commit/tree and requested paths; current configuration, current extension and historical snapshots therefore cannot share an indistinguishable RLM source key.

Git evidence records use schema version 2. Legacy `integration` is normalized to `merge-search`, while its response retains old `status`, `merge_commit`, `review_ref` and diff-base fields. Old `NOT_MERGED` records migrate to `NO_INTEGRATION_EVIDENCE` with `legacy_status=NOT_MERGED`; old `NO_MERGE_COMMIT` migrates to `FAST_FORWARD`.

### Acceptance evidence map

| Criteria | Primary regression evidence |
|---|---|
| AC-01,03,04 | `tests/test_git_analysis.py` topology, post-merge and multiple-merge fixtures |
| AC-02 | target resolver preference and refresh contract tests |
| AC-05,06 | branch delta/reference parser and object change-set tests |
| AC-07,08 | first-parent ranges and verified diff cursor tests |
| AC-09 | historical `read-at-ref` test |
| AC-10,11 | snapshot idempotency/provenance and source-selector tests |
| AC-12,13 | `tests/test_guard.mjs` safe pipeline and injection corpus |
| AC-14 | fingerprint/state transition tests and eval tool-call budgets |
| AC-15 | 20-run target-model report from `scripts/run-opencode-evals.ps1` |
| AC-16 | full Python/Node/doctor/bootstrap/update commands |
| AC-17,18 | schema, adapter and legacy dialogue contract tests |
## Разделы функциональной спецификации

Подсистема разделов разделена на три границы: `flow1c_sections_policy.py` содержит чистые правила каталога, полноты, SHA-256 и состояний; `flow1c_sections.py` сохраняет версии и audit evidence; `flow1c_docx.py` является единственным адаптером чтения/изменения OOXML. CLI связывает их, но не дублирует смысловые правила.

Файлы draft: `.workspace/drafts/<request-id>/sections/<section-id>/` при отсутствии documentation repository либо `drafts/<request-id>/...` относительно служебного корня настроенной документации. Для work-item используется `work-items/<safe-reference-slug>/specification/sections/<section-id>/`. Исходный DOCX не перезаписывается.
## Document template modules

`config/document-types.json` registers types and product policy references. The
catalog does not contain user rules. `flow1c_templates_policy.py` owns pure
selection/coverage/content policy; `flow1c_templates_store.py` owns transactions,
containment, integrity and relocation; `flow1c_templates_extract.py` dispatches
bounded format parsing in an isolated process. `flow1c_markdown.py` handles
CommonMark/GFM ranges and `flow1c_docx.py` exposes the generic API with OOXML
mechanics in `flow1c_docx_templates.py`. `flow1c_templates.py` orchestrates these
services; `flow1c.templates` integrates shared gates. Agent adapters only
interpret user intent/content and call these contracts. Adding/removing catalog
types does not alter storage, selection or generation code or delete saved data.
