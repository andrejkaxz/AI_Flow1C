# Компактный контекст и дозагрузка

Для больших принятых материалов явно выберите compact view. Короткий пакет
сохраняет цель, маршрут, ограничения, статус, решения и blockers, а документы
представляет индексом. Summary и индекс не подтверждают прочтение оригинала.
Факты текущих или исторических XML/BSL по-прежнему проверяются через gated
source/RLM/Git tools; reader контекста не читает выгрузки 1С.

## Использование

CLI для существующего разрешённого gate:

```powershell
python scripts/flow1c.py agent-context --gate-id <gate-id> --view compact
python scripts/flow1c.py context-read --gate-id <gate-id> --entry-id scope
python scripts/flow1c.py context-read --gate-id <gate-id> --entry-id index
python scripts/flow1c.py context-read --gate-id <gate-id> --entry-id <src-id>
python scripts/flow1c.py context-read --gate-id <gate-id> --entry-id <src-id> --cursor <next-cursor>
```

В JSON transport команды принимают `--json-stdin`. OpenCode использует один
`flow1c_context`: `view=compact` для построения, `action=read` и `entry_id` для
дозагрузки. `scope` возвращает полные сохранённые цель, route, answers, decisions,
assumptions, constraints, deviations и blockers; `index` — постраничный NDJSON
список источников и частей. Большие metadata также имеют `next_cursor`.
Путь файла не является аргументом reader.

Свободный запрос получает контекст из собственного intake/evidence, без
work-item и formal registry. Formal gate получает документы своей роли и
requirements basis внутри разрешённого work-item. Состав зависит от
operation/mode/role, а source scope сохранён в identity запроса. Предъявленные
внешние пути сначала проходят обычный intake. DOCX/PDF доступны через принятый
производный текст с проверкой SHA-256 оригинала и извлечения.
Отсутствующий, пустой, неподдерживаемый или слишком большой источник явно
помечается unavailable; reader не подменяет его пустой успешной страницей.

Formal intake хранит ссылки относительно репозитория документации; reader
сужает их до своего work-item и отвергает ссылки на соседние work-items.
Исходный реестр XLSX обслуживается нормализованным requirements basis и не
дублируется в документном reader. Если legacy intake не записал SHA-256
извлечения, manifest явно фиксирует фактический digest при построении контекста;
он не выдаётся за прежнее подтверждение intake. Оригинал проверяется по
сохранённому intake hash, последующие страницы — по обоим digests.

## Полнота ревью

Для compact `functional-review` обязательны все части ФС, traceability и
requirements basis в formal режиме; для свободного ревью — все принятые
материалы. Отсутствие принятой ФС в свободном ревью оставляет required source
unavailable. Каждая readable entry разбита на точные character ranges
`part-00001`, `part-00002`, …, включая заголовки и кодовые блоки. Разбиение
является транспортным и не заменяет смысловые разделы ФС.

Можно читать весь источник по курсору либо выбрать `section_id` из index.
Покрытие сохраняет только возвращённые диапазоны. Чтение summary, scope или
index не увеличивает покрытие документов. Пропущенный префикс и частичная
страница остаются непрочитанными. `coverage.complete` относится только к
обязательным частям операции, а не к истинности фактов или согласованию.

Если обязательные части не прочитаны, complete возвращает
`CONTEXT_SCOPE_REQUIRED`, `ready=false` и оставляет gate активным. Продолжите
дозагрузку и повторите complete. Compact gate сохраняет обязательство покрытия:
переключение на full view не снимает эту проверку. Для других операций optional
документы не становятся искусственными prerequisites.

## Версии, сохранение и восстановление

`ContextManifest` v1 сохраняет SHA-256, identities, reasons, original/extracted
provenance, source scope и обязательные ranges. Связанная coverage v1 хранит
прочитанные интервалы и выданные сервером курсоры. Оба records неизменяемы;
evidence содержит ссылки и полные digests актуальных версий. Они размещаются
рядом с evidence в `contexts/<gate-id>/`, отдельно для каждого gate.

Каждая страница атомарно переключает одну authoritative evidence reference.
Сбой до переключения сохраняет прежнюю coverage и выданный cursor; повтор
может вернуть ту же страницу, но не потеряет диапазоны и не повторит действие
этапа. Context и complete/recovery используют существующую OS writer lock.
Не удаляйте contexts активного gate. Автоматического cleanup этих records нет.

Курсор привязан к manifest, source SHA-256, выбранной части и scope. Изменение
источника, intake, ответа, ограничения или маршрута требует перестроить compact
context на том же gate. Изменённый принятый файл сначала снова проходит intake.
Неизменившийся manifest при перестроении сохраняет coverage; новый сбрасывает
её. На complete версии источников проверяются повторно. Containment и
symlink/reparse checks применяются до canonical resolution. Неизвестные версии
или повреждённые digests не принимаются; восстановите сохранённые records.

После успешного complete context больше не изменяется через reader. Исторический
результат передаётся через [handoff](handoff.md); новая операция получает
собственный gate и перепроверяет источники.

## Лимиты и совместимость

`config/context.json` задаёт максимум 12 000 символов compact pack, 8 000
символов текста страницы и 24 000 символов сериализованного ответа reader.
JSON escaping учитывается: страница может быть меньше 8 000 символов, и
`next_cursor` укажет точное продолжение. Верхние пределы проверяет pure policy.
Один source ограничен 10 МиБ, manifest — 1000 entries; непоместившийся
обязательный материал остаётся непроверенным. Символы не считаются точным token
budget; token/cost measurement требует client/model evals.

Legacy `context-build --code ... --role ...` и `agent-context` сохраняют full
view по умолчанию и прежние path/content поля. Для gated compact через
`context-build` дополнительно передайте `--view compact --gate-id ...`; code/role
должны совпадать с gate. Старые gates/evidence не мигрируются принудительно.
Новые поля additive, outer gate/evidence versions и POLICY_VERSION сохранены.
Обновлённый cached OpenCode adapter требует обычного полного перезапуска.

`scripts/context-smoke.py` проверяет standalone CLI, точное чтение, process
death, сохранённый cursor и переустановку того же пакета. Он не заменяет clean
Git bootstrap/update, проверки поддерживаемых платформ или приёмку модели.
