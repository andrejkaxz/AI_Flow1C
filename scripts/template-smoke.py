#!/usr/bin/env python3
"""Isolated Windows bootstrap/update smoke. --bootstrap explicitly allows pip network in temporary venvs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, stdin: dict | None = None) -> dict:
    result = subprocess.run(command, input=json.dumps(stdin) if stdin is not None else None,
                            text=True, encoding="utf-8", capture_output=True, timeout=180, check=False)
    payload = json.loads(result.stdout) if result.stdout.strip().startswith("{") else None
    if result.returncode and not (result.returncode == 1 and payload and payload.get("state") == "WAITING_USER"):
        raise RuntimeError(f"Smoke command failed ({result.returncode}): {command}\n{result.stdout}\n{result.stderr}")
    return payload if payload is not None else {"exit_code": result.returncode}


def smoke(checkout: Path, docs: Path, *, bootstrap: bool, format_name: str) -> dict:
    python = str(checkout / ".venv/Scripts/python.exe") if bootstrap else sys.executable
    if bootstrap:
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(checkout / "scripts/bootstrap.ps1"),
             "-Profile", "template-docx" if format_name == "docx" else "template-markdown", "-NoCacheDir", "-Json"])
    cli = [python, str(checkout / "scripts/flow1c.py")]
    begin = run(cli + ["agent-begin", "--operation", "template-management", "--mode", "explore", "--summary", "Smoke intake"])
    gate_id = begin["gate_id"]
    configured = run(cli + ["template", "configure", "--json-stdin"], stdin={"gate_id": gate_id, "request": {"documentation_path": str(docs)}})
    doctor = run(cli + ["doctor", "--json", "--operation", "template-management", "--format", format_name])
    if not doctor["ready"]:
        raise RuntimeError("Targeted readiness failed")
    if format_name == "markdown":
        no_docx = run([python, "-c", "import importlib.util,json; print(json.dumps({'docx':bool(importlib.util.find_spec('docx'))}))"])
        if bootstrap and no_docx["docx"]:
            raise RuntimeError("Markdown clean bootstrap unexpectedly installed DOCX dependencies")
    source = docs / ("source.md" if format_name == "markdown" else "source.docx")
    if format_name == "markdown":
        source.write_text("# Meeting\n\n{{content}}\n", encoding="utf-8")
    else:
        run([python, "-c", "from docx import Document; import sys; d=Document(); "
             "t=d.add_table(rows=2,cols=2); t.cell(0,0).merge(t.cell(0,1)).text='Meeting'; "
             "t.cell(1,0).text='Decision'; d.save(sys.argv[1])", str(source)])
    received = run(cli + ["template", "intake", "--json-stdin"], stdin={"gate_id": gate_id, "request": {"source": str(source), "document_type": "meeting-minutes"}})
    if received["state"] != "PROFILE_DRAFT":
        raise RuntimeError(f"Intake failed: {received}")
    operation_id = received["operation_id"]
    inspection = run(cli + ["template", "inspect", "--json-stdin"], stdin={"gate_id": gate_id, "request": {"operation_id": operation_id}})
    profile = {"schema_version": 1, "source_sha256": received["source_sha256"], "rules": [], "questions": [], "coverage": []}
    ops = []
    for t in inspection["targets"]:
        variable = t["kind"] == "field" or t.get("blank_cell", False)
        profile["coverage"].append({"target_id": t["id"], "role": "variable" if variable else "constant",
            "purpose": "Content supplied by user" if variable else "Preserve structure", "required": variable,
            "required_basis": "user defined" if variable else None, "unknown": "Ask the user"})
        if variable:
            ops.append({"target_id": t["id"], "kind": "text", "value": "Actual smoke content", "basis": "Synthetic user statement"})
    saved = run(cli + ["template", "profile-save", "--json-stdin"], stdin={"gate_id": gate_id, "request": {"operation_id": operation_id, "profile": profile}})
    active = run(cli + ["template", "activate", "--json-stdin"], stdin={"gate_id": gate_id, "request": {"operation_id": operation_id, "expected_revision": None, "default": True}})
    run(cli + ["agent-complete", "--json-stdin"], stdin={"gate_id": gate_id, "summary": "Stored and checked template"})
    document = run(cli + ["agent-begin", "--operation", "template-document", "--mode", "draft", "--summary", "Generate smoke draft"])
    document_gate = document["gate_id"]
    meeting = docs / "meeting-input.md"
    meeting.write_text("# Synthetic meeting\nUse weekly review.\n", encoding="utf-8")
    run(cli + ["artifact-intake", "--json-stdin"], stdin={"gate_id": document_gate, "source": [str(meeting)]})
    run(cli + ["agent-context", "--json-stdin"], stdin={"gate_id": document_gate, "view": "compact"})
    index = run(cli + ["context-read", "--json-stdin"], stdin={"gate_id": document_gate, "entry_id": "index"})
    entry = json.loads(index["content"].splitlines()[0])["entry_id"]
    read = run(cli + ["context-read", "--json-stdin"], stdin={"gate_id": document_gate, "entry_id": entry})
    pin = run(cli + ["template", "resolve", "--json-stdin"], stdin={"gate_id": document_gate, "request": {"document_type": "meeting-minutes"}})
    plan = run(cli + ["document-plan", "--json-stdin"], stdin={"gate_id": document_gate, "request": {"operations": ops}})
    output = run(cli + ["document-write", "--json-stdin"], stdin={"gate_id": document_gate, "request": {"plan_id": plan["plan_id"]}})
    verification = output.get("result_verification", {})
    if verification.get("status") != "PASSED" or verification.get("output_sha256") != output["output_sha256"]:
        raise RuntimeError("Independent document verification is missing or unbound")
    run(cli + ["memory", "--json-stdin"], stdin={"gate_id": document_gate, "action": "overview",
                                                "request": {"query": "Synthetic template smoke"}})
    memory = run(cli + ["memory", "--json-stdin"], stdin={"gate_id": document_gate, "action": "remember", "request": {
        "kind": "decision", "subject": "Synthetic meeting review cadence", "text": "Use weekly review.",
        "basis": "accepted_document", "source": {"type": "context_entry", "entry_id": entry,
                                                  "locator": "line:2", "sha256": read["source_sha256"]}}})
    if memory["state"] != "SAVED":
        raise RuntimeError(f"Template draft did not save evidence-backed memory: {memory}")
    run(cli + ["memory", "--json-stdin"], stdin={"gate_id": document_gate, "action": "review", "request": {
        "findings": [{"kind": "decision", "topic": "Synthetic meeting review cadence", "disposition": "saved",
                      "records": [{"memory_id": memory["memory_id"], "revision": memory["revision"]}],
                      "reason": "The source line was read from an accepted attachment on the same draft gate."}]}})
    complete = run(cli + ["agent-complete", "--json-stdin"], stdin={"gate_id": document_gate, "summary": "Checked draft"})
    if complete.get("memory", {}).get("status") != "UPDATED":
        raise RuntimeError(f"Template draft did not complete with the memory receipt: {complete}")
    source.unlink()
    local_path = checkout / ".flow1c.local.json"
    local = json.loads(local_path.read_text(encoding="utf-8"))
    legacy_path = docs / "legacy.docx"
    local.update(schema_version=1, functional_spec_template=str(legacy_path), custom_preserved={"value": "retain"})
    local_path.write_text(json.dumps(local), encoding="utf-8")
    before_index = (docs / ".flow1c/document-templates/library.json").read_bytes()
    migrated = run([python, str(checkout / "scripts/migrate-local-config.py"), "--path", str(local_path), "--json"])
    updated = json.loads(local_path.read_text(encoding="utf-8"))
    if updated["custom_preserved"] != {"value": "retain"} or updated["functional_spec_template"] != str(legacy_path):
        raise RuntimeError("Migration lost local values")
    if before_index != (docs / ".flow1c/document-templates/library.json").read_bytes():
        raise RuntimeError("Migration changed the library")
    return {"format": format_name, "bootstrap": bootstrap, "doctor": doctor, "active": active["state"],
            "document": complete["state"], "layout_state": output["layout_state"], "migration": migrated["state"],
            "source_sha256": received["source_sha256"], "output_sha256": output["output_sha256"],
            "result_verification": verification, "library_preserved": True, "memory": complete["memory"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    evidence = {"schema_version": 1, "environment": {"platform": platform.platform(), "python": sys.version},
                "release_ready": False, "results": []}
    with tempfile.TemporaryDirectory(prefix="flow1c-template-smoke-") as directory:
        base = Path(directory)
        for format_name in ("markdown", "docx"):
            checkout = base / ("workflow-" + format_name)
            shutil.copytree(ROOT, checkout, ignore=shutil.ignore_patterns(".git", ".venv", ".workspace", ".tools", "__pycache__", ".flow1c.local.json", ".test-*", ".pytest_cache", "node_modules"))
            docs = base / ("docs-" + format_name)
            docs.mkdir()
            evidence["results"].append(smoke(checkout, docs, bootstrap=args.bootstrap, format_name=format_name))
    evidence["limitations"] = ["Real OpenCode/Codex/Claude model dialogue evals and qualified visual renderer are separate release checks."]
    Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
