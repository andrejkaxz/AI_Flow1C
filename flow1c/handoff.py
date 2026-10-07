"""Immutable handoff store, completion receipts and recovery from saved records."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from flow1c import context as runtime, storage, work_items
from flow1c.errors import WorkflowError
from flow1c.handoff_policy import COMPLETED_STATES, HandoffError, build_manifest, digest, validate_manifest
from flow1c.results import OperationResult
from flow1c.workflow import state


def _no_links(path: Path) -> None:
    for current in (path, *path.parents):
        if current.is_symlink() or current.exists() and storage.is_reparse_or_symlink(current):
            raise HandoffError("HANDOFF_INVALID", "Handoff paths cannot contain symlinks/reparse points")


def _safe(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (not relative or "\\" in relative or ":" in relative or relative.startswith("/")
            or any(part in {".", ".."} for part in parts)):
        raise HandoffError("HANDOFF_INVALID", "Invalid managed relative handoff path")
    target = root / relative
    _no_links(target)
    if not storage.path_is_within(target, root) or target == root:
        raise HandoffError("HANDOFF_INVALID", "Handoff path escapes its managed root")
    return target


@contextmanager
def writer(gate_id: str, *, product_root: Path) -> Iterator[None]:
    """OS locks release on process death, including Windows; no stale lock deletion."""
    gate_path = state.gate_path(gate_id, product_root=product_root)
    lock = gate_path.with_suffix(".handoff.lock")
    _no_links(lock)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise HandoffError("HANDOFF_RECOVERY_REQUIRED", "Another completion/recovery writer is active", gate_id) from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _roots(gate: dict[str, Any], product_root: Path) -> tuple[Path, Path]:
    _no_links(product_root)
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    documentation = runtime.project_data_root(local, product_root=product_root)
    _no_links(documentation)
    if gate.get("mode", "formal") != "formal":
        base = (product_root / ".workspace" / "drafts" if gate.get("storage_kind") == "workspace" or
                not local.get("documentation_path") else documentation / "drafts")
        _no_links(base / str(gate.get("request_id", gate["gate_id"])))
        root = state.request_root(gate, product_root=product_root)
        store = root / "handoffs"
        if gate.get("request_id", gate["gate_id"]) != gate["gate_id"]:
            store = store / gate["gate_id"]
    elif gate.get("work_reference") or gate.get("code"):
        _no_links(documentation / "work-items")
        root = work_items.work_item_root(str(gate.get("work_reference") or gate["code"]), product_root=product_root)
        store = root / "evidence" / "handoffs" / gate["gate_id"]
    else:
        root = runtime.project_data_root(product_root=product_root)
        store = product_root / ".workspace" / "agent-handoffs" / gate["gate_id"]
    _no_links(root)
    _no_links(store)
    return root, store


def load_producer(gate_id: str, *, product_root: Path) -> dict[str, Any]:
    """Completed records are data across policy updates, never executable gates."""
    path = state.gate_path(gate_id, product_root=product_root)
    _no_links(path)
    gate = storage.read_json(path)
    if gate is None:
        # Existing completion callers retain the published missing-gate error.
        return state.load_gate(gate_id, product_root=product_root)
    if (not isinstance(gate, dict) or type(gate.get("schema_version")) is not int
            or gate["schema_version"] != 1 or gate.get("gate_id") != gate_id):
        raise HandoffError("HANDOFF_INVALID", "Invalid producer ownership or gate schema", gate_id)
    errors = state.validate_json_record(gate, product_root / "schemas/agent-gate.schema.json")
    if errors:
        raise HandoffError("HANDOFF_INVALID", "Invalid producer gate contract", gate_id)
    completed = gate.get("state") in COMPLETED_STATES and (
        gate.get("state") != "UNVERIFIED_DRAFT" or gate.get("completed_at"))
    if not completed:
        return state.load_gate(gate_id, product_root=product_root)
    # Keep the sealed historical route and answers exactly as completed. Current
    # policy is reapplied only by begin/load_gate before new work, not by transfer.
    return gate


def _evidence(gate: dict[str, Any], product_root: Path) -> tuple[Path | None, dict[str, Any]]:
    if not gate.get("evidence_path"):
        return None, {}
    _no_links(Path(gate["evidence_path"]))
    path, record = state.evidence_for_gate(gate, product_root=product_root)
    if record.get("gate_id") not in {None, gate["gate_id"]} or record.get("operation") != gate["operation"]:
        raise HandoffError("HANDOFF_INVALID", "Evidence does not belong to the producer")
    return path, record


def _gate_record(gate: dict[str, Any]) -> dict[str, Any]:
    # Permissions are never imported from a handoff. Actions are recalculated by load_gate.
    fields = ("schema_version", "policy_version", "gate_id", "request_id", "operation", "mode", "skill", "summary", "state",
              "completed_at", "route_decision", "answers", "notes", "deviations", "compliance",
              "manifest_status", "manifest_approvals", "action_completed", "action_result",
              "update_result", "template_result", "template_operations", "query_verification",
              "completion_errors", "output", "result_summary", "work_reference", "code",
              "project_reference", "task_reference", "git_ref", "storage_kind")
    return {key: gate[key] for key in fields if key in gate}


def _evidence_digest(record: dict[str, Any]) -> str:
    return digest({k: v for k, v in record.items() if k != "handoff"})


def _artifact(root: Path, path: str, expected: str | None, kind: str) -> dict[str, Any]:
    raw = Path(path)
    if raw.is_absolute():
        _no_links(raw)
        if not storage.path_is_within(raw, root):
            raise HandoffError("HANDOFF_INVALID", "Result is outside the managed producer root")
        path = raw.relative_to(root).as_posix()
    target = _safe(root, path)
    if not target.is_file():
        raise HandoffError("HANDOFF_STALE", "Recorded result is missing")
    actual = storage.sha256(target)
    if expected and actual != expected:
        raise HandoffError("HANDOFF_STALE", "Recorded result hash changed")
    return {"path": path, "kind": kind, "sha256": actual,
            "version": expected or actual, "recorded_by_tool": bool(expected)}


def seal(gate: dict[str, Any], result: dict[str, Any], *, product_root: Path,
         evidence_patch: dict[str, Any] | None = None, legacy: bool = False) -> dict[str, Any]:
    """Freeze validated identities before committing completion; never re-bless changed files."""
    root, _ = _roots(gate, product_root)
    evidence_path, evidence = _evidence(gate, product_root)
    evidence = {**evidence, **(evidence_patch or {})}
    artifacts: dict[str, dict[str, Any]] = {}
    for item in evidence.get("changed_files", []):
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise HandoffError("HANDOFF_INVALID", "Invalid tool result index")
        # Multiple writes of the same path are historical; only the latest version is a result.
        artifacts[str(item["path"])] = item
    selected = [_artifact(root, path, item["sha256"], "tool-output") for path, item in artifacts.items()]
    output = result.get("output") or gate.get("output")
    if output:
        record = _artifact(root, str(output), None, "completion-output")
        if not any(item["path"] == record["path"] for item in selected):
            # Template/interview completions pin their own output hash.
            pin = gate.get("interview_output") or gate.get("template_result") or {}
            expected = pin.get("sha256") or pin.get("output_sha256")
            selected.append(_artifact(root, str(output), expected, "completion-output"))
    records = [{"kind": "gate", "sha256": digest(_gate_record(gate))}]
    if evidence_path:
        records.append({"kind": "evidence", "path": evidence_path.relative_to(root).as_posix(),
                        "sha256": _evidence_digest(evidence)})
    manifest = None
    if gate.get("mode", "formal") == "formal" and (gate.get("work_reference") or gate.get("code")):
        manifest_path, manifest = work_items.load_manifest(str(gate.get("work_reference") or gate["code"]), product_root=product_root)
        _no_links(manifest_path)
        records.append({"kind": "work-item", "path": manifest_path.relative_to(root).as_posix(), "sha256": digest(manifest)})
    return {"schema_version": 1, "legacy": legacy, "result_sha256": digest(result),
            "artifacts": selected, "records": records,
            "evidence_owned": bool(evidence.get("gate_id") == gate["gate_id"]),
            "approvals": {"record": "work-item" if manifest is not None else None,
                          "values": manifest.get("approvals", {}) if manifest is not None else {}},
            "evidence_patch": evidence_patch or {}}


def save_completion(gate: dict[str, Any], result: OperationResult, *, product_root: Path,
                    evidence_patch: dict[str, Any] | None = None) -> None:
    gate.setdefault("completed_at", storage.utc_now())
    gate["completion_record"] = {
        "schema_version": 1, "result": result.value, "exit_code": result.exit_code,
        "seal": seal(gate, result.value, product_root=product_root, evidence_patch=evidence_patch),
    }
    gate["handoff_status"] = "RECOVERY_REQUIRED"
    # This is the durable completion receipt, preceding any evidence/request copy.
    state.save_gate(gate, product_root=product_root)


def verify_completion(gate: dict[str, Any], receipt: dict[str, Any], *, product_root: Path) -> None:
    if not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        raise HandoffError("HANDOFF_INVALID", "Unsupported completion receipt")
    if (not isinstance(receipt.get("result"), dict) or receipt["result"].get("state") != gate.get("state")
            or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0):
        raise HandoffError("HANDOFF_INVALID", "Invalid saved completion result")
    frozen = receipt.get("seal", {})
    if not isinstance(frozen, dict) or type(frozen.get("schema_version")) is not int or frozen["schema_version"] != 1:
        raise HandoffError("HANDOFF_INVALID", "Unsupported completion seal")
    if not isinstance(frozen.get("evidence_patch"), dict) or type(frozen.get("legacy")) is not bool:
        raise HandoffError("HANDOFF_INVALID", "Invalid completion seal fields")
    current = seal(gate, receipt["result"], product_root=product_root,
                   evidence_patch=frozen.get("evidence_patch") if gate.get("handoff_status") != "READY" else None,
                   legacy=frozen.get("legacy", False))
    current["evidence_patch"] = frozen.get("evidence_patch", {})
    if current != frozen:
        raise HandoffError("HANDOFF_STALE", "Authoritative results, source records or approvals changed")


def _events(store: Path) -> list[dict[str, Any]]:
    directory = store / "journal"
    _no_links(directory)
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    records = []
    previous = None
    for index, path in enumerate(paths, 1):
        _no_links(path)
        record = storage.read_json(path)
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int or
                record["schema_version"] != 1 or type(record.get("sequence")) is not int or
                record["sequence"] != index or path.name != f"{index:08d}.json" or
                record.get("previous") != previous or
                record.get("event") not in {"recovery-started", "recovery-failed", "recovery-complete"}):
            raise HandoffError("HANDOFF_INVALID", "Handoff journal chain is invalid")
        previous = digest(record)
        records.append(record)
    return records


def _journal(store: Path, event: str, **fields: Any) -> None:
    records = _events(store)
    previous = digest(records[-1]) if records else None
    record = {"schema_version": 1, "sequence": len(records) + 1, "previous": previous,
              "event": event, "created_at": storage.utc_now(), **fields}
    storage.write_json(store / "journal" / f"{len(records) + 1:08d}.json", record)


def recover_locked(gate: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    """Caller holds writer; reconstruct only the sealed completed result."""
    if gate.get("state") not in COMPLETED_STATES or (gate.get("state") == "UNVERIFIED_DRAFT" and not gate.get("completed_at")):
        raise HandoffError("HANDOFF_INVALID", "Handoff requires a completed producer", gate["gate_id"])
    root, store = _roots(gate, product_root)
    reference = gate.get("handoff")
    if reference is not None and (not isinstance(reference, dict) or
            type(reference.get("schema_version")) is not int or reference["schema_version"] != 1):
        raise HandoffError("HANDOFF_INVALID", "Unsupported saved handoff reference; state is preserved", gate["gate_id"])
    _evidence(gate, product_root)
    if gate.get("completion_record") is not None:
        verify_completion(gate, gate["completion_record"], product_root=product_root)
    _journal(store, "recovery-started", gate_id=gate["gate_id"])
    try:
        receipt = gate.get("completion_record")
        if receipt is None:
            # Explicit additive legacy migration, with incomplete provenance.
            value = {"state": gate["state"], "output": gate.get("output"),
                     "document_status": gate.get("document_status"), "legacy": True}
            receipt = {"schema_version": 1, "result": value, "exit_code": 0,
                       "seal": seal(gate, value, product_root=product_root, legacy=True)}
            gate["completion_record"] = receipt
            gate["handoff_status"] = "RECOVERY_REQUIRED"
            state.save_gate(gate, product_root=product_root)
        verify_completion(gate, receipt, product_root=product_root)
        evidence_path, evidence = _evidence(gate, product_root)
        if receipt["seal"]["evidence_patch"]:
            evidence.update(receipt["seal"]["evidence_patch"])
        manifest = build_manifest(gate, receipt, evidence)
        # The full digest lives in the record/reference; short names keep Windows
        # atomic temporary paths usable in nested standalone checkouts.
        target = _safe(store, f"{manifest['record_id'][:16]}.json")
        if target.exists():
            if storage.read_json(target) != manifest:
                raise HandoffError("HANDOFF_INVALID", "An immutable handoff version was altered")
        else:
            storage.write_json(target, manifest)
        reference = {"schema_version": 1, "path": target.relative_to(store).as_posix(), "sha256": storage.sha256(target)}
        if evidence_path:
            evidence["handoff"] = reference
            storage.write_json(evidence_path, evidence)
        gate.update(handoff=reference, handoff_status="READY")
        state.save_request(gate, product_root=product_root)
        _journal(store, "recovery-complete", gate_id=gate["gate_id"], record_id=manifest["record_id"])
        return manifest
    except (OSError, ValueError, WorkflowError) as exc:
        try:
            _journal(store, "recovery-failed", gate_id=gate["gate_id"], code=getattr(exc, "code", "HANDOFF_RECOVERY_REQUIRED"))
        except (OSError, WorkflowError):
            pass
        raise


def read_locked(gate: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    _, store = _roots(gate, product_root)
    _events(store)
    reference = gate.get("handoff")
    if not reference:
        raise HandoffError("HANDOFF_RECOVERY_REQUIRED", "Completed producer has no handoff", gate["gate_id"])
    if not isinstance(reference, dict) or type(reference.get("schema_version")) is not int or reference["schema_version"] != 1:
        raise HandoffError("HANDOFF_INVALID", "Unsupported handoff reference")
    target = _safe(store, str(reference.get("path", "")))
    if not target.is_file() or storage.sha256(target) != reference.get("sha256"):
        raise HandoffError("HANDOFF_INVALID", "Handoff digest mismatch")
    manifest = storage.read_json(target)
    validate_manifest(manifest)
    verify_completion(gate, gate.get("completion_record", {}), product_root=product_root)
    _, evidence = _evidence(gate, product_root)
    if manifest != build_manifest(gate, gate["completion_record"], evidence):
        raise HandoffError("HANDOFF_INVALID", "Handoff differs from its authoritative producer")
    if evidence and evidence.get("handoff") != reference:
        raise HandoffError("HANDOFF_RECOVERY_REQUIRED", "Evidence handoff reference needs recovery")
    return manifest


def command(gate_id: str, action: str, *, product_root: Path) -> OperationResult:
    try:
        if not isinstance(action, str) or action not in {"read", "recover"}:
            raise HandoffError("HANDOFF_INVALID", "Unknown handoff action", gate_id)
        with writer(gate_id, product_root=product_root):
            gate = load_producer(gate_id, product_root=product_root)
            manifest = recover_locked(gate, product_root=product_root) if action == "recover" else read_locked(gate, product_root=product_root)
            return OperationResult({"gate_id": gate_id, "state": gate["state"], "handoff": manifest,
                                    "next_action": manifest["next_action"]})
    except (OSError, ValueError, WorkflowError) as exc:
        error = exc if isinstance(exc, HandoffError) else HandoffError("HANDOFF_RECOVERY_REQUIRED", str(exc), gate_id)
        error.gate_id = gate_id
        return OperationResult(error.payload(), 2)
