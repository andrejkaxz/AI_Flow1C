# Сопровождение Flow1C

Работайте в корне собственного checkout этого репозитория. Разработка исходников
Flow1C использует редактор, Git и тесты; продуктовые gates обслуживают задачи 1С
и не являются условием рефакторинга самого продукта.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test --test-isolation=none tests/test_guard.mjs tests/test_opencode_runtime.mjs
.\.venv\Scripts\python.exe -m compileall -q scripts tests
```

Python 3.10+, четыре пробела и явные типы. Policy детерминирована и не выполняет I/O;
сервисы получают явные зависимости и возвращают данные. Новые функции не расширяют
монолит и не дублируют смысловые правила в адаптерах. Импорт не имеет side effects.

Сохраняйте публичные CLI/schema/state контракты, containment, provenance и approvals.
Несовместимые изменения требуют миграции и release note. Регрессии используют
synthetic fixtures и mock внешних границ, не живые сервисы или данные заказчика.
Значимые изменения сопровождаются тестами и обновлением документации/CHANGELOG.

`.workspace/`, `.venv/`, `.tools/`, `.flow1c.local.json` и secrets не коммитятся.
PR должны объяснять проблему, результат, проверки и ограничения. Их согласуют люди.
Clean bootstrap/update, resume и реальные client/model проверки выполняются перед
заявлением о готовности соответствующего выпуска.
