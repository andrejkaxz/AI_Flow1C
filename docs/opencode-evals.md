# OpenCode Qwen evals

## Current diagnostic scope

Model evals are optional diagnostics for a reproducible user problem. Select
the affected scenario and the exact configured model/variant; start with one
run and repeat only when a failure or intermittent behavior needs investigation.
The current delivery scope uses Windows CI and targeted regressions. Full
corpus evaluation, baseline comparison, 20-run model certification and extra
platform/client checks are not prerequisites for this scope.

The benchmark criteria documented below still define their respective report
fields. A targeted diagnostic does not pass full model acceptance, and changing
delivery scope does not turn failed or incomplete reports into passing ones.

## Model route selection

`scripts/routing_evals.py` runs the frozen `evals/routing-cases.json` corpus
against the configured model through the same OpenCode controller/tools as E2E.
It sends only prompt/context/saved answers and a versioned selection protocol;
expected labels, rejected routes and split are not supplied to the model. The
protocol asks for a route check or one clarification, then stops before begin.
This measures **selection**, not execution, completion, safety of a full task,
or resume. Diagnose execution and resume separately when the reported problem
concerns those behaviors.

Annotation version 2 identifies the synthetic attachment `synthetic-spec.md`
in the injection case's input context. Its expected selector was already in
version 1 but absent from the user input; labels/thresholds/prompts/activation
descriptions remain unchanged. Preflight rejects selectors absent from user
inputs so a model is not asked to guess hidden fixture metadata.

```powershell
$EvalDirectory = Join-Path $env:TEMP ("flow1c-routing-" + [guid]::NewGuid().ToString("N"))
.\scripts\run-opencode-evals.ps1 -Routing -Model "<configured-provider/model-id>" `
    -Variant "<configured-variant>" -Runs 1 -OutputDirectory $EvalDirectory
```

Use a new disposable directory outside the author workspace, not an active
user checkout. Fixtures contain synthetic settings/materials and unavailable
local integration endpoints. They never copy the user's local settings, tokens
or source exports. `-FixtureOnly` prepares fixtures without model calls and
cannot pass model acceptance. `-Scenario <case-id>` and `-Split held-out` select
diagnostics; subsets cannot satisfy full corpus acceptance. Do not use held-out
results to rewrite activation descriptions.

The evaluator scores the **first attempted check**, including errors. A later
correct retry does not erase a wrong first decision. It compares operation,
mode, primary skill, role, source versions/selectors and source relation with
independent annotations; source order and resolver metadata are ignored.
Ambiguity requires one real question before any gate. Supplied answers must not
be asked again. Any attempted tool outside catalog/check/question/skill fails
the selection protocol, even if the tool itself refuses the action.

Reports include exact requested and observed model/variant, OpenCode/Python/OS,
raw messages/questions/events, model usage reported before stopping, prompt
version, corpus and evaluator hashes, product/client hashes, category coverage
and per-dimension scores. Usage may be partial because selection stops before
the full task; it is not a measurement of task token savings. Hashes are checked
again at the end so edits during a run invalidate its acceptance result.

`selection_passed` requires complete unique runs, at least 95% exact first
decisions and operation/mode/primary/source scores, 100% ambiguity and zero
selection-scope violations. Missing cases, duplicate runs, provider/timeouts,
unobserved model/variant and unexpected questions fail acceptance. Add
`-BaselineRef <local-product-commit>` for interleaved baseline/candidate runs on
the same model and variant; a passing baseline run that fails on the candidate
is a regression. `corpus_acceptance_passed` additionally requires the whole
corpus, unchanged artifacts and baseline comparison. `release_ready` remains
false: client resume, mandatory E2E repetitions and platform checks are separate.

`python scripts/context-smoke.py --output context-smoke-evidence.json` проверяет
compact manifest, точные страницы, обязательное покрытие, process death и resume
после переустановки того же пакета. Changed-source/cursor и containment защищены
context unit tests. Это deterministic evidence: поддержка клиента/модели и
token/cost savings требуют отдельных live evals с compact view. [Контракт](context.md).

До begin допускаются `flow1c_route_catalog` и `flow1c_route_check`, а также
прежние ограниченные Redmine исключения. Первая операция с gate —
`flow1c_begin`; route decision не заменяет её. Evaluator проверяет эту границу.
CLI fixtures включают `flow1c/`, а `scripts/opencode_evals.py --output-dir`
позволяет создать новый каталог fixtures вне workspace автора.

`python scripts/routing-smoke.py --integration` проверяет structured/legacy
gates, process restart, evidence v1/v2 и сохранение локальных настроек при
повторной установке Python-пакета той же версии. Это не clean bootstrap,
Git update или измерение точности модели. Corpus `evals/routing-cases.json`
остаётся размеченным входом pure checks; 95% routing/mode/source и 20 E2E
повторений подтверждаются только отдельными model reports.

Реальные прогоны выполняются только в одноразовом checkout с тестовыми репозиториями документации и расширения. Корпус находится в `evals/opencode-natural-language.json`; каждое предусловие должно быть воспроизведено перед запуском соответствующего сценария.

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\run-opencode-evals.ps1 -Model "<provider>/<qwen-model-id>" -Runs 20
```

Скрипт использует primary agent `flow1c-controller`, сохраняет сырые JSON events в `.workspace/opencode-evals/` (или `-OutputDirectory`), допускает catalog/check и ограниченные Redmine exceptions до первого gate, проверяет запрет built-in tools и допустимое терминальное состояние. Все сценарии работают на одноразовых fixtures; внешние действия не разрешаются. `-Variant` закрепляет вариант рассуждений вместо неявного выбора из настроек клиента.

Сценарии `intent-ambiguous-inspection` и `intent-general-method` проверяют выбор маршрута до gate: предметный вопрос при неопределённой просьбе «посмотри», затем отсутствие источниковых инструментов для явного вопроса о методе. Для неоднозначного сценария проверяется порядок `question` перед `flow1c_begin`.

Сценарии анализа запроса требуют `flow1c_query_check` перед `flow1c_complete`, включая работу без базы и XML. Локальные CLI-тесты также проверяют, что явно запрошенное составление запроса, ошибочно переданное как `consultation`, открывает `query-analysis` с обязательной проверкой.
Сценарий `query-create-register-reconciliation` сверяет обязательные элементы именно сохранённого `query_text`, а не свободного итогового сообщения. Совпадение фрагментов не доказывает корректность расчёта или исполнение в 1С.

Успешный unit-тест CLI не подтверждает поведение модели. Для воспроизведения
проблемы указывай точный настроенный ID модели и затронутый сценарий. Если отдельно
заказана полная модельная приёмка, её прежний критерий — 20 последовательных
успешных прогонов каждого применимого сценария. Не подставляй вымышленный ID.

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

For separately requested full model certification, run each required case
20 times with the exact configured target model ID:

```powershell
.\scripts\run-opencode-evals.ps1 -Model <configured-provider/model-id> -Runs 20 -Scenario <case-id>
```

Do not infer a model ID. If the configured target is unavailable, record that
the live diagnostic could not run. Fixture/unit checks do not confirm model
behavior. This does not automatically block delivery of the current scope.
