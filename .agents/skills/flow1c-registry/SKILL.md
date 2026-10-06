---
name: flow1c-registry
description: Import, validate, reconcile, and assess impact from the project Excel registers of processes, requirements, and functional specifications. Use when a register is added or updated, or when requirement status is requested.
---

# FLOW1C registry

If validation is structurally impossible, keep the structured `errors`, `warnings`, and `report_path` available to the user. Local row and relation errors produce `partial`: use the scoped index for the selected requirement or FS and offer ordinary `fs-start` when that scope is usable. Offer a typed `registry_bypass` only when the selected scope cannot be resolved, or an independent draft. Never interpret a generic deviation as permission to bypass the registry.

Use `registry-reconcile` for a corrected workbook. Apply only exact or user-confirmed mappings, preserve old evidence, and leave the work item provisional when any mapping is disputed.

Read `docs/lifecycle.md` when assessing changes to an existing work item.

Accept a new workbook from any user-selected location or from `<documentation_path>/inbox/`. Run `scripts/flow1c.py registry-import --file <xlsx>` from the workflow repository; the CLI writes results to the external documentation repository configured in `.flow1c.local.json`. `--allow-errors` remains a compatibility alias; partial indexes are written by default and never replace the last fully verified normalized indexes.

MVP invariants:

- column J is the authoritative requirement ID;
- the user assigns every project/task reference; it is opaque and may use the legacy `G-xxx` format but Flow1C never requires or invents it;
- one FS may contain several requirements;
- one requirement may belong to at most one FS;
- source rows are never silently deleted from project history;
- a changed requirement produces an impact warning, not an automatic PR.

Read `<documentation_path>/registry/import-report.md`. Resolve errors in the selected requirement/FS scope before creating a work item; unrelated local errors remain visible but do not block scoped work. Treat generated JSON as an index, not as a replacement for the versioned source workbook.
