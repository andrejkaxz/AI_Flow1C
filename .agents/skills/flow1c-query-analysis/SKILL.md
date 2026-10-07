---
name: flow1c-query-analysis
description: Create a 1C query from a user's description, review an existing query, or propose a safer structural optimization with explicit static evidence and no database requirement.
---

# FLOW1C 1C query work

Use `query-analysis` in `explore` for `create`, `review`, or `optimize`. Pass explicit `query_intent` to `flow1c_begin`. This consultation saves a candidate and evidence inside its ignored request directory; it never changes configuration or extension sources and needs no work-item reference.

## Establish the requested result

- For `create`, identify the desired row grain, columns, source objects, filters and period, parameters, ordering, totals, and the meaning of amounts/currencies. Ask one focused question only when a missing decision would materially change the result. Record assumptions and unresolved decisions. Then write one complete candidate query.
- For `review`, preserve the supplied query as the candidate unless the user asks for a corrected version. Explain defects with exact fragments and distinguish proven errors from risks and open questions.
- For `optimize`, preserve the original as `baseline_text`, propose a complete candidate, and explain each change's semantic precondition. Compare row grain, filters, joins, NULL behavior, totals, and parameters. Call possible speedups hypotheses; no performance gain is measured here.
- For a query in a Data Composition Schema, use `skd-info` through `flow1c_cc_inspect` on the actual `Template.xml` to identify the dataset and query. Do not apply `skd-info` to a pasted ordinary query.

## Obtain evidence without inventing metadata

Use `flow1c_source_query` to discover exact current configuration or extension objects and XML paths. A successful RLM call means only that output was retrieved; a printed object, string match, empty list, truncated result, or exception does not confirm a field. For discovered object XML, call `flow1c_query_schema` with the same source/path and RLM evidence ID. For accepted user XML, use `source=request` and its accepted relative path. The schema tool confirms only explicitly exported fields and tabular sections. Standard fields, virtual tables, computed fields, and database state remain unknown unless another suitable source explicitly proves them. `flow1c_cc_inspect meta-*` may provide supplementary detail; `skd-*` is for `Template.xml` only.

If the user explicitly says the source is unavailable or requests text-only analysis, go directly to `flow1c_query_check` without probing RLM or asking to restore it. If a source is expected to be available but RLM fails, follow the documented recovery once, retry, then continue the independent text analysis. Do not fill missing metadata from memory or a fuzzy search hit. Explain the limitation and, where necessary, use visibly provisional names or request the exact XML export.

## Check and deliver the exact candidate

Call `flow1c_query_check` with the full final `text`, relevant `schema_ids`, a short `expected_result` describing row grain, and material `assumptions`. For `optimize`, also pass the complete original `baseline_text` and a `changes` list explaining the proposed edits. Inspect every `SOURCE_UNVERIFIED`, `FIELD_UNVERIFIED`, syntax diagnostic, and limitation. Correct the candidate and call `flow1c_query_check` again if needed. The checker covers visible text and explicitly supplied XML; `PARTIAL` is normal without the 1C platform. Never call it compilation, execution, or a performance test.

Use `flow1c_complete` only after the final check. Present the exact `query_text` returned by `flow1c_complete` without manually rewriting it. In the answer, give the query, its intended row grain and logic, source/evidence IDs for confirmed names, unresolved fields or assumptions, and structural optimization hypotheses. State that compilation, real data, result correctness, plan, and speed were not tested in 1C. Do not claim `EXECUTED` or measured improvement without a separate platform integration that actually provides those results.

<!-- flow1c:routes:start -->
Generated from `config/intent-routes.json` and `config/stages.json`.

Interpret the user's goal, then check a RouteProposal before begin. A route grants no permissions.
CLI: `route-catalog --json`, `route-check --json-stdin` (direct proposal), then `agent-begin --json-stdin` (nested `route_proposal`).
OpenCode: `flow1c_route_catalog`, `flow1c_route_check(proposal_json)`, then `flow1c_begin(route_proposal_json)`; operation/mode/summary must match.
Minimal proposal shape: `{"schema_version":1,"expected_outcome":"user goal","operation":"consultation","mode":"explore","sources":[{"kind":"chat","version":"provided"}]}`. Replace operation/mode/sources for the actual request; `summary` and `route_id` are not proposal fields.
The pre-gate catalog returns `proposal_schema`; use it to correct invalid inputs without read/grep/bash. Do not retry the same invalid proposal unchanged.
Reuse saved answers; clarify one unresolved choice before gate. Do not launch subagents or a next formal stage automatically.
For large accepted documents use `flow1c_context(view=compact)` / `agent-context --view compact`. Read entries with `flow1c_context(action=read,entry_id=...,cursor=...)` / `context-read`; `scope` contains exact saved decisions and `index` lists every source/part. Continue cursors until mandatory coverage is complete. Summaries grant no evidence or permissions; changed sources require rebuilding on the same gate. Legacy full view remains the default.

| Operation | Mode → primary skill / role | Apply when | Exclude | Sources / output |
|---|---|---|---|---|
| query-analysis | explore → flow1c-query-analysis / —; draft → flow1c-query-analysis / — | Напиши запрос 1С для подсчёта остатков по складам.; Проверь приложенный текст запроса 1С без базы и XML. | Общее объяснение языка запросов; исполнение запроса в живой базе | chat, attachment, configuration, extension, git_snapshot / schemas/query-check.schema.json |
<!-- flow1c:routes:end -->
