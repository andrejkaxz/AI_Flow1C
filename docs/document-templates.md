# Пользовательские шаблоны документов

Библиотека сохраняет шаблон, структуру, профиль и инструкцию заполнения для
повторного использования в новых чатах. Это сохранённые правила конкретной
ревизии, а не дообучение модели. Типы задаются в `config/document-types.json`:
протокол встречи, ФС, ТЗ, протокол тестирования и инструкция. Добавление или
удаление типа из каталога не удаляет сохранённые варианты, историю и pins.
Идентификаторы типов стабильны; переименование отображаемого названия безопасно.

Можно сказать агенту «добавь наш шаблон протокола», «составь документ по нашему
шаблону», «замени шаблон ФС» или «исправь правило заполнения». Файл достаточно
передать один раз. При нескольких вариантах без default агент спрашивает выбор.
Обновление шаблона не запускает обновление самого Workflow.

## Папка и зависимости

Укажите существующую абсолютную папку пользовательской документации вне всех
checkout Workflow. Git, RLM, 1С и установленный Word для библиотеки не нужны.
`template configure` сохраняет этот путь, не инициализируя Git. Библиотека лежит
в `<documentation_path>/document-templates/`, результаты — в `drafts/<gate-id>/`.
Существующая папка используется повторно. Недоступный путь даёт ошибку,
автоматического переноса в `.workspace` нет. Шаблоны не публикуются автоматически.

Markdown: `bootstrap.ps1 -Profile template-markdown -Plan -Json`; после согласования
плана — та же команда без `-Plan`. DOCX: профиль `template-docx`. Markdown parser:
`markdown-it-py==4.2.0`, CommonMark с GFM-таблицами. JSON Schema: jsonschema 4.x.
DOCX использует общий `flow1c_docx` API и python-docx 1.x. Эти профили устанавливают
только необходимые библиотеки в `.venv`, без Git/RLM/Office приложения.

```powershell
.\.venv\Scripts\python.exe scripts\flow1c.py doctor --json --operation template-management --format markdown
.\.venv\Scripts\python.exe scripts\flow1c.py doctor --json --operation template-document --format docx
```

Пустая библиотека допускается readiness. Готовность записи проверяется отдельно
по пригодной ревизии и заполненному профилю. DOCX renderer не устанавливается
автоматически: визуальная проверка первой реализации имеет `UNVERIFIED`.

## Машинный контракт

В OpenCode импорт выполняется через отдельный `flow1c_template` с `gate_id`
текущего setup; открывать второй `template-management` gate для этого подшага
не требуется. `flow1c_action action=template` не поддерживается. Для отдельных
запросов на шаблоны используются операции ниже.

Если gate перечисляет `flow1c_template`, но инструмента нет в наборе OpenCode,
проверьте, обновлялся ли Workflow после запуска приложения. OpenCode может
сохранять загруженный модуль инструментов, пока работает экземпляр проекта.
Полностью закройте и откройте OpenCode с тем же проектом и продолжите сохранённые
`gate_id`, `setup_id` и `operation_id`. Новый setup и повторный импорт уже
сохранённых операций не нужны. Не подменяйте отсутствующий инструмент
`flow1c_action` или прямым Python через `bash`: ограничения этих маршрутов ожидаемы.
Адаптер возвращает `OPENCODE_RESTART_REQUIRED`, если его файлы изменились
после загрузки. Пустая библиотека и `doctor ready=true` подтверждают зависимости,
но не наличие инструментов в уже работающей сессии OpenCode.

Начать запрос: `agent-begin --operation template-management --mode explore
--summary "Добавить шаблон"` либо `template-document --mode draft`.
Дальше передавать JSON в `flow1c.py template <action> --json-stdin`:

```json
{
  "gate_id": "идентификатор из agent-begin",
  "request": {
    "source": "абсолютный путь к переданному файлу",
    "document_type": "meeting-minutes",
    "variant_name": "Корпоративный"
  }
}
```

Для `configure`: `request.documentation_path`. Для `intake`: `source`,
`document_type`, optional `operation_id`, `variant_name`, `resources_root`.
Обновление: также `template_id`, `parent_revision` с ожидаемой active revision.
Correction-only использует сохранённый исходник той же ревизии и новый профиль.
Повтор operation_id возвращает checkpoint; другой input с тем же ID — конфликт.

| Action | Вход и результат |
|---|---|
| list | Каталог, варианты, active/default и integrity readiness |
| intake | Копия файла; PROFILE_DRAFT либо сохранённый recoverable checkpoint |
| inspect | operation_id либо pin, offset >=0, limit 1..50; context page и next_offset |
| profile-save | operation_id, profile по schema; generated guide и VALIDATED/NEEDS_CLARIFICATION |
| activate | operation_id, обязательный expected_revision (null для нового), optional default |
| history | template_id; manifest каждой ревизии и active |
| status / resume | operation_id; текущий checkpoint / повтор безопасного извлечения |
| rollback | pin прежней проверенной ревизии и expected_revision |
| resolve | document_type, optional template_id/revision_id; pin текущего document gate |
| defer | Причина и optional document_type; checkpoint отложенного шага |
| relocate | documentation_path, operation_id; копирование с проверкой хешей, старый корень сохраняется |

`list` и первая страница `inspect` возвращают authoritative schemas профиля и
плана в `contracts`. OpenCode может прочитать контракт через разрешённые tools,
без доступа к файлам через запрещённые built-ins.

Порядок выбора: explicit variant/revision → pin → default → единственный active.
Запрос с pin нельзя молча перевести на новую ревизию; создайте новый запрос/копию.
Каждый profile классифицирует все targets и содержит purpose, required,
required_basis, unknown handling, rules с provenance/source и сохранённые answers.
Неразрешённые questions и пересекающиеся variable targets блокируют активацию.
`filling-guide.md` генерируется из profile, самостоятельная правка нарушает хеши.

Профили заполненных примеров требуют явного выделения переменных значений.
Даты, имена, решения, объекты 1С, результаты тестов и согласования образца не
являются фактами нового документа. Правило `required` требует явного основания.
Профиль ФС отображает канонические section IDs из действующего стандарта;
профиль не отменяет approvals, evidence и проверки содержания.

## Создание копии

После resolve используйте `document-plan`, `document-write`, `document-validate`
с `--json-stdin` и тем же gate_id. Для плана request содержит `operations` и
optional `output_name`; для записи/проверки — `plan_id` из ответа.

```json
{
  "gate_id": "идентификатор document gate",
  "request": {
    "operations": [
      {"target_id": "field-2", "kind": "text", "value": "Значение из ответа", "basis": "Дословный ответ пользователя"}
    ]
  }
}
```

Allowlist operations: `text` для поля/абзаца, `section` для тела раздела,
`table` для строк таблицы. Targets берутся из extraction, не из произвольного
OOXML/code payload. Существующий результат никогда не перезаписывается.
План связывает хеш содержания, исходника, profile и pin. Итог имеет
`UNVERIFIED_DRAFT`; agent-complete повторно проверяет сохранённый результат.

DOCX: placeholders, включая split runs, поля в простых таблицах/колонтитулах,
абзацы, разделы с уникальными структурными адресами, таблицы с образцом строки.
Незаполняемые части OOXML сохраняются побайтово, включая стили, numbering,
relationships, настройки секций и изображения. Bookmarks/content controls
извлекаются как anchors; запись выполняется в содержащий однозначный target.
Изменение сложных таблиц, text boxes, OLE, tracked changes, dynamic fields и
рисунков отвергается. Замена раздела, содержащего разрыв секции Word, также
отклоняется: используйте отдельные текстовые targets внутри него, чтобы сохранить
настройки страниц и привязки колонтитулов. Проверка действует и для ранее
сохранённых extraction/планов. Неизменяемые сложные области сохраняются. Расширение
поддержки операций требует нового format adapter contract и regression fixture.

Markdown: поля, тела разделов, GFM-таблицы. Повторные заголовки различаются
range/ID. Поля экранируются; таблицы сохраняют колонки. Заголовки не меняются
операцией section. Code fences/HTML сохраняются как недоверенные данные и не
исполняются; изменение этих областей не поддержано. Локальные ресурсы принимаются
только с явным resources_root, без parent traversal и junctions; копируются в
ревизию и результат. Внешние ресурсы не скачиваются. Недостающие файлы отражены
в checkpoint и блокируют полную готовность.

## Сохранность и восстановление

Лимиты: Markdown 10 MiB и 200 000 tokens, maxNesting 20; DOCX 50 MiB исходника,
250 MiB распакованного пакета и 4096 частей. Извлечение в отдельном процессе
имеет hard timeout 10 секунд и output budget 16 MiB. `inspect` выдаёт до 10 000
символов targets, 8000 символов guide и 4000 символов metadata плюс envelope.
Для полного чтения используйте `coverage.next_offset`, `coverage.next_text_offset`,
`guide_next_offset` и `metadata_next_offset` как соответствующие входные offsets.
Длинный target читается порциями с одним и тем же offset и изменяемым text_offset;
guide_limit можно уменьшить (1..8000). ZIP size проверяется до CRC/decompression, DTD/entities,
макросы, traversal и reparse points запрещены. Ссылки и содержимое не выполняются.

Active/default меняются под exclusive lock с expected revision. После сбоя
resume сохраняет исходник и прежнюю active. После аварийного завершения writer
lock удаляют только после проверки, что writer больше не работает. История и
готовые результаты автоматически не удаляются. Перенос сохраняет library_id,
hashes и pins и отказывается затирать библиотеку с другим ID. Future schema
отклоняется без переписывания. Migration local config 1→2 сохраняет неизвестные
поля и legacy functional_spec_template; backup создаётся до записи.

Formal ФС сохраняет специализированный маршрут. Библиотечный пригодный вариант
удовлетворяет наличию шаблона; без него действует legacy вход. `resolve` закрепляет
его в каталоге текущего work item. `document-plan` и повторная запись требуют
актуального согласования canonical section content через section-approve и
сверяют каждый mapped target с согласованным текстом. `agent-complete` повторяет
эти проверки и требование Word: отзыв согласования или изменение текста после
записи не позволяет завершить формальный этап. Новые validation records связывают
записанный документ с хешем операций плана; существующие records проверяются
по сохранённому Markdown-содержанию без миграции. Новая Word-копия и Markdown
содержание сохраняются в `specification/template-documents/<gate-id>/`;
Markdown-шаблон создаёт Markdown-копию. Содержимое передаётся в существующий
`flow1c_write` маршрут формальной ФС; проверки этапа и публикации сохраняются.
`template_library.require_word_for: ["functional-spec"]` явно требует Word
и не отменяется Markdown-шаблоном. Formal тестирование использует свой gate,
work item и evidence; шаблон сам по себе не подтверждает результаты тестов.
Визуальный renderer и реальные model-dialogue evals требуют отдельной
квалификации: релизная готовность заявляется только с соответствующим evidence.

Пути библиотеки и drafts указаны относительно служебного корня документации:
для новых проектов это `.flow1c/`, для старых — корень документации.
[Версии структуры и совместимость](documentation-layout.md).
