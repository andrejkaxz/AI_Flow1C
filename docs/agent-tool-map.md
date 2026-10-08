# Agent tool contract

The same CLI contract is used by OpenCode, Codex, and Claude Code. In environments without custom `flow1c_*` tools, invoke the mapped command directly.

| Tool | CLI |
|---|---|
| `flow1c_route_catalog` (read-only, pre-gate) | `flow1c.py route-catalog --json`; продуктовый каталог и digest, без пользовательских файлов |
| `flow1c_route_check` (read-only, pre-gate) | `flow1c.py route-check --json-stdin`; прямой bounded RouteProposal, tool field `proposal_json` |
| `flow1c_handoff` (completed gate only) | `flow1c.py agent-handoff --json-stdin`; `gate_id`, `action=read|recover`. [Передача и recovery](handoff.md); без нового этапа и повторения effects |
| `flow1c_begin` | `flow1c.py agent-begin --json-stdin`; `reference_kind=auto|requirement|specification`, для `query-analysis` явный `query_intent=create|review|optimize` |
| `flow1c_interview` | `flow1c.py interview-register --json-stdin`; `inspect|write|audit`, отдельный interview-preparation gate; XLSX остаётся UNVERIFIED_DRAFT, исходник сохраняется. [Контракт](interview-preparation.md) |
| `flow1c_git_inspect` | `flow1c.py agent-git-inspect --json-stdin`; `action=integration`, `git_refs`, `target_ref` находят вливания нескольких веток без сканирования bounded log |
| `flow1c_dialogue` | `flow1c.py agent-dialogue --json-stdin`; для `deviate`: `deviation_type`, `scope`, `condition_ids`, `user_statement`, `answer` |
| `flow1c_intake` | `flow1c.py artifact-intake --json-stdin` |
| `flow1c_redmine_files` (optional, OpenCode; read-only, no gate required) | `flow1c.py redmine files <issue-number> --json` |
| `flow1c_redmine_relations` (optional, OpenCode; read-only, no gate required) | `flow1c.py redmine relations <issue-number> --json` |
| `flow1c_redmine_fetch` (optional, OpenCode) | `flow1c.py redmine fetch <issue-number> [--gate-id <active-gate>] [--code <matching-reference>] [--dms-file <id> ... | --all-dms] [--dms-revision <id>]` |
| `flow1c_redmine_upload` (optional, OpenCode; preflight is read-only, write requires explicit confirmation) | `flow1c.py redmine upload <issue-number> --file <absolute-path> [--confirmed] --json` |
| `flow1c_context` | `flow1c.py agent-context --json-stdin`; legacy full default, `view=compact`, `action=read` с `entry_id`, `section_id`, `cursor`, `max_chars`; отдельный CLI `context-read`. [Контракт](context.md) |
| `flow1c_inspect` | `flow1c.py agent-inspect --json-stdin` |
| `flow1c_diff` | `flow1c.py agent-diff --json-stdin` |
| `flow1c_source_read` | `flow1c.py agent-source-read --json-stdin` |
| `flow1c_source_query` | `flow1c.py source-query --json-stdin` (`query` = цель evidence, `code` = Python для `rlm_execute` с `print()`) |
| `flow1c_cc_inspect` | `flow1c.py cc-inspect --json-stdin`; только фиксированные read-only операции `meta-info`/`skd-info`, относительный XML-путь и активный `query-analysis` gate |
| `flow1c_query_schema` | `flow1c.py query-schema --json-stdin`; точные поля одного XML-объекта, `source`, относительный `path`, для конфигурации/расширения `rlm_evidence_id` с тем же путём |
| `flow1c_query_check` | `flow1c.py query-check --json-stdin`; полный `text`, `schema_ids`, `expected_result`, `assumptions`, при оптимизации `baseline_text` и `changes`; сохраняет точный кандидат и консервативный статический отчёт |
| `flow1c_section` | `flow1c.py section-catalog|section-save|section-approve --json-stdin`; черновик и согласование версии раздела, без изменения Word |
| `flow1c_docx` | `flow1c.py docx-inspect|docx-write-plan|docx-write --json-stdin`; проверяемая запись согласованного раздела в новый DOCX |
| `flow1c_write` | `flow1c.py agent-write --json-stdin` |
| `flow1c_analyze_bsl` | `flow1c.py agent-analyze-bsl --json-stdin`; optional `source={kind: extension|configuration|git_snapshot, snapshot_id?, commit?}` |
| `flow1c_promote` | `flow1c.py draft-promote --json-stdin` |
| `flow1c_action` | `flow1c.py agent-action --json-stdin`; actions также включают typed `provisional-start` и `registry-reconcile` для разрешённых document stages |
| `flow1c_complete` | `flow1c.py agent-complete --json-stdin` |

`flow1c_begin` принимает необязательный `route_proposal_json`, который передаётся
CLI как вложенный `route_proposal` через UTF-8 stdin без shell-конкатенации.
CLI пересчитывает решение; outer operation/mode/summary должны совпадать.
Первая операция, создающая gate, — begin; route tools и опубликованные Redmine
исключения допускаются до неё. Решение о маршруте не выдаёт permissions.

## Доступность по режимам

`flow1c_analyze_bsl` доступен для `code-review` в `explore`, `draft` и `formal`, включая заблокированный формальный этап. Это read-only диагностика выбранного текущего источника или snapshot, созданного в том же запросе; результат содержит provenance и путь к JSON/SARIF. Диагностика не меняет формальный статус. Если у заблокированного formal gate ещё нет work-item evidence, результат сохраняется отдельно в `.workspace/diagnostics/bsl-ls`.

| Режим | `flow1c_source_read` | Источник и ограничения |
|---|---|---|
| `explore` | нет | Консультация использует чат, принятые материалы и разрешённые запросы к RLM |
| `draft` | да | Только точный разрешённый файл из текущего `extension_path`, без записи |
| `formal` | по `config/stages.json` | Только этапы, в чьём `allowed_tools` уже указан инструмент |

`flow1c_source_read` не читает основную конфигурацию, историю Git, произвольные commit/blob или файлы вне канонизированного `extension_path`. Для конфигурации и полнотекстового поиска по текущему checkout используется `flow1c_source_query`; история, состав разработки и diff проверяются через `flow1c_git_inspect` до RLM при наличии номера разработки и настроенного Git. RLM-хелпер `git_search` ищет строку в текущем checkout и не заменяет Git evidence истории.

Если RLM недоступен, сначала восстановите его через `bootstrap.ps1 -Profile analysis -Resume` (если инструмент отсутствует), `start-rlm-tools-bsl.ps1` (если сервис остановлен), затем `rlm-index.ps1 -Action Ensure`/`Wait` для настроенных источников и повторите тот же `flow1c_source_query`. Ответ `NEEDS_INPUT` содержит `next_actions`; лишь после неудачного восстановления он означает ограничение проверки.

`flow1c_cc_inspect` не является оболочкой общего назначения: он не принимает команду или путь к скрипту, ограничивает вход 25 МиБ, вывод 24 000 символами и выполнение 30 секундами. `skd-*` принимает только `Template.xml`; `meta-*` его отвергает. Источником может быть только принятый XML текущего gate либо относительный путь внутри настроенной локальной выгрузки конфигурации/расширения. Результат подтверждает лишь прочитанный XML, а не состояние базы 1С.

Для `query-analysis` начните `flow1c_begin` с явным `query_intent` (`create`, `review`, `optimize`). Любой новый gate этого маршрута требует последнего `flow1c_query_check`: `flow1c_complete` сверяет SHA-256 сохранённого текста и версии выбранных XML, а ответ возвращает точный `query_text`. Ранее сохранённые gate с `query_contract_version=0` сохраняют прежнее завершение. Явную просьбу составить, проверить или оптимизировать запрос 1С, ошибочно переданную как `consultation`, CLI переводит в `query-analysis` и записывает исходную операцию в gate. Вопрос «как написать запрос» остаётся консультацией. Успешный `flow1c_source_query` в этом маршруте возвращает `RETRIEVED_UNVALIDATED`: напечатанный результат не является подтверждением поля.
Если RLM или CC-skills недоступны, ответы этих инструментов в `query-analysis` сохраняют `flow1c_query_check` среди доступных действий. Продолжайте независимую проверку текста и укажите ограничение; отсутствие источника не подтверждает метаданные.
Если пользователь заранее указал, что база и XML недоступны, или просит только статический анализ, сразу вызовите `flow1c_query_check` без пробного `flow1c_source_query`.

Before formal completion, run `doctor --json --operation <operation>`. A user may explicitly record a document-process deviation with `agent-dialogue` action `deviate`; publication and extension-write safety controls remain non-waivable. `available_actions` is state-aware: waiting states expose dialogue, input states expose dialogue plus configured intake/recovery actions, ready states expose the stage allowlist, and terminal states expose no work tools.
## Historical Git tools

| Tool | Scope | Contract |
|---|---|---|
| `flow1c_git_refresh` | explore/draft | One targeted origin fetch after saved user intent; never changes checkout |
| `flow1c_git_inspect merge-search` | explore/draft | Batch resolver with topology, message, PR and patch evidence |
| `flow1c_git_inspect branch-changes` | explore/draft | Unique commits and deterministic 1C object/routine change set |
| `flow1c_git_inspect history-search` | explore/draft | Bounded structured target-history filters |
| `flow1c_git_inspect diff` | explore/draft | Full names plus bounded stat/patch pages; `next_cursor` continues within a large file before moving to the next file |
| `flow1c_git_inspect read-at-ref` | explore/draft | Allowed historical blob content without checkout |
| `flow1c_git_snapshot` | explore/draft | Atomic managed historical source with manifest/provenance |
| `flow1c_source_query` with `git_snapshot` | explore/draft | Separate historical RLM source; recoverable failure preserves Git evidence |

Direct bash is supplementary and guard-limited. Direct `git fetch`, mutation commands, configuration flags, external diff/textconv and paths outside configured repository roots are denied.
## Shared template tools

| OpenCode | Codex / Claude CLI | Contract |
|---|---|---|
| `flow1c_template` | template ACTION --json-stdin | docs/document-templates.md; shared gate_id and request JSON |
| `flow1c_document` | document-plan / document-write / document-validate --json-stdin | pinned template-document draft gate; typed operations |

Template readiness uses doctor --json --operation template-management or
template-document with additive --format markdown/docx. flow1c_template is available
as a substep in setup/update/formal FS/testing; flow1c_document is available in draft
and specialized formal FS/testing gates, and cannot bypass formal
approvals or write directly into the library. All adapters use canonical
.agents/skills/flow1c-document-templates/SKILL.md and the same schemas/policy.

## Project knowledge and result navigation

`flow1c_knowledge` maps to `knowledge --json-stdin`, action/request. Reads use the current role gate; mutations require a ready formal status gate and the user instruction. Navigation, Git/local search/read, exact preview/write and selected documentation commit/PR use one contract: [project-knowledge.md](project-knowledge.md).
