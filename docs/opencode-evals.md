# OpenCode Qwen evals

Реальные прогоны выполняются только в одноразовом checkout с тестовыми репозиториями документации и расширения. Корпус находится в `evals/opencode-natural-language.json`; каждое предусловие должно быть воспроизведено перед запуском соответствующего сценария.

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\run-opencode-evals.ps1 -Model "<provider>/<qwen-model-id>" -Runs 20
```

Скрипт использует primary agent `flow1c-controller`, сохраняет сырые JSON events в `.workspace/opencode-evals/`, проверяет, что первым FLOW1C-инструментом был `flow1c_begin`, что запрещённые built-in tools не вызывались и что получено допустимое терминальное состояние. Мутационные сценарии пропускаются по умолчанию; включать их можно только в изолированной fixture параметром `-AllowMutatingScenarios`.

Сценарии `intent-ambiguous-inspection` и `intent-general-method` проверяют выбор маршрута до gate: предметный вопрос при неопределённой просьбе «посмотри», затем отсутствие источниковых инструментов для явного вопроса о методе. Для неоднозначного сценария проверяется порядок `question` перед `flow1c_begin`.

Сценарии анализа запроса требуют `flow1c_query_check` перед `flow1c_complete`, включая работу без базы и XML. Локальные CLI-тесты также проверяют, что явно запрошенное составление запроса, ошибочно переданное как `consultation`, открывает `query-analysis` с обязательной проверкой.
Сценарий `query-create-register-reconciliation` сверяет обязательные элементы именно сохранённого `query_text`, а не свободного итогового сообщения. Совпадение фрагментов не доказывает корректность расчёта или исполнение в 1С.

Успешный unit-тест CLI не заменяет этот eval. Для целевых локальных Qwen и DeepSeek проводи отдельные прогоны с точным настроенным ID каждой модели; приёмка каждой модели считается пройденной только при 20 последовательных успешных прогонах каждого применимого сценария. Не подставляй вымышленный ID модели.

Сценарий `technical-implementation-summary-past-tense` проверяет редактуру раздела
по пяти категориям, прошедшее время (включая отсутствие изменений), совместное
перечисление процедур одного модуля и отсутствие листингов/подробных карточек.
`section_content_checks` проверяет регулярными выражениями текст последнего
успешного `flow1c_section action=save` выбранного раздела, а не обещания в ответе.
Это ограниченная проверка контрольного примера, а не универсальный анализ языка.
`flow1c_section action=catalog` возвращает полные канонические правила в `contracts`,
сохраняя совместимый список имён `sections`.

## Reliable Git-analysis evals

Git cases assert structured tool calls rather than answer keywords: one begin, at most one refresh, one batch merge-search, SHA/evidence level, names/stat before patch, no semantic duplicates, one snapshot per commit, terminal completion and an unchanged extension tree hash. Each case defines a maximum Git/tool-call budget. Visible assistant text is rejected when it contains internal reconsideration markers such as `Actually`, `Hmm`, or `Let me reconsider`.

`git-single-large-patch` creates one large `Module.bsl` change and checks that the agent follows every `next_cursor` through the final patch page. A fixture-only run verifies the local setup; model behavior is assessed only by a configured OpenCode run.

Run every required case 20 times with the exact configured target model ID:

```powershell
.\scripts\run-opencode-evals.ps1 -Model <configured-provider/model-id> -Runs 20 -Scenario <case-id>
```

Do not infer a model ID. If the configured Qwen3.8-27B target is unavailable, fixture/unit checks remain useful but release status is `BLOCKED_RELEASE: TARGET_MODEL_EVAL_UNAVAILABLE`.
