"""Gate and evidence authorization for the shared memory service."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from flow1c import context_manifest, handoff, memory, memory_store, storage
from flow1c.errors import WorkflowError
from flow1c.handoff_policy import digest
from flow1c.memory_policy import ProjectMemoryError
from flow1c.results import OperationResult
from flow1c.workflow import state

FIELDS = {
    "overview": {"query", "max_chars"},
    "search": {"query", "kind", "max_chars"},
    "read": {"memory_id", "revision"},
    "remember": {"kind", "subject", "aliases", "text", "basis", "source", "memory_id", "expected_revision"},
    "forget": {"memory_id", "expected_revision", "confirmed"},
}


def _authorized_source(source: Any, basis: str, gate: dict[str, Any], project: memory_store.Project,
                       product_root: Path) -> dict[str, Any]:
    if not isinstance(source, dict) or set(source) - {"type", "entry_id", "answer_index", "note_index", "gate_id", "locator", "sha256"}:
        raise ProjectMemoryError("MEMORY_SOURCE: invalid source fields")
    kind = source.get("type")
    project_root, _, project_id = project
    locator = source.get("locator")
    if not isinstance(locator, str) or not locator.strip() or len(locator) > 200:
        raise ProjectMemoryError("MEMORY_SOURCE: a precise locator is required")
    if basis == "accepted_document" and kind == "context_entry":
        if not re.fullmatch(r"(?:line:\d+(?:-\d+)?|section:[^\x00-\x1f]{1,160}|sheet:[^\x00-\x1f]{1,160}|artifact:[^\x00-\x1f]{1,160})", locator.strip()):
            raise ProjectMemoryError("MEMORY_SOURCE: document locator needs line, section, sheet or artifact identity")
        try:
            target, entry = context_manifest.verified_memory_entry(gate, source.get("entry_id"),
                                                                    locator.strip(), product_root=product_root)
        except WorkflowError as exc:
            code = "MEMORY_SOURCE_STALE" if getattr(exc, "code", "") == "CONTEXT_CURSOR_STALE" else "MEMORY_SOURCE"
            raise ProjectMemoryError(f"{code}: {exc}") from exc
        if not storage.path_is_within(target, project_root) or not target.is_file():
            raise ProjectMemoryError("MEMORY_SOURCE: accepted source is outside documentation")
        if storage.sha256(target) != entry["sha256"] or source.get("sha256") != entry["sha256"]:
            raise ProjectMemoryError("MEMORY_SOURCE_STALE: source changed since the recorded read")
        return {"type": kind, "path": target.relative_to(project_root).as_posix(),
                "locator": locator.strip(), "sha256": entry["sha256"],
                "gate_id": gate["gate_id"], "entry_id": entry["entry_id"]}
    if basis == "user_statement" and kind == "dialogue_answer":
        index = source.get("answer_index")
        answers = gate.get("answers", [])
        if type(index) is not int or not 0 <= index < len(answers):
            raise ProjectMemoryError("MEMORY_SOURCE: saved dialogue answer is required")
        answer = answers[index]
        if not isinstance(answer, dict) or not answer.get("answer"):
            raise ProjectMemoryError("MEMORY_SOURCE: answer has no saved user statement")
        sha = digest(answer)
        if source.get("sha256") not in {None, sha}:
            raise ProjectMemoryError("MEMORY_SOURCE_STALE: answer digest differs")
        return {"type": kind, "path": "", "locator": f"answer:{index}: {locator.strip()}",
                "sha256": sha, "gate_id": gate["gate_id"], "answer_index": index}
    if basis == "user_statement" and kind == "dialogue_note":
        index = source.get("note_index")
        notes = gate.get("notes", [])
        if type(index) is not int or not 0 <= index < len(notes) or notes[index].get("kind") != "user_answer":
            raise ProjectMemoryError("MEMORY_SOURCE: saved user_answer note is required")
        sha = digest(notes[index])
        if source.get("sha256") not in {None, sha}:
            raise ProjectMemoryError("MEMORY_SOURCE_STALE: user answer digest differs")
        return {"type": kind, "path": "", "locator": f"note:{index}: {locator.strip()}",
                "sha256": sha, "gate_id": gate["gate_id"], "note_index": index}
    if basis == "completed_result" and kind == "sealed_handoff":
        producer_id = source.get("gate_id")
        if not isinstance(producer_id, str) or gate.get("memory_seen_handoffs", {}).get(producer_id) != source.get("sha256"):
            raise ProjectMemoryError("MEMORY_SOURCE: first select the sealed handoff through overview")
        producer = handoff.load_producer(producer_id, product_root=product_root)
        if not handoff.producer_in_project(producer, project_root, project_id, product_root):
            raise ProjectMemoryError("MEMORY_SCOPE: handoff belongs to another project")
        with handoff.writer(producer_id, product_root=product_root):
            handoff.read_locked(producer, product_root=product_root)
        if producer["handoff"]["sha256"] != source["sha256"]:
            raise ProjectMemoryError("MEMORY_SOURCE_STALE: sealed handoff changed")
        return {"type": kind, "path": "", "locator": locator.strip(),
                "sha256": source["sha256"], "gate_id": producer_id}
    raise ProjectMemoryError("MEMORY_SOURCE: basis and source type do not match")


def command(args: Any, *, product_root: Path) -> OperationResult:
    action, request = args.action, args.request
    if action not in FIELDS or not isinstance(request, dict) or set(request) - FIELDS[action]:
        raise ProjectMemoryError("MEMORY_INVALID: invalid action or request fields")
    try:
        gate = state.load_gate(args.gate_id, states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"}, product_root=product_root)
    except WorkflowError as exc:
        raise ProjectMemoryError(f"MEMORY_GATE: {exc}") from exc
    if gate.get("completed_at"):
        raise ProjectMemoryError("MEMORY_GATE: completed gate cannot use memory")
    try:
        state.require_gate_tool(gate, "flow1c_memory", product_root=product_root)
    except WorkflowError as exc:
        raise ProjectMemoryError(f"MEMORY_GATE: {exc}") from exc
    try:
        project = memory_store.project(product_root=product_root)
    except WorkflowError as exc:
        raise ProjectMemoryError(f"MEMORY_SCOPE: {exc}") from exc
    _, directory, project_id = project
    if gate.get("memory_project_id") != project_id or gate.get("memory_data_root") != str(directory):
        raise ProjectMemoryError("MEMORY_SCOPE: begin a new gate for the configured documentation project")
    if action == "overview":
        result = memory.overview(request, gate, project, product_root=product_root)
    elif action == "search":
        result = memory.search(request, project, product_root=product_root)
    elif action == "read":
        result = memory.read(request, project, product_root=product_root)
    elif action == "remember":
        source = _authorized_source(request.get("source"), request.get("basis"), gate, project, product_root)
        result = memory.remember(request, gate, source, project, product_root=product_root)
    else:
        statements = [str(gate.get("summary") or ""),
                      *(str(item.get("answer") or "") for item in gate.get("answers", []) if isinstance(item, dict)),
                      *(str(item.get("text") or "") for item in gate.get("notes", [])
                        if isinstance(item, dict) and item.get("kind") == "user_answer")]
        if not any(re.search(r"(забуд|удал|forget|delete).{0,60}(памят|memory|запис)", item, re.I) for item in statements):
            raise ProjectMemoryError("MEMORY_CONFIRMATION_REQUIRED: current gate must record the user's deletion request")
        result = memory.forget(request, project, product_root=product_root)
    return OperationResult(result)
