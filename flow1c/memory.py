"""Bounded project memory operations; stored text is untrusted navigation data."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flow1c import documentation, handoff, memory_policy as policy, memory_store as store, storage
from flow1c.errors import WorkflowError
from flow1c.handoff_policy import digest
from flow1c.memory_policy import ProjectMemoryError
from flow1c.workflow import state


def _freshness(record: dict[str, Any], root: Path, product_root: Path) -> str:
    source = record["source"]
    try:
        if source["type"] == "context_entry":
            target = documentation.regular_path(root / source["path"])
            if not storage.path_is_within(target, root) or not target.is_file():
                return "MISSING"
            actual = storage.sha256(target)
        elif source["type"] in {"dialogue_answer", "dialogue_note"}:
            gate = handoff.load_producer(source["gate_id"], product_root=product_root)
            if gate.get("memory_project_id") != record["project_id"]:
                return "MISSING"
            items = gate.get("answers", []) if source["type"] == "dialogue_answer" else gate.get("notes", [])
            index = source.get("answer_index", -1) if source["type"] == "dialogue_answer" else source.get("note_index", -1)
            actual = digest(items[index]) if 0 <= index < len(items) else None
        else:
            gate = handoff.load_producer(source["gate_id"], product_root=product_root)
            if not handoff.producer_in_project(gate, root, record["project_id"], product_root):
                return "MISSING"
            with handoff.writer(source["gate_id"], product_root=product_root):
                handoff.read_locked(gate, product_root=product_root)
            actual = gate.get("handoff", {}).get("sha256")
    except (OSError, ValueError, WorkflowError):
        return "MISSING"
    return policy.freshness(source["sha256"], actual)


def _summary(record: dict[str, Any], root: Path, product_root: Path) -> dict[str, Any]:
    return {"memory_id": record["memory_id"], "revision": record["revision"],
            "kind": record["kind"], "subject": record["subject"],
            "text": record["text"][:240], "state": _freshness(record, root, product_root),
            "source": record["source"]}


def _bounded(value: dict[str, Any], limit: int, items: list[Any]) -> dict[str, Any]:
    while policy.response_size(value) > limit and items:
        items.pop()
        value["truncated"] = True
    if policy.response_size(value) > limit:
        raise ProjectMemoryError("MEMORY_LIMIT: response metadata exceeds max_chars")
    return value


def search(request: dict[str, Any], project: store.Project, *, product_root: Path) -> dict[str, Any]:
    root, directory, project_id = project
    query = request.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        raise ProjectMemoryError("MEMORY_INVALID: query must be at most 200 characters")
    kind = request.get("kind")
    if kind is not None and kind not in policy.KINDS:
        raise ProjectMemoryError("MEMORY_INVALID: unknown kind")
    limit = policy.response_limit(request.get("max_chars", 8000))
    records, complete, warnings = store.scan(directory, project_id)
    ranked = sorted((r for r in records if (kind is None or r["kind"] == kind) and policy.score(r, query)),
                    key=lambda r: (policy.score(r, query), r["updated_at"]), reverse=True)
    matches = [_summary(record, root, product_root) for record in ranked[:policy.MAX_RESULTS]]
    value = {"schema_version": 1, "state": "RESULTS", "project_id": project_id,
             "matches": matches, "scan_complete": complete, "warnings": warnings,
             "truncated": len(ranked) > len(matches)}
    return _bounded(value, limit, matches)


def read(request: dict[str, Any], project: store.Project, *, product_root: Path) -> dict[str, Any]:
    root, directory, project_id = project
    identifier = policy.memory_id(request.get("memory_id"))
    revision = policy.revision(request.get("revision"))
    record = store.read(directory, identifier, project_id)
    if record["revision"] != revision:
        raise ProjectMemoryError("MEMORY_CONFLICT: record revision changed; search again")
    return {"schema_version": 1, "state": _freshness(record, root, product_root),
            "record": record, "next_action": "Recheck the source before relying on this statement"}


def overview(request: dict[str, Any], gate: dict[str, Any], project: store.Project, *, product_root: Path) -> dict[str, Any]:
    root, directory, project_id = project
    query = request.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        raise ProjectMemoryError("MEMORY_INVALID: query must be at most 200 characters")
    limit = policy.response_limit(request.get("max_chars", 8000))
    records, complete, warnings = store.scan(directory, project_id)
    ranked = sorted((r for r in records if policy.score(r, query)),
                    key=lambda r: (policy.score(r, query), r["updated_at"]), reverse=True)
    matches = [_summary(item, root, product_root) for item in ranked[:policy.MAX_RESULTS]]
    handoffs: list[dict[str, Any]] = []
    gate_dir = product_root / ".workspace" / "agent-gates"
    if gate_dir.exists():
        handoff._no_links(gate_dir)
        files = sorted(gate_dir.glob("*.json"), key=lambda p: p.lstat().st_mtime, reverse=True)
        if len(files) > 100:
            complete = False
            warnings.append("HANDOFF_SCAN_LIMIT")
        for index, path in enumerate(files[:100]):
            try:
                candidate = handoff.load_producer(path.stem, product_root=product_root)
                if not handoff.producer_in_project(candidate, root, project_id, product_root) or candidate.get("handoff_status") != "READY":
                    continue
                with handoff.writer(path.stem, product_root=product_root):
                    manifest = handoff.read_locked(candidate, product_root=product_root)
                handoffs.append({"gate_id": path.stem, "goal": str(manifest.get("goal", ""))[:200],
                                 "completion": str(manifest.get("completion", {}).get("summary") or "")[:240],
                                 "open_questions": manifest.get("open_questions", [])[:3],
                                 "sha256": candidate["handoff"]["sha256"]})
                if len(handoffs) >= 5:
                    if index + 1 < len(files[:100]):
                        complete = False
                        warnings.append("HANDOFF_RESULT_LIMIT")
                    break
            except (OSError, ValueError, WorkflowError) as exc:
                complete = False
                warnings.append(f"{path.name}: {exc}")
    value = {"schema_version": 1, "state": "OVERVIEW", "project_id": project_id,
             "handoffs": handoffs, "matches": matches, "scan_complete": complete,
             "warnings": warnings[:5], "truncated": len(ranked) > len(matches) or
             any(item in warnings for item in ("HANDOFF_RESULT_LIMIT", "HANDOFF_SCAN_LIMIT"))}
    _bounded(value, limit, matches)
    _bounded(value, limit, handoffs)
    gate["memory_seen_handoffs"] = {item["gate_id"]: item["sha256"] for item in handoffs}
    state.save_gate(gate, product_root=product_root)
    return value


def remember(request: dict[str, Any], gate: dict[str, Any], source: dict[str, Any], project: store.Project,
             *, product_root: Path) -> dict[str, Any]:
    root, directory, project_id = project
    kind, basis = request.get("kind"), request.get("basis")
    if kind not in policy.KINDS or basis not in policy.BASES:
        raise ProjectMemoryError("MEMORY_INVALID: unsupported kind or basis")
    subject = policy.bounded_text(request.get("subject"), "subject", 160)
    body = policy.bounded_text(request.get("text"), "text", 800)
    aliases = request.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 8:
        raise ProjectMemoryError("MEMORY_INVALID: aliases must have at most eight items")
    aliases = [policy.bounded_text(item, "alias", 80) for item in aliases]
    identifier = request.get("memory_id")
    if identifier is not None:
        identifier = policy.memory_id(identifier)
    expected = request.get("expected_revision")
    if identifier and expected is None or not identifier and expected is not None:
        raise ProjectMemoryError("MEMORY_INVALID: updates need memory_id and expected_revision")
    if expected is not None:
        policy.revision(expected)
    candidate = {"kind": kind, "subject": subject, "aliases": aliases, "text": body,
                 "basis": basis, "source": source}
    with store.writer(root, directory):
        if identifier:
            previous = store.read(directory, identifier, project_id)
        else:
            records, complete, warnings = store.scan(directory, project_id)
            if not complete:
                raise ProjectMemoryError("MEMORY_CORRUPT: inspect existing records before adding another")
            replay = next((item for item in records if item["origin_gate_id"] == gate["gate_id"]
                           and all(item[key] == candidate[key] for key in candidate)), None)
            if replay:
                return {"schema_version": 1, "state": "UNCHANGED", "memory_id": replay["memory_id"],
                        "revision": replay["revision"], "source": source}
            if len(records) >= policy.MAX_RECORDS:
                raise ProjectMemoryError("MEMORY_LIMIT: project record limit reached")
            previous = None
            identifier = store.new_id()
        if previous and previous["revision"] != expected:
            raise ProjectMemoryError("MEMORY_CONFLICT: revision changed; read it before replacing")
        if previous and all(previous[key] == value for key, value in candidate.items()):
            return {"schema_version": 1, "state": "UNCHANGED", "memory_id": identifier,
                    "revision": previous["revision"], "source": source}
        now = storage.utc_now()
        value = {"schema_version": 1, "memory_id": identifier, "revision": 1 if previous is None else previous["revision"] + 1,
                 "project_id": project_id, **candidate, "origin_gate_id": gate["gate_id"],
                 "created_at": previous["created_at"] if previous else now, "updated_at": now, "state": "WORKING"}
        store.validate(value, identifier, project_id)
        if _freshness(value, root, product_root) != "WORKING":
            raise ProjectMemoryError("MEMORY_SOURCE_STALE: source changed before save")
        store.put(directory, value)
    return {"schema_version": 1, "state": "SAVED", "memory_id": identifier,
            "revision": value["revision"], "source": source, "project_id": project_id}


def forget(request: dict[str, Any], project: store.Project, *, product_root: Path) -> dict[str, Any]:
    root, directory, project_id = project
    identifier = policy.memory_id(request.get("memory_id"))
    expected = policy.revision(request.get("expected_revision"))
    if request.get("confirmed") is not True:
        raise ProjectMemoryError("MEMORY_CONFIRMATION_REQUIRED: explicit user request is required")
    with store.writer(root, directory):
        previous = store.read(directory, identifier, project_id)
        if previous["revision"] != expected:
            raise ProjectMemoryError("MEMORY_CONFLICT: revision changed; read it before deleting")
        store.record_path(directory, identifier).unlink()
    return {"schema_version": 1, "state": "FORGOTTEN", "memory_id": identifier,
            "boundary": "Only the local memory record was deleted; source documents, handoffs and wiki remain."}
