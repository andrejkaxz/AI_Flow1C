# Установка и восстановление

Compact context и прочитанные диапазоны сохраняются рядом с evidence запроса.
После рестарта продолжите выданный cursor на том же gate; после смены источника
снова выполните intake и перестройте compact context. [Дозагрузка и полнота](context.md).

Передача завершённого результата использует сохранённый completion receipt;
повторять его действие после сбоя не требуется. [Handoff/recovery](handoff.md).

После обновления route tools перезапусти OpenCode и продолжи по сохранённым IDs.
Старые gates и evidence сохраняются; [совместимость маршрутов и evidence](routing-update.md).

Flow1C настраивается по требуемому уровню работы. Обычный update не создаёт setup и не требует повторного подтверждения корректных значений.

## Профили

| Профиль | Возможности |
|---|---|
| `conversation` | CLI, консультация, независимый черновик |
| `project-basic` | `conversation` + Git и подключённый проект |
| `documents` | `project-basic` + Excel/DOCX/PDF |
| `analysis` | `documents` + RLM; рекомендуемый профиль аналитика/архитектора |
| `implementation` | `analysis` + BSL Language Server |
| `full` | `implementation` + отдельно подтверждённый `cc-1c-skills` |

`full` никогда не выбирается неявно. Отсутствие необязательной capability не блокирует операции другого профиля.

## Управляемый setup

Первый запрос во всех поддерживаемых агентах: «Подготовь проект к работе». Если уровень не указан, агент задаёт один вопрос о профиле и рекомендует `analysis`. Далее пользователь принимает не более двух сводных решений: подтверждает все пути/URL и точный план установок.

Аудит не меняет окружение и всегда выдаёт JSON:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\setup-state.ps1 -Json -Profile analysis
```

План bootstrap не пишет файлы и не обращается к сети:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -Profile analysis -Plan -Json
```

После подтверждения агент запускает тот же скрипт без `-Plan`. Для корпоративной offline-установки используется абсолютный `-Wheelhouse ... -NoIndex`. Поддерживается Python `>=3.10` без верхней границы; при автоматической установке предпочтительна версия 3.14, а launcher сначала выбирает новейшую установленную версию Python 3.

Подтверждённые несекретные значения и завершённые шаги атомарно сохраняются в `.workspace/setup/<setup-id>.json`. Токены, пароли и ключи туда не записываются. После перезапуска агент продолжает через `setup-status` или `setup-resume`; неизменившиеся значения повторно не подтверждаются.

## Подключение проекта

`configure-project.ps1` получает `-Profile`, `-SetupId`, `-Confirmed` и сводный набор значений. Идентификатор проекта передаётся как `-ProjectReference`; это непрозрачная строка пользователя. Пустой локальный репозиторий документации можно инициализировать только с явными `-InitializeDocumentationRepository -Confirmed`. URL репозитория и флаг инициализации взаимоисключающие.

Скрипт отклоняет относительные и вложенные в Flow1C внешние пути, сверяет Git origins и не изменяет конфигурацию 1С либо исходники расширения. Новая `.flow1c.local.json` пишется через временный файл только после проверки; при сбое предыдущая рабочая версия восстанавливается.

Для пользователя configure создаёт `Материалы встреч`, `Шаблоны документов`,
`Реестр процессов и требований`, `Документы для анализа` и корневой README
с примерами запросов. Положите исходные файлы в подходящий раздел и попросите
агента выполнить анализ, импорт или настройку шаблона.
[Назначение папок и порядок обработки](project-materials.md).

В новых проектах служебные области находятся внутри `.flow1c/`;
существующие проекты сохраняют прежнее расположение. Версия задаётся
`.flow1c/layout.json`. [Контракт структуры](documentation-layout.md).

Принятые агентом материалы без задачи сохраняются в `inbox/<intake-id>/`
относительно служебного корня.
После назначения пользовательского `task_reference` они перемещаются в
безопасно нормализованный каталог `work-items/<work-item-slug>/input/`;
оригинальный идентификатор остаётся в manifest. Пользовательские исходные
файлы и прежние служебные пути не переименовываются.

## Фоновая индексация

Для `analysis` и старших профилей configure вызывает `rlm-index.ps1 -Action Ensure`. Свежий индекс используется сразу; отсутствующий или устаревший строится отдельным процессом. Configure возвращает `WAITING_BACKGROUND` с `setup_id`, PID и путями к логам. Повторный `Ensure` не создаёт дубль, а resume продолжает setup после `FRESH`.

Пока индекс строится, доступны `conversation` и операции `documents`, не использующие запросы к 1С. Совместимый общий endpoint на `127.0.0.1:9000` принимается после проверки health и набора MCP tools; неизвестный процесс отклоняется.

## Проверка готовности

Готовность определяется для профиля или операции:

```powershell
.\.venv\Scripts\python.exe scripts\flow1c.py doctor --json --profile analysis
.\.venv\Scripts\python.exe scripts\flow1c.py doctor --json --operation functional-spec
```

Legacy `doctor --json` сохранён. Его исходные поля `schema_version`, `state`, `ready`, `checks` не переименованы.

Если пользователь отказывается от условия документного gate, агент записывает причину через `flow1c_dialogue`/`agent-dialogue` с action `deviate` и продолжает с видимым статусом `READY_WITH_DEVIATIONS`. Результат остаётся `UNVERIFIED_DRAFT` и завершается как `COMPLETE_WITH_DEVIATIONS`. Если отклонена сама формальная привязка, работа безопасно продолжается как независимый `DRAFT_COMPLETE`. Отклонение не отменяет ограничения публикации, approvals/evidence, границ путей или записи в расширение.

## ZIP и prerequisites

ZIP можно использовать для offline bootstrap Git/Python и профиля `conversation`. Отсутствие `.git` или origin отражается в JSON-диагностике. Для update нужен Git checkout; агент не превращает непустую ZIP-папку в репозиторий и не заменяет её без отдельного подтверждения. Установки через WinGet или организационные installer-файлы выполняются только после явного разрешения и без скрытого повышения прав.

`cc-1c-skills` устанавливается только отдельным opt-in:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -Profile full -InstallCc1cSkills
```
## Git-analysis readiness

Setup validates `config/git-read-policy.json`, the OpenCode guard contract and schema version 2 before Git analysis is exposed. Network refresh is never a hidden prerequisite: it requires explicit user intent in the active gate. Gitea reuses the existing `gitea.token_env` credential source; no second token store is created. Run `doctor --json --operation code-review` to inspect only the capabilities relevant to this workflow.

The BSL Language Server download is bounded to 180 seconds. A timeout or checksum mismatch removes the partial archive, preserves the existing virtual environment and local configuration, and returns a recoverable bootstrap error. Rerun the same profile with `-Resume`; omit `-Json` when detailed download diagnostics are needed.
## Необязательная библиотека шаблонов

После выбора профиля предложите все типы из `config/document-types.json` одним
шагом. Принимайте частичный набор файлов; для каждого сохраняйте operation_id,
готовность, вопросы и defer в setup checkpoint. Ошибка одного файла не отменяет
остальные. Resume использует сохранённые IDs без повторного запроса файлов.
Пустая библиотека не блокирует setup. Предложите импорт настроенного legacy
`functional_spec_template`, сохранив прежний вход при отложенном импорте.

Общий контракт: [document-templates.md](document-templates.md). Для библиотеки
без подключения проекта откройте template-management gate и настройте только
папку документации через template configure; Git/1С/RLM не требуются. Сетевые
действия bootstrap выполняются в рамках согласованного плана выбранного формата.
