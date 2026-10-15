"""Verify memory assessments and keep their scope pinned through completion.

Lock order: active gate, then memory project. Callers own the gate lock;
completed handoff producers never acquire the memory project lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from flow1c import context_manifest, memory, memory_store as store, storage
from flow1c import memory_review_policy as policy
from flow1c.errors import WorkflowError
from flow1c.handoff_policy import digest
from flow1c.memory_policy import ProjectMemoryError
from flow1c.results import OperationResult
from flow1c.workflow import state


def _project(gate: dict[str, Any], product_root: Path) -> store.Project:
    project = store.project(product_root=product_root)
    if gate.get("memory_project_id") != project[2] or gate.get("memory_data_root") != str(project[1]):
        raise ProjectMemoryError("MEMORY_SCOPE: documentation project changed; preserve this gate and reassess")
    return project


def _scope(gate: dict[str, Any], product_root: Path) -> str:
    keys = ("operation", "mode", "summary", "code", "work_reference", "request_id",
            "answers", "notes", "route_proposal", "git_ref", "action_completed", "template_result",
            "memory_seen_handoffs", "memory_observations")
    evidence = state.evidence_for_gate(gate, product_root=product_root)[1] if gate.get("evidence_path") else {}
    context = (context_manifest.memory_review_scope(gate, product_root=product_root)
               if evidence.get("context_manifest") else None)
    # Memory receipts and service timestamps are deliberately outside this projection.
    return digest({"gate": {key: gate.get(key) for key in keys},
                   "evidence": {k: v for k, v in evidence.items() if k not in {"created_at", "updated_at"}},
                   "context": context})


def observe(gate: dict[str, Any], result: dict[str, Any], *, action: str, project: store.Project) -> None:
    identifier = result.get("memory_id") if action == "remember" else result.get("record", {}).get("memory_id")
    if not identifier or action == "read" and result.get("state") != "WORKING":
        return
    record = store.read(project[1], identifier, project[2])
    revision = result.get("revision") if action == "remember" else result["record"]["revision"]
    if record["revision"] != revision:
        raise ProjectMemoryError("MEMORY_CONFLICT: record changed before observation; read it again")
    observations = gate.setdefault("memory_observations", {})
    previous = observations.get(identifier, {})
    saved = action == "remember" and result.get("state") == "SAVED"
    checked = policy.observation(record, saved=saved)
    if previous.get("sha256") == checked["sha256"] and previous.get("saved"):
        checked["saved"] = True
    observations[identifier] = checked


def _records(items: list[dict[str, Any]], gate: dict[str, Any], project: store.Project,
             product_root: Path) -> list[dict[str, Any]]:
    checked = {}
    for item in items:
        for ref in item["records"]:
            record = store.read(project[1], ref["memory_id"], project[2])
            seen = gate.get("memory_observations", {}).get(ref["memory_id"], {})
            sha = digest(record)
            if (record["revision"] != ref["revision"] or seen.get("revision") != ref["revision"]
                    or seen.get("sha256") != sha):
                raise ProjectMemoryError("MEMORY_REVIEW_REQUIRED: read or remember the exact record version in this gate")
            if item["kind"] != record["kind"]:
                raise ProjectMemoryError("MEMORY_REVIEW_INVALID: record kind does not cover the assessed finding")
            if item["disposition"] == "saved" and not seen.get("saved"):
                raise ProjectMemoryError("MEMORY_REVIEW_INVALID: saved finding needs a successful remember in this gate")
            if memory.freshness(record, project[0], product_root) != "WORKING":
                raise ProjectMemoryError("MEMORY_SOURCE_STALE: review needs a current verified source")
            checked[ref["memory_id"]] = {**ref, "sha256": sha}
    return sorted(checked.values(), key=lambda r: r["memory_id"])


def review(request: dict[str, Any], gate: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    items = policy.findings(request.get("findings"))
    if not gate.get("memory_overview_done"):
        raise ProjectMemoryError("MEMORY_REVIEW_REQUIRED: call overview before assessing reusable findings")
    project = _project(gate, product_root)
    with store.writer(project[0], project[1]):
        records = _records(items, gate, project, product_root)
        receipt = {"schema_version": 1, "project_id": project[2], "scope_sha256": _scope(gate, product_root),
                   "findings": items, "records": records, "status": policy.status(items),
                   "reviewed_at": storage.utc_now()}
        policy.validate_receipt(receipt)
        gate["memory_review"] = receipt
        state.save_gate(gate, product_root=product_root)
    return {"schema_version": 1, "state": "REVIEWED", **policy.summary(gate)}


@contextmanager
def completion_guard(gate: dict[str, Any], *, product_root: Path) -> Iterator[OperationResult | None]:
    if not gate.get("memory_review_required"):
        yield None
        return
    # Acquire the project lock before validation and hold it until completion is durable.
    from contextlib import ExitStack
    with ExitStack() as stack:
        try:
            if gate.get("memory_lifecycle_version") != policy.LIFECYCLE_VERSION:
                raise ProjectMemoryError("MEMORY_REVIEW_INVALID: unsupported lifecycle version")
            project = _project(gate, product_root)
            stack.enter_context(store.writer(project[0], project[1]))
            receipt = gate.get("memory_review")
            policy.validate_receipt(receipt)
            if not gate.get("memory_overview_done") or receipt["project_id"] != project[2]:
                raise ProjectMemoryError("MEMORY_REVIEW_REQUIRED: assess memory in the current project")
            if receipt["scope_sha256"] != _scope(gate, product_root):
                raise ProjectMemoryError("MEMORY_REVIEW_REQUIRED: inputs changed after review")
            if receipt["records"] != _records(receipt["findings"], gate, project, product_root):
                raise ProjectMemoryError("MEMORY_REVIEW_REQUIRED: records changed after review")
        except (WorkflowError, OSError, ValueError) as exc:
            yield OperationResult({"state": gate["state"], "ready": False, "code": "MEMORY_REVIEW_REQUIRED",
                                   "gate_id": gate["gate_id"], "preserved_state": True,
                                   "reason_code": getattr(exc, "code", "MEMORY_UNAVAILABLE"),
                                   "next_action": "Preserve the result. Call memory overview/read, remember new reusable findings with exact sources, then review and retry complete on this gate. Do not repeat the completed workflow action."}, 2)
            return
        yield None
