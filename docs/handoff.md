# Передача завершённого результата и восстановление

После успешного `agent-complete` Flow1C сохраняет неизменяемый пакет передачи
результата. Он содержит цель, маршрут, состояние, индекс файлов и evidence,
ссылки на решения пользователя, assumptions и открытые вопросы, актуальные
approvals/deviations и предложение ближайшего действия. Текст пакета является
данными; он не выдаёт полномочий и не открывает следующий этап.

Для Codex и Claude используется CLI; OpenCode предоставляет `flow1c_handoff`:

```powershell
python scripts/flow1c.py agent-handoff --gate-id <saved-gate-id> --action read
python scripts/flow1c.py agent-handoff --gate-id <saved-gate-id> --action recover
```

Обе команды принимают также `--json-stdin` с `gate_id` и необязательным `action`.
Произвольные пути, записи handoff и approvals на вход не принимаются.

При сбое после сохранения результата `agent-complete` возвращает завершённое
состояние и `handoff_error.code=HANDOFF_RECOVERY_REQUIRED`. Повторите завершение
с тем же gate ID либо выполните `agent-handoff --action recover`. Восстановление
использует сохранённую запись completion, дописывает evidence/request copy и
пакет передачи. Уже завершённые действия и проверки completion не повторяются.
При исправном пакете повторное завершение возвращает прежний результат.

`HANDOFF_INVALID` означает неверный контракт, принадлежность или повреждённую
передачу. `HANDOFF_STALE` означает изменение результата, authoritative evidence
или manifest/approvals. Эти ошибки не откатывают выполненный этап. Восстановление
не подменяет сохранённые хеши текущими и не исправляет старую неизменяемую версию:
изменённые материалы нужно проверить заново в соответствующем процессе.

## Persisted contracts

Outer gate/evidence сохраняют свои прежние версии. Новые поля `completion_record`,
`handoff_status` и `handoff` additive; пакет имеет собственный `schema_version: 1`
и [schema](../schemas/agent-handoff.schema.json). Завершённая запись хранит
проверенный результат, exit code и seal хешей gate, evidence, work-item manifest
и результатов. Seal сохраняется раньше дописывания completion в evidence.
Хеш evidence вычисляется без ссылки на handoff, чтобы избежать циклического
digest. Gate/evidence ссылаются на версию пакета относительным путём и SHA-256.

Версии располагаются в `drafts/<request-id>/handoffs/` для свободного запроса,
`work-items/<slug>/evidence/handoffs/<gate-id>/` для formal work-item либо
`.workspace/agent-handoffs/<gate-id>/` для formal системной операции без work-item.
Короткое имя версии содержит префикс identity; полная identity и digest обязательны
в записи. Возможная коллизия имени отклоняется, а файл не перезаписывается.

Completion и recovery имеют одного writer на gate. Переносимая OS-блокировка
использует `msvcrt` на Windows и `flock` на POSIX и освобождается при завершении
процесса. Последовательные события журнала сохраняются отдельными атомарными
файлами с цепочкой хешей; неудачные попытки добавляют новые события.
Symlinks/reparse points, выход за управляемые корни и подмена gate/evidence
отклоняются. Approvals читаются только из authoritative manifest.

## Совместимость и границы

Активные legacy gates продолжают прежний процесс. Завершённый gate без seal
можно явно восстановить командой `recover`: его пакет отмечается `legacy` и
`resume.incomplete=true`. Он не доказывает хеш исходного результата на момент
старого completion, не выдумывает route/evidence и не создаёт approvals.

Завершённый producer читается как сохранённые данные после смены policy version;
его исторический маршрут не переписывается. Активный gate и новые действия
проверяются текущей policy как прежде. Неизвестная outer schema или версия
completion/handoff возвращает ошибку с сохранением исходных данных.

Пакет проверяет сохранённые source identities в evidence; живой внешний источник
не объявляется перепроверенным чтением handoff. Для новой источниковой операции
нужно обновить соответствующее gated evidence. RLM, Git и текущие XML/BSL сохраняют
свои отдельные контракты. Compact context и bounded reader поставляются отдельно.

`scripts/handoff-smoke.py --output <report.json>` проверяет отдельные CLI-процессы,
сбой записи передачи, сохранённое completion, переустановку package files,
повторное завершение и отказ при изменённом файле. Это synthetic smoke;
он не заменяет clean bootstrap/Git update или client/model acceptance.
