# Обновление маршрутов и сохранённых запросов

Compact context включается явно и использует additive manifest/coverage v1.
Старые full-context consumers сохраняют контракт. [Полнота, cursors и resume](context.md).

Обновление добавляет необязательные route fields к gate schema version 1;
gate POLICY_VERSION остаётся 3. Существующие записи не переписываются массово.
Старый CLI без proposal сохраняет defaults и узкий query remap. Structured
begin сохраняет исходный proposal и пересчитанное решение до первой записи gate.

После обновления полностью перезапусти OpenCode в том же checkout и продолжи
по сохранённому gate_id через dialogue. Cached runtime проверяет catalog и
proposal/decision schemas вместе с tools/guard/controller. Устаревший клиент
возвращает `OPENCODE_RESTART_REQUIRED`; missing tool не заменяется shell.

При чтении structured gate текущая policy заново проверяет proposal и владельцев
operation/mode/skill. Сохранённый digest не даёт прежних полномочий. Если контракт
несовместим, `ROUTE_RECOVERY_REQUIRED` сохраняет gate, ответы и evidence; сначала
исправь совместимость установки или выполни явную повторную оценку маршрута.
Автоматический replacement/superseded resume — предмет отдельного этапа handoff.

Evidence schema принимает фактически используемые версии 1 и 2. Существующий
reader нормализует v1 Git records в памяти до v2; прежние artifacts и пользовательские
поля сохраняются. Чтение не перезаписывает исходный файл; очередная штатная запись
evidence сохраняет нормализованное представление. Версии выше 2 и неверные типы
версии отклоняются с сохранением файла. Это исправляет прежнее расхождение между
writer v2 и declaration v1, без изменения approvals или уровня доказательств.

Проверки: routing integration regressions, saved-gate smoke и projection `--check`.
Same-version package reinstall smoke не подтверждает real Git update или bootstrap;
для выпуска нужны отдельные platform/client/model reports.
