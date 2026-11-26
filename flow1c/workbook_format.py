"""Format a project workbook with a native Excel copy and record its provenance."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from flow1c import context, documentation, handoff, storage, workbook_safety
from flow1c.handoff_policy import HandoffError, digest
from flow1c.results import OperationResult
from flow1c.workbook_format_policy import LIMITS, MAX_FILE_BYTES, WorkbookFormatError, format_map
from flow1c.workflow import state

OPERATION = "workbook-format"
TOOL = "flow1c_workbook"


def _project_path(value: Any, root: Path) -> Path:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise WorkbookFormatError("WORKBOOK_INPUT", "Provide a workbook path in the project documentation.")
    raw = Path(value)
    if ":" in str(raw.relative_to(raw.anchor)) or ".." in raw.parts:
        raise WorkbookFormatError("WORKBOOK_PATH", "Workbook paths cannot use parent traversal or alternate streams.")
    path = documentation.regular_path(raw if raw.is_absolute() else root / raw)
    if not storage.path_is_within(path, root):
        raise WorkbookFormatError("WORKBOOK_PATH", "Workbook must be inside the project documentation.")
    if any(part.casefold() in {".git", ".flow1c", ".workspace"} for part in path.relative_to(root).parts):
        raise WorkbookFormatError("WORKBOOK_PATH", "Workbook must be in the project documents, outside internal storage.")
    return path


def _input_book(value: Any, root: Path) -> Path:
    path = _project_path(value, root)
    if path.suffix.lower() not in LIMITS or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise WorkbookFormatError("WORKBOOK_INPUT", "Use an existing .xls or .xlsx workbook of at most 24 MiB.")
    return path


def _stage(source: Path, folder: Path, name: str) -> tuple[Path, str]:
    candidate = folder / (name + source.suffix.lower())
    expected = storage.sha256(source)
    shutil.copyfile(source, candidate)
    if storage.sha256(candidate) != expected:
        raise WorkbookFormatError("WORKBOOK_CHANGED", "A workbook changed during copy. Inspect the current version and retry.")
    workbook_safety.check_workbook(candidate)
    return candidate, expected


def _excel(action: str, payload: dict[str, Any], staging: Path, product_root: Path) -> dict[str, Any]:
    if os.name != "nt":
        raise WorkbookFormatError("WORKBOOK_EXCEL_REQUIRED", "Formatting requires desktop Microsoft Excel on Windows.")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        raise WorkbookFormatError("WORKBOOK_EXCEL_REQUIRED", "Windows PowerShell is unavailable.")
    request = staging / "excel-request.json"
    storage.write_json(request, {"action": action, **payload})
    try:
        process = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
             "-File", str(product_root / "scripts" / "format-workbook.ps1"), "-RequestPath", str(request)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        returned = json.loads(process.stdout)
        if not isinstance(returned, dict):
            raise ValueError("Expected a JSON object.")
    except subprocess.TimeoutExpired as exc:
        raise WorkbookFormatError("WORKBOOK_EXCEL_TIMEOUT", "Excel did not finish within 120 seconds.") from exc
    except (ValueError, OSError) as exc:
        raise WorkbookFormatError("WORKBOOK_EXCEL_FAILED", "Excel did not return a workbook result.") from exc
    if process.returncode or returned.get("state") == "BLOCKED":
        raise WorkbookFormatError(str(returned.get("code") or "WORKBOOK_EXCEL_FAILED"),
                                  str(returned.get("message") or "Excel could not format the workbook."))
    return returned


def _verified_output(path: Path, record: dict[str, Any]) -> None:
    if not path.is_file() or storage.sha256(path) != record.get("sha256"):
        raise WorkbookFormatError("WORKBOOK_OUTPUT_CHANGED", "The recorded workbook is missing or has changed.")


def _publish(candidate: Path, output: Path) -> None:
    # Windows rename fails on an existing target. The link fallback retains that
    # property when the filesystem path is exercised by tests on other systems.
    if os.name == "nt":
        candidate.rename(output)
    else:
        os.link(candidate, output)


def command(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    try:
        with handoff.writer(args.gate_id, product_root=product_root):
            gate = state.load_gate(args.gate_id, states={"READY", "UNVERIFIED_DRAFT"}, product_root=product_root)
            state.require_gate_tool(gate, TOOL, product_root=product_root)
            if gate["operation"] != OPERATION or gate["mode"] != "draft" or gate.get("completed_at"):
                raise WorkbookFormatError("WORKBOOK_GATE", "Use an active workbook-format draft request.")
            return OperationResult(_run(args.action, args.request, gate, product_root))
    except HandoffError as exc:
        raise WorkbookFormatError("WORKBOOK_GATE", str(exc)) from exc
    except OSError as exc:
        raise WorkbookFormatError("WORKBOOK_IO", str(exc)) from exc


def _run(action: str, request: Any, gate: dict[str, Any], product_root: Path) -> dict[str, Any]:
    fields = {"list": ({"folder"},), "inspect": ({"source"},),
              "write": ({"source", "reference", "mappings"}, {"source", "profile"})}
    if not isinstance(action, str) or action not in fields or not isinstance(request, dict) or set(request) not in fields[action]:
        raise WorkbookFormatError("WORKBOOK_REQUEST", "Use list with folder, inspect with source, or write with a source and style map.")
    local = storage.read_json(product_root / context.LOCAL_CONFIG_FILE, {})
    configured = local.get("documentation_path")
    if not configured:
        raise WorkbookFormatError("WORKBOOK_PROJECT_REQUIRED", "Configure the project documentation path first.")
    project = documentation.regular_path(Path(configured).expanduser())
    if action == "list":
        directory = _project_path(request["folder"], project)
        if not directory.is_dir():
            raise WorkbookFormatError("WORKBOOK_INPUT", "Select a project directory containing workbooks.")
        books = sorted((path for path in directory.iterdir()
                        if path.suffix.lower() in LIMITS and not path.name.startswith("~$")
                        and path.is_file() and not storage.is_reparse_or_symlink(path)),
                       key=lambda path: path.name.casefold())
        return {"state": "LISTED", "files": [str(path) for path in books[:100]], "truncated": len(books) > 100}
    source = _input_book(request["source"], project)
    root = state.request_root(gate, product_root=product_root)
    folder = documentation.regular_path(root / "workbooks")
    folder.mkdir(exist_ok=True)
    if action == "inspect":
        with tempfile.TemporaryDirectory(prefix=".excel-", dir=folder) as temp:
            staging = Path(temp)
            snapshot, sha = _stage(source, staging, "source")
            result = _excel("inspect", {"source": str(snapshot)}, staging, product_root)
            return {**result, "source": str(source), "sha256": sha}
    style = request
    if "profile" in request:
        profile = _project_path(request["profile"], project)
        if profile.suffix.lower() != ".json" or not profile.is_file() or profile.stat().st_size > 32768:
            raise WorkbookFormatError("WORKBOOK_PROFILE", "Select a JSON style profile of at most 32 KiB.")
        style = storage.read_json(profile)
        if (not isinstance(style, dict) or set(style) != {"schema_version", "reference", "mappings"}
                or type(style["schema_version"]) is not int or style["schema_version"] != 1):
            raise WorkbookFormatError("WORKBOOK_PROFILE", "Style profile version 1 needs reference and mappings.")
    reference = _input_book(style["reference"], project)
    if source == reference:
        raise WorkbookFormatError("WORKBOOK_INPUT", "Select a separate workbook as the style reference.")
    mappings = format_map(style["mappings"], source.suffix.lower(), reference.suffix.lower())
    return _write(source, reference, mappings, gate, folder, product_root)


def _write(source: Path, reference: Path, mappings: list[dict[str, Any]], gate: dict[str, Any],
           folder: Path, product_root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=".excel-", dir=folder) as temp:
        staging = Path(temp)
        snapshot, source_sha = _stage(source, staging, "source")
        sample, reference_sha = _stage(reference, staging, "reference")
        plan = {"source": str(source), "source_sha256": source_sha, "reference": str(reference),
                "reference_sha256": reference_sha, "mappings": mappings}
        fingerprint = digest(plan)
        name = f"{source.stem[:60]}-formatted-{fingerprint[:12]}{source.suffix.lower()}"
        output = documentation.regular_path(folder / name)
        relative = f"workbooks/{name}"
        evidence_path, evidence = state.evidence_for_gate(gate, product_root=product_root)
        records = evidence["changed_files"]
        existing = next((record for record in records if record.get("path") == relative), None)
        if output.exists():
            if not existing or existing.get("format_plan") != fingerprint:
                raise WorkbookFormatError("WORKBOOK_OUTPUT_EXISTS", "An unrecognized file occupies the output path.")
            _verified_output(output, existing)
            if storage.sha256(source) != source_sha or storage.sha256(reference) != reference_sha:
                raise WorkbookFormatError("WORKBOOK_CHANGED", "A workbook changed during formatting. Inspect and retry.")
            return {"state": "WRITTEN", "reused": True, "output": str(output), "record": existing}
        candidate = staging / ("formatted" + source.suffix.lower())
        verification = _excel("write", {"source": str(snapshot), "reference": str(sample),
                                        "output": str(candidate), "mappings": mappings}, staging, product_root)
        if verification.get("state") != "VERIFIED" or not candidate.is_file():
            raise WorkbookFormatError("WORKBOOK_VERIFICATION_FAILED", "Excel did not verify the saved copy.")
        workbook_safety.check_workbook(candidate)
        if storage.sha256(source) != source_sha or storage.sha256(reference) != reference_sha:
            raise WorkbookFormatError("WORKBOOK_CHANGED", "A workbook changed during formatting. Inspect and retry.")
        record = {"target": "draft", "kind": OPERATION, "path": relative, "sha256": storage.sha256(candidate),
                  "format_plan": fingerprint, "source": plan["source"], "source_sha256": source_sha,
                  "reference": plan["reference"], "reference_sha256": reference_sha, "mappings": mappings,
                  "verification": verification, "document_status": "UNVERIFIED_DRAFT"}
        # Record the verified hash first. If publishing is interrupted, a retry can
        # reproduce a missing output, while completion rejects missing/mutated files.
        if existing:
            records.remove(existing)
        records.append(record)
        storage.write_json(documentation.regular_path(evidence_path), evidence)
        _publish(candidate, output)
        return {"state": "WRITTEN", "reused": False, "output": str(output), "record": record}


def complete_outputs(gate: dict[str, Any], *, product_root: Path) -> list[Path]:
    _, evidence = state.evidence_for_gate(gate, product_root=product_root)
    root = state.request_root(gate, product_root=product_root)
    outputs = []
    for record in evidence["changed_files"]:
        if record.get("kind") == OPERATION:
            output = documentation.regular_path(root / record["path"])
            if not storage.path_is_within(output, root / "workbooks"):
                raise WorkbookFormatError("WORKBOOK_PATH", "Recorded output is outside the workbook result directory.")
            _verified_output(output, record)
            outputs.append(output)
    if not outputs:
        raise WorkbookFormatError("WORKBOOK_OUTPUT_REQUIRED", "Format and verify at least one workbook before completing.")
    return outputs
