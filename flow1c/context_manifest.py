"""Immutable context sources/manifests and atomic, gate-owned read coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from flow1c import context as runtime, handoff, storage, work_items
from flow1c.context_policy import (ContextError, compact_text, coverage_summary, digest,
                                  merge_ranges, parts, scope_record, source_specs, validate_limits, validate_records)
from flow1c.results import OperationResult
from flow1c.errors import WorkflowError
from flow1c.workflow import state

TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".diff", ".patch"}


def _no_links(path: Path) -> None:
    for current in (path, *path.parents):
        if current.is_symlink() or current.exists() and storage.is_reparse_or_symlink(current):
            raise ContextError("CONTEXT_INVALID", "Context paths cannot contain symlinks or reparse points")


def _safe(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/"))):
        raise ContextError("CONTEXT_INVALID", "Invalid managed context path")
    target = root.joinpath(*PurePosixPath(relative).parts)
    _no_links(target)
    if not storage.path_is_within(target, root):
        raise ContextError("CONTEXT_INVALID", "Context path escapes its managed root")
    return target


def _limits(product_root: Path) -> dict[str, int]:
    return validate_limits(storage.read_json(product_root / "config/context.json"))


def _roots(gate: dict[str, Any], product_root: Path) -> tuple[Path, Path, dict[str, Any]]:
    _no_links(product_root)
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    if local.get("documentation_path"):
        _no_links(Path(local["documentation_path"]).expanduser())
    documentation = runtime.project_data_root(local, product_root=product_root)
    if gate.get("mode", "formal") == "formal":
        reference = str(gate.get("work_reference") or gate.get("code") or "")
        if not reference:
            raise ContextError("CONTEXT_SCOPE_REQUIRED", "This formal operation has no document context scope")
        _no_links(documentation / "work-items")
        root = work_items.work_item_root(reference, product_root=product_root)
        _no_links(root)
        _no_links(root / "manifest.yaml")
        _, manifest = work_items.load_manifest(reference, product_root=product_root)
    else:
        base = (product_root / ".workspace/drafts" if gate.get("storage_kind") == "workspace" or not local.get("documentation_path")
                else documentation / "drafts")
        _no_links(base / str(gate.get("request_id", gate["gate_id"])))
        root = state.request_root(gate, product_root=product_root)
        manifest = {}
    _no_links(root)
    _no_links(Path(str(gate.get("evidence_path", ""))))
    evidence_path, _ = state.evidence_for_gate(gate, product_root=product_root)
    _no_links(evidence_path)
    store = evidence_path.parent / "contexts" / gate["gate_id"]
    _no_links(store)
    return root, store, manifest


def _specs(root: Path, gate: dict[str, Any], manifest: dict[str, Any], evidence: dict[str, Any],
           *, product_root: Path) -> list[dict[str, Any]]:
    artifacts = (storage.read_json(_safe(root, "input/artifacts.json"), {}).get("artifacts", [])
                 if gate.get("mode", "formal") == "formal" else evidence.get("artifacts", []))
    if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
        raise ContextError("CONTEXT_INVALID", "Invalid accepted artifact index")
    if gate.get("mode", "formal") == "formal":
        # Formal intake persists documentation-relative paths; the reader owns only this work-item.
        prefix = root.relative_to(runtime.project_root(product_root=product_root)).as_posix() + "/"
        normalized = []
        for artifact in artifacts:
            selected = dict(artifact)
            for field in ("relative_path", "path", "derived_path"):
                value = selected.get(field)
                if isinstance(value, str) and value.startswith(prefix):
                    selected[field] = value[len(prefix):]
                elif isinstance(value, str) and value.startswith(("work-items/", ".flow1c/work-items/")):
                    raise ContextError("CONTEXT_INVALID", "Accepted context artifact belongs to another work-item")
            normalized.append(selected)
        artifacts = normalized
    return source_specs(gate, manifest, artifacts)


def _immutable(store: Path, kind: str, value: dict[str, Any]) -> dict[str, Any]:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    sha = hashlib.sha256(payload).hexdigest()
    path = _safe(store, f"{kind}-{sha[:16]}.json")
    if path.exists():
        if storage.sha256(path) != sha:
            raise ContextError("CONTEXT_INVALID", "Immutable context record was modified")
    else:
        storage.atomic_write_bytes(path, payload)
    return {"schema_version": 1, "path": path.name, "sha256": sha}


def _record(store: Path, reference: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(reference, dict) or type(reference.get("schema_version")) is not int or reference["schema_version"] != 1:
        raise ContextError("CONTEXT_INVALID", "Unsupported context record version")
    path = _safe(store, reference.get("path", ""))
    if not path.is_file() or storage.sha256(path) != reference.get("sha256"):
        raise ContextError("CONTEXT_INVALID", "Context record digest mismatch")
    try:
        record = storage.read_json(path)
    except WorkflowError as exc:
        raise ContextError("CONTEXT_INVALID", "Context payload is not valid JSON") from exc
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise ContextError("CONTEXT_INVALID", "Unsupported context payload version")
    return record


def _text(path: Path, maximum: int) -> tuple[str | None, str | None, str | None]:
    if not path.is_file():
        return None, None, "missing"
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None, storage.sha256(path), "source format needs accepted text extraction"
    if path.stat().st_size > maximum:
        return None, storage.sha256(path), "source exceeds configured byte limit"
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        # No newline normalization: offsets are tied to exact decoded UTF-8 bytes.
        return raw.decode("utf-8-sig"), sha, None
    except UnicodeError:
        return None, sha, "source is not UTF-8 text"


def _entry(root: Path, spec: dict[str, Any], scope_id: str, limits: dict[str, int]) -> dict[str, Any]:
    path = _safe(root, spec["path"])
    text, sha, unavailable = _text(path, limits["max_source_bytes"])
    expected = spec.get("accepted_sha256")
    if expected and sha != expected:
        raise ContextError("CONTEXT_CURSOR_STALE", "Accepted source changed; repeat intake before building context")
    original = None
    if spec.get("original_path"):
        original_path = _safe(root, spec["original_path"])
        if not original_path.is_file() or storage.sha256(original_path) != spec.get("original_sha256"):
            raise ContextError("CONTEXT_CURSOR_STALE", "Accepted original changed; repeat intake")
        original = {"path": spec["original_path"], "sha256": spec["original_sha256"]}
    identity = digest({"scope_id": scope_id, "path": spec["path"], "sha256": sha, "original": original})
    readable = text is not None and bool(text.strip())
    return {"entry_id": "src-" + identity[:24], "identity": identity,
            "path": spec["path"], "sha256": sha, "original": original,
            "reason": spec["reason"], "required": spec["required"],
            "readable": readable, "unavailable_reason": unavailable or (None if readable else "empty source"),
            "length": len(text) if text is not None else 0,
            "parts": parts(len(text), limits["page_chars"], spec["required"]) if readable else []}


def _load(gate: dict[str, Any], product_root: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    root, store, current_manifest = _roots(gate, product_root)
    _, evidence = state.evidence_for_gate(gate, product_root=product_root)
    ref = evidence.get("context_manifest")
    if not isinstance(ref, dict):
        raise ContextError("CONTEXT_SCOPE_REQUIRED", "Build compact context before reading manifest entries")
    manifest = _record(store, ref)
    coverage = _record(store, ref.get("coverage", {}))
    schema_errors = state.validate_json_record(manifest, product_root / "schemas/context-manifest.schema.json")
    if schema_errors:
        raise ContextError("CONTEXT_INVALID", "Invalid context manifest contract")
    if (manifest.get("gate_id") != gate["gate_id"] or coverage.get("gate_id") != gate["gate_id"]
            or manifest.get("scope_id") != digest(scope_record(gate, current_manifest))
            or manifest.get("selection_id") != digest(_specs(root, gate, current_manifest, evidence, product_root=product_root))
            or coverage.get("manifest_sha256") != ref["sha256"]):
        raise ContextError("CONTEXT_CURSOR_STALE", "Context scope changed or belongs to a different gate")
    validate_records(manifest, coverage)
    if _record(store, manifest["scope"]) != {"schema_version": 1, **scope_record(gate, current_manifest)}:
        raise ContextError("CONTEXT_INVALID", "Context scope snapshot differs from saved decisions")
    return root, store, manifest, coverage, evidence


def _check_version(root: Path, entry: dict[str, Any], limits: dict[str, int]) -> str | None:
    path = _safe(root, entry["path"])
    if entry.get("original"):
        original = entry["original"]
        original_path = _safe(root, original["path"])
        if not original_path.is_file() or storage.sha256(original_path) != original["sha256"]:
            raise ContextError("CONTEXT_CURSOR_STALE", "Accepted original changed since context build")
    text, sha, _ = _text(path, limits["max_source_bytes"])
    if sha != entry["sha256"] or (text is not None and len(text) != entry["length"]):
        raise ContextError("CONTEXT_CURSOR_STALE", "Context source changed; rebuild context on this gate")
    return text


def build(gate: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    limits = _limits(product_root)
    root, store, work_manifest = _roots(gate, product_root)
    evidence_path, evidence = state.evidence_for_gate(gate, product_root=product_root)
    scope = scope_record(gate, work_manifest)
    scope_id = digest(scope)
    previous = evidence.get("context_manifest")
    if "context_manifest" in evidence:
        old_manifest = _record(store, previous)
        old_coverage = _record(store, previous.get("coverage", {}))
        validate_records(old_manifest, old_coverage)
        if (old_manifest.get("gate_id") != gate["gate_id"] or old_coverage.get("gate_id") != gate["gate_id"]
                or old_coverage.get("manifest_sha256") != previous["sha256"]):
            raise ContextError("CONTEXT_INVALID", "Saved context belongs to another gate or manifest")
    specs = _specs(root, gate, work_manifest, evidence, product_root=product_root)
    if len(specs) > limits["max_entries"]:
        raise ContextError("CONTEXT_SCOPE_REQUIRED", "Too many context entries; select a smaller accepted scope")
    entries = [_entry(root, spec, scope_id, limits) for spec in specs]
    scope_ref = _immutable(store, "scope", {"schema_version": 1, **scope})
    index_text = "".join(json.dumps({"entry_id": entry["entry_id"], "path": entry["path"],
                                    "sha256": entry["sha256"], "reason": entry["reason"],
                                    "readable": entry["readable"], "unavailable_reason": entry["unavailable_reason"],
                                    **part}, ensure_ascii=False) + "\n"
                         for entry in entries for part in (entry["parts"] or [{"section_id": "unavailable", "required": entry["required"]}]))
    index_ref = _immutable(store, "index", {"schema_version": 1, "text": index_text})
    manifest = {"schema_version": 1, "gate_id": gate["gate_id"], "scope_id": scope_id,
                "selection_id": digest(specs), "operation": gate["operation"], "mode": gate.get("mode", "formal"),
                "role": gate.get("context_role"), "source_scope": scope["source_scope"],
                "limits": limits, "entries": entries, "scope": scope_ref, "index": index_ref}
    ref = _immutable(store, "manifest", manifest)
    coverage = {"schema_version": 1, "gate_id": gate["gate_id"], "manifest_sha256": ref["sha256"], "ranges": {}, "cursors": {}}
    # Resume identical source/scope versions; changed identities intentionally reset coverage.
    if previous and previous["sha256"] == ref["sha256"]:
        coverage = old_coverage
    validate_records(manifest, coverage)
    content = compact_text(manifest, scope, coverage, limits["compact_chars"])
    pack = _safe(store, "compact-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16] + ".md")
    if not pack.exists():
        storage.atomic_write_bytes(pack, content.encode("utf-8"))
    elif storage.sha256(pack) != hashlib.sha256(content.encode("utf-8")).hexdigest():
        raise ContextError("CONTEXT_INVALID", "Compact pack was modified")
    ref["coverage"] = _immutable(store, "coverage", coverage)
    sha = storage.sha256(pack)
    evidence.update(context_manifest=ref, context_path=str(pack), context_sha256=sha,
                    context_evidence_id="CTX-" + sha[:12])
    # One atomic authoritative commit; an interrupted write leaves previous coverage usable.
    storage.write_json(evidence_path, evidence)
    return {"schema_version": 1, "state": "READY", "view": "compact", "path": str(pack),
            "sha256": sha, "evidence_id": evidence["context_evidence_id"], "content": content,
            "manifest_id": ref["sha256"], "coverage": coverage_summary(manifest, coverage)}


def read(gate: dict[str, Any], args: argparse.Namespace, *, product_root: Path) -> dict[str, Any]:
    root, store, manifest, coverage, evidence = _load(gate, product_root)
    limits = _limits(product_root)
    limit = min(limits["page_chars"], manifest["limits"]["page_chars"])
    requested = getattr(args, "max_chars", None)
    if requested is not None:
        if type(requested) is not int or requested < 1:
            raise ContextError("CONTEXT_INVALID", "max_chars must be a positive integer")
        limit = min(limit, requested)
    entry_id = getattr(args, "entry_id", "")
    section_id = getattr(args, "section_id", "") or "whole"
    if not isinstance(entry_id, str) or not isinstance(section_id, str):
        raise ContextError("CONTEXT_INVALID", "Entry and section IDs must be strings")
    entry = next((entry for entry in manifest["entries"] if entry["entry_id"] == entry_id), None)
    if entry_id in {"scope", "index"}:
        record = _record(store, manifest[entry_id])
        text = record.get("text") if entry_id == "index" else json.dumps(record, ensure_ascii=False, indent=2)
        if not isinstance(text, str):
            raise ContextError("CONTEXT_INVALID", "Context index must contain text")
        sha = manifest[entry_id]["sha256"]
    elif entry is not None:
        text = _check_version(root, entry, limits)
        if not entry["readable"] or text is None:
            raise ContextError("CONTEXT_SCOPE_REQUIRED", "Source requires extraction, repair or a smaller accepted scope")
        sha = entry["sha256"]
    else:
        raise ContextError("CONTEXT_INVALID", "Unknown manifest entry ID")
    start, end = 0, len(text)
    if section_id != "whole":
        part = next((p for p in entry["parts"] if p["section_id"] == section_id), None) if entry else None
        if not part:
            raise ContextError("CONTEXT_INVALID", "Unknown manifest section ID")
        start, end = part["start"], part["end"]
    cursor = getattr(args, "cursor", "") or ""
    if not isinstance(cursor, str):
        raise ContextError("CONTEXT_INVALID", "Cursor must be a string")
    if cursor:
        saved = coverage["cursors"].get(cursor)
        if not saved or any(saved.get(k) != v for k, v in {"entry_id": entry_id, "section_id": section_id, "sha256": sha}.items()):
            raise ContextError("CONTEXT_CURSOR_STALE", "Cursor does not belong to this source, section and scope")
        start = saved["offset"]
        if not 0 <= start <= end:
            raise ContextError("CONTEXT_INVALID", "Cursor offset is outside its selected part")
    stop = min(start + limit, end)
    result = {"schema_version": 1, "state": "READ", "entry_id": entry_id, "section_id": section_id,
              "source_sha256": sha, "manifest_id": evidence["context_manifest"]["sha256"],
              "start": start, "end": stop, "content": text[start:stop], "next_cursor": None}
    # Bound the serialized response as well as the source page (escaped control characters).
    while True:
        next_record = {"entry_id": entry_id, "section_id": section_id, "sha256": sha, "offset": stop}
        next_cursor = digest({"manifest": coverage["manifest_sha256"], **next_record}) if stop < end else None
        updated = merge_ranges(coverage["ranges"].get(entry_id, []), start, stop) if entry else []
        proposed = {**coverage, "ranges": {**coverage["ranges"], **({entry_id: updated} if entry else {})}}
        result.update(end=stop, content=text[start:stop], next_cursor=next_cursor, coverage=coverage_summary(manifest, proposed))
        if len(json.dumps(result, ensure_ascii=False, indent=2)) <= limits["response_chars"]:
            break
        if stop <= start:
            raise ContextError("CONTEXT_SCOPE_REQUIRED", "Response metadata exceeds configured envelope")
        stop = start + (stop - start) // 2
    if next_cursor:
        proposed["cursors"] = {**coverage["cursors"], next_cursor: next_record}
    evidence["context_manifest"]["coverage"] = _immutable(store, "coverage", proposed)
    evidence_path, _ = state.evidence_for_gate(gate, product_root=product_root)
    storage.write_json(evidence_path, evidence)
    return result


def command(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    # Share the existing OS writer lock with complete/recovery; recheck gates inside it.
    try:
        with handoff.writer(args.gate_id, product_root=product_root):
            gate = state.load_gate(args.gate_id, states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"}, product_root=product_root)
            if gate.get("completed_at"):
                raise ContextError("CONTEXT_INVALID", "Completed context is historical data; use handoff without changing coverage")
            state.require_gate_tool(gate, "flow1c_context", product_root=product_root)
            action = getattr(args, "action", "build")
            if action == "build":
                value = build(gate, product_root=product_root)
            elif action == "read":
                value = read(gate, args, product_root=product_root)
            else:
                raise ContextError("CONTEXT_INVALID", "Context action must be build or read")
    except OSError as exc:
        raise ContextError("CONTEXT_RECOVERY_REQUIRED", "Context I/O interrupted; retry on the same gate with the saved cursor") from exc
    return OperationResult(value)


def completion_errors(gate: dict[str, Any], *, product_root: Path) -> list[str]:
    """Only additive compact contracts enforce coverage; legacy full view remains valid."""
    _, evidence = state.evidence_for_gate(gate, product_root=product_root)
    if "context_manifest" not in evidence:
        return []
    try:
        root, _, manifest, coverage, _ = _load(gate, product_root)
        limits = _limits(product_root)
        for entry in manifest["entries"]:
            _check_version(root, entry, limits)
        unread = coverage_summary(manifest, coverage)
        if not unread["complete"]:
            return [f"CONTEXT_SCOPE_REQUIRED: {unread['unread_required_count']} mandatory context parts remain unverified"]
    except ContextError as exc:
        return [f"{exc.code}: {exc}"]
    return []
