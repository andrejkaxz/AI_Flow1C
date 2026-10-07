# Контракт маршрута: каталог и проверка

Каталог и проверка подключены к CLI и адаптеру OpenCode. `route-catalog --json`
читает только продуктовые правила; `route-check --json-stdin` принимает прямой
RouteProposal и не создаёт gate. `agent-begin --json-stdin` принимает необязательное
поле `route_proposal`, заново проверяет его и сохраняет решение с первой записью gate.
Каталог возвращает точную `proposal_schema`, чтобы исправить формат предложения
через разрешённый pre-gate интерфейс без чтения файлов или угадывания полей.

Адаптер определяет смысл просьбы и передаёт поля. Чистая политика проверяет их
совместимость; она не классифицирует произвольный текст и не подтверждает
семантическую правильность выбора. `VALID` позволяет обратиться к существующему
gate, который заново проверяет prerequisites, approvals, evidence и пути.

## Владельцы правил

- `config/intent-routes.json`: 19 публичных операций, activation/exclusion
  примеры, режимы, свободный primary skill, источники и ограниченные substeps.
- `config/stages.json`: formal skill/role, prerequisites и разрешения. Каталог
  хранит ссылку на stage, без копии required files, approvals или allowlists.
- `flow1c.routing_policy`: чистая композиция и проверка; не читает файлы.
- `flow1c.routing`: явная загрузка только продуктового каталога и stage config,
  проверка существования skills и выходных контрактов. Пользовательские настройки,
  реестр, источники 1С и gates не читаются и не создаются.

SHA-256 эффективных правил включает каталог, stage policies и действующие free
tool lists. Изменение владельца policy меняет digest. Версия route policy — 1;
версии существующих gates и evidence этим этапом не меняются.

## Предложение и результат

Контракты находятся в `schemas/route-proposal.schema.json` и
`schemas/route-decision.schema.json`. Предложение ограничено 32 KiB UTF-8 JSON;
цель — 4000 символов, источники — 8, ссылки на сообщения/ответы — 16.

Пример проверки в Python из корня установленного checkout:

```python
from pathlib import Path
from flow1c.routing import check_proposal

decision = check_proposal({
    "schema_version": 1,
    "expected_outcome": "Проверить изменения в явно выбранной ветке",
    "operation": "code-review",
    "mode": "explore",
    "sources": [{
        "kind": "git_history", "selector": "user-selected-branch",
        "version": "historical",
    }],
    "references": {"task_reference": "user-selected-task"},
    "basis": ["message-with-explicit-branch"],
}, product_root=Path.cwd())
```

`primary_skill` и `role` можно не передавать: их возвращает политика. Переданные
поля обязаны совпасть с владельцем operation/mode. В свободном `code-review`
сохранён `flow1c-consultation`; formal использует `flow1c-technical-review`.
Для query/interview/templates сохранены действующие профильные исключения.

| Результат | Ближайшее действие |
|---|---|
| `VALID` | Перейти к текущим gate checks; это не approval или разрешение mutation |
| `CLARIFICATION_REQUIRED` | Задать один вопрос о первой неразрешённой части, до gate |
| `INVALID` | Исправить предложение; пользовательское состояние сохранено |

`ambiguities` описывает нерешённые operation/mode/source/object/outcome. Адаптер
должен сначала использовать сохранённые ответы и указать ссылки в `basis`.
Эта итерация не загружает историю gate и не повторяет вопросы самостоятельно.
Незнакомая операция не заменяется консультацией. Поля permissions, approvals,
доказанный commit и freshness в предложении отклоняются.

## Версии источников

`configuration`/`extension` запрашивают текущую версию через gated RLM;
`git_history` — историческую через Git inspect; `git_snapshot` — историческую
семантику через snapshot и RLM. `chat`/`attachment` описывают предоставленный
материал. Остальные descriptors ссылаются на существующие workflow, registry,
work-item, template и Redmine границы.

`selector` сохраняется как пользовательское значение, без поиска и разрешения
Git ref. Для Git источников можно явно передать `repository=workflow|extension`.
Commit и freshness определяют последующие source tools. Номер задачи
в `task_reference` не становится веткой автоматически. Для исторического ref,
вложения или Redmine объекта без selector требуется уточнение.

Для сравнения передаётся `source_relation=compare` и минимум два различных
descriptors. Решение возвращает resolver для каждой стороны; получение и
проверка evidence обеих сторон остаются обязанностью gate integration.

Redmine files/relations остаются опубликованными read-only исключениями до gate.
Template library является gated substep и сохраняет primary маршрута. Guard
разрешает до gate только новые `flow1c_route_catalog` / `flow1c_route_check` и
прежние ограниченные исключения. Они не открывают read/bash, source или mutation.

## Совместимость и проверка

`tests/fixtures/routing-baseline.json` фиксирует defaults, узкое исправление
consultation → query-analysis и фактические runtime versions. Comparison bridge
`check_legacy_route` переиспользует прежние mode/query policies и сохраняет
`requested_operation`; он подключён к legacy begin. Brand/operation aliases
не добавляются. `code`/`g_number` остаются reference aliases, а Git action
`integration` — alias `merge-search`.

## CLI и продолжение

```json
{
  "operation": "functional-spec",
  "mode": "draft",
  "summary": "Подготовить черновик приёмки",
  "route_proposal": {
    "schema_version": 1,
    "expected_outcome": "Подготовить черновик приёмки",
    "operation": "functional-spec",
    "mode": "draft",
    "sources": [{"kind": "chat", "version": "provided"}]
  }
}
```

Передай объект через stdin в `python scripts/flow1c.py agent-begin --json-stdin`.
В OpenCode proposal передаётся JSON-строкой: `flow1c_route_check(proposal_json=...)`,
затем `flow1c_begin(route_proposal_json=...)`. Outer operation/mode/summary и
пересекающиеся references должны совпадать. Proposal не принимает digest,
permissions, approvals или identity другого gate; resume выполняется dialogue.
`route-check` возвращает exit code 0 / 1 / 2 для VALID / CLARIFICATION_REQUIRED /
INVALID. Неоднозначный или несовместимый structured begin не создаёт состояние.

Gate schema version 1 и POLICY_VERSION сохранены. Additive `route_origin`,
`route_proposal` и `route_decision` не требуют миграции старых записей. Legacy
вызовы сохраняют defaults/query remap и получают `route_origin=legacy`; decision
описывает совместимость, но не меняет прежнюю assessment пустого/неполного запроса.
Старые gates без route fields загружаются как прежде. При загрузке structured
gate proposal проверяется по текущему каталогу; сохранённый digest не закрепляет
permissions. Несовместимость возвращает `ROUTE_RECOVERY_REQUIRED` и сохраняет
state/answers/evidence. Явная смена свободного mode через dialogue обновляет
proposal/decision на том же gate.

`python scripts/generate-route-projections.py` обновляет только помеченные блоки
AGENTS/CLAUDE, controller, docs и canonical/Claude skills. `--check` не пишет
файлы и возвращает 1 при drift. Текст вне блоков сохраняется. Изменение catalog,
proposal/decision schemas входит в OpenCode runtime identity и требует перезапуска
загруженного клиента, после которого продолжается сохранённый gate.

Детерминированные тесты и smoke не подтверждают точность модели. Handoff,
compact context и target-client/model acceptance
остаются отдельными этапами.

Generic formal template gates сохранены в каталоге как существующая возможность
legacy begin. Запись template-document требует `draft` либо специализированного
formal gate functional-spec/testing. Каталог не расширяет writer permissions.

В runtime существуют evidence v1/v2 при декларации старой schema v1. Расхождение
зафиксировано в baseline; миграция схем и записей относится к следующему этапу.

`evals/routing-cases.json` содержит 69 размеченных синтетических случаев, включая
development и held-out splits. Held-out prompts не входят в activation или
boundary examples каталога. Проверка аннотаций не измеряет понимание языка моделью.
Живые client/model evals и их пороги остаются отдельной приёмкой.

```text
python -m unittest tests.test_routing_policy tests.test_routing_contract -v
python scripts/routing-smoke.py --output routing-smoke-evidence.json
```

Smoke сравнивает структурированные аннотации и legacy defaults. В отчёте явно
указаны отсутствие gate integration/model evaluation и неготовность полного
перехода к выпуску.
