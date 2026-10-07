"""Deterministic compatibility rules for project-level update gates."""

from __future__ import annotations

from typing import Any


def migrate_update_scope(gate: dict[str, Any]) -> dict[str, Any]:
    """Detach legacy work-item scope while retaining its historical references."""
    if gate.get("operation") != "update" or gate.get("mode", "formal") != "formal" or gate.get("completed_at"):
        return gate
    fields = ("code", "work_reference", "task_reference", "evidence_path",
              "manifest_status", "manifest_approvals", "traceability_mode")
    if not any(gate.get(field) for field in ("code", "work_reference", "task_reference", "evidence_path")):
        return gate
    migrated = {**gate}
    migrated.setdefault("update_scope_migration", {
        "schema_version": 1, "previous_work_item_context": {
            field: gate[field] for field in fields if field in gate
        },
    })
    migrated.update(code=None, work_reference=None, task_reference=None,
                    reference_source="none", work_item_exists=False)
    for field in ("evidence_path", "manifest_status", "manifest_approvals", "traceability_mode"):
        migrated.pop(field, None)
    return migrated
