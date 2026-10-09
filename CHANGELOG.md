# Изменения Flow1C

## В разработке

- Добавлен поясняющий ролик «Flow1C за 80 секунд» с обложкой и ссылкой из README:
  материалы встречи, основания требований, документы по шаблонам, замечания ревью
  и продолжение работы. [Описание и музыкальный кредит](docs/intro-video.md).

- Добавлены логотип с бабочкой и слоган «Анализ — легко. Процесс — в удовольствие.»
  в README. Исходный PNG и SVG для отображения на светлой подложке доступны
  в [материалах оформления](docs/brand.md).

- Добавлены схемы архитектуры: компоненты и границы ответственности, путь
  запроса через OpenCode/gate и разделение репозиториев продукта, документации
  и источников 1С. [Схемы](docs/architecture-diagrams.md) доступны из описания
  архитектуры и README.

- Исправлено создание нового репозитория документации по ранее сохранённому
  пути: явная инициализация пустого репозитория без HEAD выбирает `.flow1c/`,
  включая повтор после прерванной настройки. Существующие данные и layout
  сохраняются; миграция старых проектов не выполняется.

- Обновлены публичный README, быстрый старт, профили и описание навигации/знаний.
  Добавлены MIT License, условия пилотного распространения и шаблоны Issues.
  Основная заявленная среда — Windows/OpenCode; стабильный выпуск не объявлен.

- Исправлено описание Claude-обёртки `flow1c-wiki`: оно совпадает с каноническим
  навыком навигации и базы знаний. В Windows layout fixture Git stdout читается
  как UTF-8, чтобы русские имена файлов не зависели от кодовой страницы runner.

- Добавлены навигация по существующим результатам и Markdown-база знаний
  проекта. Корневой README ведёт к каталогу задач/документов и карточкам функций;
  refresh сохраняет пользовательский текст, persisted paths и завершённые
  результаты. `knowledge --json-stdin` / `flow1c_knowledge` дают ограниченные
  navigation/search/read с версиями Git/local, SHA-256 и continuation, а также
  preview/write карточек и точный commit/PR по поручению пользователя. Wiki
  не меняет approvals и не доказывает внедрение. Проектный status не зависит
  от work-item из сохранённого project reference. [Контракт](docs/project-knowledge.md).

- Новые проекты собирают служебные области документации в `.flow1c/`, оставляя
  четыре пользовательские входные папки и README в корне. Добавлен versioned
  layout v2 с общим resolver для registry, intake, work-items, drafts, sections,
  template library, context и handoff. Git и относительные ссылки сохраняют
  корень documentation_path. Configure и template-only configure создают
  недостающие инструкции; update/resume сохраняют layout и данные. Старые
  проекты работают по прежним путям; автоматический перенос не выполняется.
  [Версии структуры и совместимость](docs/documentation-layout.md).

- Исправлена команда guard/OpenCode тестов Windows CI для Node 22:
  используется поддерживаемый `--experimental-test-isolation=none`.

- Исправлено восстановление Windows/OpenCode update: сохранённый проект больше
  не требует work-item, SSH/HTTPS URL расширения проверяются по общей identity
  policy setup. `flow1c_action action=update-diagnose` возвращает безопасную
  read-only диагностику BLOCKED без credentials и прав на mutation. Повторный
  вызов сохраняет исходный backup и update_id; gate отражает результат updater.
  Существующие незавершённые update gates проходят идемпотентную scope migration
  v1: прежние ссылки/evidence остаются в update_scope_migration как история.
  Завершённые gates и gates других операций не мигрируют.

- Настройка проекта создаёт четыре понятные входные папки: `Материалы встреч`,
  `Шаблоны документов`, `Реестр процессов и требований`, `Документы для анализа`,
  и корневую инструкцию с примерами запросов. Начальный scaffold вынесен из
  configure в `templates/project-documentation/`; добавляются только отсутствующие
  файлы. Исходники пользователя, intake/library и persisted state сохраняют
  прежние контракты; обработка начинается по явному запросу, а не по появлению файла.

- Обязательный CI сокращён до одного Windows job: регрессии, установка/update,
  сохранность задач и guard/OpenCode. Linux и дополнительные матрицы версий
  исключены из текущего объёма. Model evals используются для диагностики
  конкретных проблем; массовая приёмка не является условием текущей поставки.

- Добавлены model routing evals по независимым аннотациям: первое решение,
  отдельные operation/mode/primary/source scores, реальные уточнения до gate,
  baseline regressions и отказ при неполных/fixture-only прогонах. Reports
  фиксируют prompts, hashes, raw events и точные model/variant. OpenCode E2E
  поддерживает явный reasoning variant и общий внешний каталог fixtures.
  Selection acceptance не заменяет исполнение, resume и условия выпуска.

- Исправлены CI fixtures пилота: interview smoke включает route output docs,
  shallow clone явно выбирает main, Redmine credential mock ограничен сервисом,
  а CLI baseline проверяет фактические SHA-256 с учётом LF/CRLF. Исторические
  fixtures и runtime safety checks сохранены.

- Подготовлен публичный пилот установки через агента с отдельного Git clone.
  README и [порядок пилота](docs/pilot-setup.md) описывают setup, профили,
  подключение источников, библиотеку шаблонов и resume. Полная release acceptance
  остаётся открытой.

- Добавлен ContextManifest v1 и явный compact view для role context и свободных
  запросов без work-item. Ограниченный reader принимает manifest entry IDs,
  проверяет SHA-256 оригиналов/извлечений и сохраняет coverage/cursors атомарно.
  Изменения источника/scope отклоняют старый cursor; restart/reinstall сохраняет
  чтение. Compact functional review не завершается до чтения обязательных частей;
  gate остаётся активным для дозагрузки. Legacy full view и публичные fixtures
  сохранены. [Лимиты, контракты и восстановление](docs/context.md).

- Добавлен AgentHandoff v1: immutable версии, managed result paths, SHA-256,
  ссылки на authoritative decisions/evidence/approvals и recovery journal.
  Completion receipt сохраняется перед дописыванием evidence. Повторный complete
  восстанавливает передачу без повторного выполнения этапа; изменённые результаты
  и approvals отклоняются с сохранением завершённого состояния.
- CLI `agent-handoff` и OpenCode `flow1c_handoff` читают/восстанавливают только
  завершённый producer. Legacy transfer помечается incomplete; смена policy
  не превращает чтение исторического результата в разрешение новых действий.
  [Совместимость, миграция и ограничения](docs/handoff.md).

- Подключены bounded read-only `route-catalog` / `route-check` и OpenCode tools
  до gate. Structured begin пересчитывает proposal, отклоняет конфликт до записи
  состояния и сохраняет decision вместе с gate. Legacy defaults/remap сохранены.
- Structured gates перепроверяют proposal при загрузке; старые gates без route
  полей продолжаются. Свободная смена mode через dialogue сохраняет один gate.
- Инструкции каталога генерируются в помеченных блоках canonical/Claude skills,
  AGENTS/CLAUDE и OpenCode controller; CI проверяет drift. Catalog/contracts
  входят в cached adapter identity. Решение не заменяет prerequisites/permissions.
- Исправлено расхождение evidence writer v2 и declaration v1: schema принимает
  обе версии, reader сохраняет данные при нормализации v1 и отклоняет будущие
  версии без перезаписи evidence. Добавлены version/resume regressions.

- Подготовлен самостоятельный продуктовый checkout со своими AGENTS/CLAUDE,
  README, contributor instructions, tests и CI.
- Пользовательские инструкции и установка не зависят от внешнего workspace автора.
- Зафиксированы публичные CLI-контракты перед модульным переносом: parser,
  ошибки JSON stdin, draft lifecycle между процессами и контекст четырёх ролей.
- Завершено модульное разделение: CLI находится в `flow1c.cli`, прежний
  `scripts/flow1c.py` выполняет только запуск из любого рабочего каталога.
- Setup/readiness, registry/work-items, intake/Redmine, documents/templates,
  publication и workflow lifecycle имеют самостоятельных владельцев.
- Сервисы получают пути явно и возвращают данные. Удалены пять передач
  `globals()`, `run_captured`, внутренние вызовы CLI и устаревшая Git-реализация.
- Публичные CLI fixtures, schemas, persisted state, approvals, containment и
  provenance сохранены; структурный перенос не требует миграции данных.
- Добавлена проверка отсутствия циклов/CLI dependencies и импорта без I/O;
  smoke реального Git update проверяет backup, пользовательские данные и
  продолжение прежнего gate в отдельном clone.
- Smoke установки интервью включает новый Python-пакет; compileall в CI
  проверяет `flow1c/` на Windows.
- Добавлена основа route contract: каталог 19 операций, schemas предложения и
  решения, pure policy и явная загрузка продуктовых правил без пользовательского
  состояния. Formal skills/roles берутся из stage policies; решение не выдаёт permissions.
- Зафиксированы 69 синтетических routing cases и 19 legacy default/remap cases,
  версии runtime и расхождение legacy evidence schema v1 с runtime v1/v2.
- Добавлены routing contract regressions и smoke сравнения аннотаций с policy.
  Точность целевых моделей этой детерминированной проверкой не измеряется.

## 0.1.0-dev.1 — 2026-10-06

- Инициализирован самостоятельный Git-репозиторий с веткой `main`.
- Перенесены функциональная основа, schemas, policies, шаблоны и регрессионные тесты.
- Введены собственные имена CLI, skills, OpenCode tools, настроек и credentials.
- Локальные данные, окружение, Git-история и remote других установок не перенесены.
- ТЗ адаптированы к именам Flow1C; определена последовательность следующих этапов.

На дату инициализации модульное разделение, новый каталог маршрутов, handoff
и компактный контекст не были реализованы. Предыдущие отчёты о выпуске
не являются evidence этой версии.
