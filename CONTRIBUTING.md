# Сопровождение Flow1C

Работайте в корне собственного checkout этого репозитория. Разработка исходников
Flow1C использует редактор, Git и тесты; продуктовые gates обслуживают задачи 1С
и не являются условием рефакторинга самого продукта.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pytest
node --test --experimental-test-isolation=none tests/test_guard.mjs tests/test_opencode_runtime.mjs
.\.venv\Scripts\python.exe -m compileall -q flow1c scripts tests
```

Python 3.10+, четыре пробела и явные типы. Policy детерминирована и не выполняет I/O;
сервисы получают явные зависимости и возвращают данные. Новые функции не расширяют
пакет несвязанными обязанностями и не дублируют смысловые правила в адаптерах.
`scripts/flow1c.py` остаётся запускателем; CLI, сервисы и policy имеют отдельных
владельцев по `docs/architecture.md`. Импорт не имеет side effects.

Команда Node выше соответствует Node.js 22 в CI. В версиях со стабильным
флагом используйте `--test-isolation=none`.

Полные unittest и pytest выполняйте последовательно: DOCX fixtures используют
общий тестовый каталог. Проверки границ модулей входят в оба прогона.

Сохраняйте публичные CLI/schema/state контракты, containment, provenance и approvals.
Несовместимые изменения требуют миграции и release note. Регрессии используют
synthetic fixtures и mock внешних границ, не живые сервисы или данные заказчика.
Значимые изменения сопровождаются тестами и обновлением документации/CHANGELOG.

`.workspace/`, `.venv/`, `.tools/`, `.flow1c.local.json` и secrets не коммитятся.
PR должны объяснять проблему, результат, проверки и ограничения. Их согласуют люди.
Clean bootstrap/update, resume и реальные client/model проверки выполняются перед
заявлением о готовности соответствующего выпуска.
