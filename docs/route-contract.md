# Контракт маршрута: каталог и проверка

Первая итерация реализует каталог и проверку структурированного предложения.
Действующие CLI, gates и инструкции клиентов продолжают использовать прежнюю
маршрутизацию. Команды `route-catalog`/`route-check`, подключение к `agent-begin`
и инструменты адаптеров будут включены отдельным этапом.

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
Template library является gated substep и сохраняет primary маршрута. Сам
каталог и решение не меняют guard или доступность инструментов.

## Совместимость и проверка

`tests/fixtures/routing-baseline.json` фиксирует defaults, узкое исправление
consultation → query-analysis и фактические runtime versions. Comparison bridge
`check_legacy_route` переиспользует прежние mode/query policies и сохраняет
`requested_operation`; он пока не подключён к CLI. Brand/operation aliases
не добавляются. `code`/`g_number` остаются reference aliases, а Git action
`integration` — alias `merge-search`.

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
