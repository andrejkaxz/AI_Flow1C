# Диалог и независимые черновики

Structured route проверяется до begin; неоднозначный proposal возвращает один
вопрос без создания gate. После begin ответы сохраняются через dialogue на том
же gate. Явная смена `explore`/`draft` обновляет proposal и decision, сохраняя
ответы. Старые gates без route fields продолжаются. [Контракт маршрута](route-contract.md).

Завершённый запрос имеет immutable handoff. Ошибка записи передачи не требует
нового запроса: повторный complete или `agent-handoff --action recover` на том
же gate восстанавливает сохранённый результат. [Recovery и ограничения](handoff.md).

Пользователь может начать с описания задачи в чате, без номера разработки и файлов. Агент уточняет только то, что влияет на ближайший результат. Очевидный вопрос допустим до вызова инструмента. Ответы и существенные решения сохраняются; один и тот же вопрос не задаётся повторно.

| Режим | Результат | Условия |
|---|---|---|
| explore | Консультация или ревью | Описание цели; материалы необязательны |
| draft | Документ UNVERIFIED_DRAFT | Беседа и доступные источники; идентификатор задачи необязателен |
| formal | Результат этапа разработки | Требования, согласования и evidence согласно этапу |

В OpenCode контроллер передаёт режим явно. Для `code-review` без `--mode` CLI выбирает `explore`, если в тексте нет явного намерения формального согласования/публикации, и `draft`, если запрошен документ. Остальные старые вызовы CLI без `--mode` сохраняют formal. Выбор и его инициатор сохраняются в `mode_selection` и `mode_history`.

Составление запроса 1С по описанию, проверка и оптимизация готового текста используют консультационную операцию `query-analysis` в `explore` с явным `query_intent=create|review|optimize`. Она не требует рабочего элемента и не даёт разрешения менять XML, конфигурацию или расширение. `query-check` сохраняет полный кандидат с SHA-256 и сообщает `STATIC`/`PARTIAL` либо `FAIL`, не выдавая его за компиляцию. `flow1c_complete` возвращает именно проверенный текст. Для оптимизации сохраняется исходный `baseline_text`; ускорение остаётся гипотезой без замеров. Проверка при выполнении возможна только через реальную интеграцию 1С; XML, RLM и подключение к обычной СУБД её не заменяют.

## Источники и вопросы

Для числовой ветки, например `11963`, разрешённый пользователем `flow1c_git_refresh` проверяет имена веток `origin`. Он предпочитает точное `11963`, затем `extension_branch_prefix/11963` из локальной настройки, затем единственное совпадение `*/11963`. При нескольких совпадениях возвращаются кандидаты для выбора; ошибка доступа или сети не считается отсутствием ветки. Префикс необязателен и задаётся при `setup-configure` параметром `extension_branch_prefix` (PowerShell: `-ExtensionBranchPrefix`). Старая настройка сохраняется, если параметр не передан.

Файлы и папки принимаются через `flow1c_intake`; свободные запросы сохраняют собственные копии и хеши. Ответы, допущения, решения и открытые вопросы записываются через `flow1c_dialogue`. Чтение/поиск текста workflow и текущего запроса выполняется через `flow1c_inspect`. В режимах `explore` и `draft` `flow1c_git_inspect` безопасно проверяет `git_ref`, читает bounded log и строит diff без checkout, merge или изменения репозитория. `action=integration` принимает до 20 веток в `git_refs`, сопоставляет их с `target_ref` или основной веткой проекта и по полной first-parent topology возвращает `MERGE_FOUND`, `NO_MERGE_COMMIT`, `NOT_MERGED`, `SOURCE_NOT_FOUND` либо `TARGET_NOT_FOUND`; глубина обычного `log` на этот поиск не влияет. Squash/rebase без ancestry не выдаётся за доказанный merge. Устаревший `latest-merge` сохранён для совместимости и означает только последний merge, достижимый из заданного ref. Числовая ветка остаётся `git_ref`, а не становится `task_reference`. В режиме `draft` точный `.bsl`, `.xml`, `.json` или `.md` файл текущего checkout расширения можно прочитать через `flow1c_source_read`; путь берётся только относительно настроенного `extension_path`, канонизируется и остаётся доступным только для чтения. Конкретные сведения о метаданных 1С, основная конфигурация и полнотекстовый поиск по текущему checkout требуют `flow1c_source_query`. Доступность RLM проверяется при обращении к нему, а не перед первым вопросом.

Если `extension_path` не настроен или недоступен, `flow1c_source_read` возвращает `NEEDS_INPUT` с ближайшим действием `flow1c_dialogue`, но сам draft-gate остаётся рабочим: общую часть документа можно продолжать из чата и уже принятых материалов.

В `query-analysis` локальные XML можно исследовать через `flow1c_cc_inspect`. `query-schema` извлекает только явно экспортированные поля одного объекта и табличной части; для конфигурации/расширения путь сначала обнаруживается через gated RLM. Стандартные поля и виртуальные таблицы этим инструментом не подтверждаются. `skd-info` применяется исключительно к `Template.xml`, чтобы получить запросы наборов, поля, параметры и связи СКД. Отсутствие CC-skills, XML или MCP фиксируется как ограничение, после чего статический разбор текста продолжается.

`flow1c_dialogue action=ask` сохраняет вопрос и WAITING_USER. После настоящего ответа `action=answer` возобновляет тот же запрос. `action=record` сохраняет существенные сведения без создания нового gate. После свободного intake не требуется повторный begin. Состояние и ответы переживают перезапуск и сжатие контекста.

При конфликте запроса и указанной ссылки на задачу агент показывает расхождение. Пользователь выбирает исправление ссылки или независимый черновик. Связи требований в реестре автоматически не меняются.

## Хранение и завершение

Черновики располагаются в `drafts/<request-id>` внешнего репозитория документации. До настройки этого репозитория используется `.workspace/drafts/<request-id>` внутри workflow. Внутренний request ID не заменяет пользовательский идентификатор проекта или задачи. `request.json` хранит описание и ответы, `sources/` — принятые материалы, `evidence.json` — их происхождение, `result.md` — итог по умолчанию.

Вложения, полученные до открытия свободного запроса, остаются в `inbox`. После `flow1c_begin` вызов `flow1c_intake` с `promote_intake_id` копирует проверенные оригиналы в `sources/` текущего запроса. Для DOCX/PDF ответ содержит `extraction_status`, `derived_path` или `extraction_error`; готовый Markdown читается через `flow1c_inspect scope=request`. Повтор того же вызова после восстановления MarkItDown повторяет неудачное извлечение без повторной загрузки. Оригинал в `inbox` сохраняется.

`flow1c_complete` с summary закрывает консультацию как CONSULTATION_COMPLETE. Для DRAFT_COMPLETE нужен содержательный записанный файл с маркером UNVERIFIED_DRAFT. Evidence чтения исходника фиксирует происхождение и SHA-256, но не является согласованием или разрешением публикации. Ни один свободный результат не повышает формальный статус.

`flow1c_promote` копирует завершённый черновик вместе с происхождением в `work-items/<safe-reference-slug>/input/drafts/<request-id>` после согласия на конкретную связь и проверки ID требований. Оригинал сохраняется. Утверждённая ФС и manifest не заменяются.

## Формальный процесс и совместимость

Evidence каждого gate хранится в отдельном файле. При завершении формального документа сохраняется снимок входов и результата; для работы с расширением — также Git-состояние. Публикация, включая прямой CLI, повторно сверяет эти данные, статус, необходимые согласования и рецензентов выбранной фазы. `confirmed=true` не превращает BLOCKED в разрешение на публикацию.

Активные gate предыдущей политики требуют повторного `flow1c_begin` с сохранёнными параметрами. Старые evidence не удаляются, но без актуального снимка они не разрешают публикацию. Старые пользовательские ссылки вида `G-104` продолжают читаться как обычные непрозрачные строки.

Изменения самого workflow выполняются обычными инструментами разработки репозитория. Они не требуют искусственного идентификатора задачи и не дают разрешения на изменение внешней конфигурации или расширения.
## Работа без валидированного реестра

Формальный gate без work-item или валидированного реестра возвращает `BLOCKED`, устанавливает `awaiting_user_input=true` и предлагает `provide-reference`, `create-provisional` или `independent-draft`. Решение не фиксируется без реального ответа пользователя. Provisional разрешается только после явного `agent-dialogue --action deviate --deviation-type registry_bypass` с причиной, actor и scope. Обычный `deviate` обход реестра не разрешает.

После подтверждения `agent-action --action provisional-start` принимает пользовательский `task_reference` либо `project_reference` и непустой `user_brief`. Без пользовательского идентификатора доступен только независимый черновик; Flow1C идентификаторы не генерирует.

## Formal с отклонением

Formal-gate с отсутствующими входами возвращает `conditions`: стабильный `id`, категорию, сообщение, источник и признаки `waivable`/`blocking`. Параметр `allow_incomplete_draft` не является авторизацией и не открывает formal-процесс.

После явного решения пользователя контроллер вызывает на том же gate:

```json
{
  "action": "deviate",
  "deviation_type": "process",
  "scope": "gate",
  "condition_ids": ["missing:meeting_materials"],
  "user_statement": "Продолжай формально без материалов встреч; риск принимаю.",
  "answer": "Пользователь осознанно принимает ограничение входных данных."
}
```

Отменяются только уже показанные и разрешённые condition IDs. Новое условие после изменения файлов, статуса или Git state требует нового решения. При полном снятии выбранных блокеров gate переходит в `READY_WITH_DEVIATIONS`, сохраняет `mode=formal` и получает `compliance=DEVIATED`. `flow1c_context`, чтение источника, запись документа и `flow1c_complete` становятся доступны только если их реально разрешает текущий state-aware `available_actions`.

Формальный документ с таким решением автоматически содержит `UNVERIFIED_DRAFT` и завершается только как `COMPLETE_WITH_DEVIATIONS` с `ready=false`. Это не согласование, не approval и не разрешение публикации. Отсутствующий/пустой output, нарушение path safety, изменение обязательных проверок и другие ошибки целостности приводят к `NON_COMPLIANT`.
## Git analysis dialogue protocol

For a branch integration question, call `flow1c_begin` once with all `git_refs`, a `target_ref`, and explicit refresh intent when present. Refresh at most once, then perform one batch `merge-search`. A terminal result blocks `latest-merge` and an equivalent repeat; an identical fingerprint returns its saved evidence.

Use `branch-changes` for unique source commits. Use `diff` names/stat before a patch. For a large patch, repeat `diff` with the returned `next_cursor` until it is null: a cursor resumes the same file when its patch exceeds the page limit. Concatenate `content` in cursor order to review the entire patch; do not infer that a truncated page is the end of a file. Use `read-at-ref` with `start_line` for exact historical context. Saved tool-output files are not part of the allowed repository roots and must not be read through bash/grep. When Git is configured and a development number is supplied, resolve its branch/commit and inspect Git evidence first. Then confirm 1C behavior with RLM; if the requested commit is not the current checkout, create one snapshot and query RLM with its typed selector. If RLM is unavailable, recover its installation, service or indexes and retry the same query. A failed recovery does not invalidate Git evidence; finish with a partial, evidence-linked answer and the specific remaining limitation.

`NO_INTEGRATION_EVIDENCE` means only that the available fresh, non-shallow evidence sources did not prove integration. `REFRESH_REQUIRED`, shallow history, unavailable PR evidence and partial patch coverage must be reported as limitations instead of being rewritten as “not merged”.
## Draft раздела и отдельное согласование Word

Фраза «согласовано» фиксирует только текущий хеш раздела через `section-approve`; сама по себе она не является разрешением на изменение Word. Перед записью пользователь отдельно запрашивает заполнение документа. Если раздел изменен хотя бы на один символ, требуется новая версия и новое согласование.

Для нескольких разделов состояние, checklist, approval и ошибки ведутся независимо. При заполненном разделе агент сначала показывает существующий текст и запрашивает режим `replace` или `append`; при неоднозначном разделе или нескольких DOCX выбор автоматически не делается.

При записи DOCX адаптер наследует оформление абзаца и символов из существующего текста выбранного раздела. Для Markdown-таблицы он использует таблицу-образец этого раздела: стиль, границы, ширины столбцов и оформление ячеек. Число столбцов должно совпадать; иначе возвращается `DOCX_LAYOUT_UNSUPPORTED` с указанием подготовить подходящий шаблон. Если образца таблицы нет, создаётся таблица в стиле `Table Grid` исходного документа. `docx-write` проверяет структуру и текст, но возвращает `WRITTEN_LAYOUT_UNVERIFIED`: перед передачей заказчику результат нужно открыть в Word или отрендерить и визуально проверить переносы, страницы и таблицы.
## Template continuation

Template requests retain operation_id, answers, unresolved questions and
revision pins. Waiting for a source or material clarification does not complete
the request. After the answer, resume the same intake and original document
request. Load the saved profile and generated guide in a new chat. Never ask for
the unchanged source again. A user statement correcting a rule creates a new
profile revision; existing drafts keep their pin. Details:
[document-templates.md](document-templates.md).
