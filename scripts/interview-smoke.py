#!/usr/bin/env python3
"""Clean-copy interview smoke. --bootstrap explicitly opts into pip in an isolated venv."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def call(python: Path, checkout: Path, command: str, data: dict | None = None, extra: list[str] | None = None) -> dict:
    args = [str(python), str(checkout / "scripts/flow1c.py"), command, *(extra or [])]
    if data is not None:
        args.append("--json-stdin")
    result = subprocess.run(args, input=json.dumps(data, ensure_ascii=False) if data is not None else None,
                            text=True, encoding="utf-8", capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f"Smoke failed: {command}\n{result.stdout}\n{result.stderr}")
    return json.loads(result.stdout)


def smoke(parent: Path, bootstrap: bool) -> dict:
    checkout = parent / "checkout"
    checkout.mkdir()
    for name in ("scripts", "config", "schemas", "standards"):
        shutil.copytree(ROOT / name, checkout / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    skills = checkout / ".agents/skills"
    skills.mkdir(parents=True)
    for source in (ROOT / ".agents/skills").glob("flow1c-*"):
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, skills / source.name)
    python = Path(sys.executable)
    if bootstrap:
        subprocess.run([str(python), "-m", "venv", str(checkout / ".venv")], check=True, timeout=90)
        python = checkout / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements-capabilities/interview.txt")], check=True, timeout=180)
    ready = call(python, checkout, "doctor", extra=["--json", "--operation", "interview-preparation"])
    assert ready["ready"]
    assert not (checkout / ".flow1c.local.json").exists()
    first_gate = call(python, checkout, "agent-begin", {"operation": "interview-preparation", "summary": "Подготовь интервью по закупкам"})
    question = {"l1_code": "01", "l1_name": "Закупки", "l2_code": "01.01", "l2_name": "Управление поставщиками",
                "l3_code": "01.01.01", "l3_name": "Учёт поставщиков", "question_id": "Q-0001", "question": "Как проверяются дубли?"}
    written = call(python, checkout, "interview-register", {"gate_id": first_gate["gate_id"], "action": "write", "request": {"rows": [question]}})
    first_path = Path(written["absolute_path"])
    initial = first_path.read_bytes()
    first_done = call(python, checkout, "agent-complete", {"gate_id": first_gate["gate_id"]})
    assert first_done["state"] == "DRAFT_COMPLETE"
    # Update compatibility: load a saved gate lacking the newly published tool map.
    second = call(python, checkout, "agent-begin", {"operation": "interview-preparation", "summary": "Выдели требования из ответов"})
    gate_path = checkout / ".workspace/agent-gates" / (second["gate_id"] + ".json")
    persisted = json.loads(gate_path.read_text(encoding="utf-8"))
    persisted["available_actions"] = [item for item in persisted["available_actions"] if item != "flow1c_interview"]
    gate_path.write_text(json.dumps(persisted, ensure_ascii=False), encoding="utf-8")
    intake = call(python, checkout, "artifact-intake", {"gate_id": second["gate_id"], "source": [str(first_path)]})
    source = intake["copied"][0]["path"]
    revised = call(python, checkout, "interview-register", {"gate_id": second["gate_id"], "action": "write", "request": {
        "source": source, "patches": [{"question_id": "Q-0001", "fields": {"answer": "Проверять совпадение ИНН"}}],
        "requirements": [{"draft_id": "DRAFT-1", "question_id": "Q-0001", "text": "Проверять совпадение ИНН",
                          "acceptance_criterion": "Совпадение обнаруживается, дальнейшее действие уточнить"}]}})
    audit = call(python, checkout, "interview-register", {"gate_id": second["gate_id"], "action": "audit", "request": {"source": revised["record"]["path"]}})
    done = call(python, checkout, "agent-complete", {"gate_id": second["gate_id"]})
    assert audit["audit"]["ready"] and done["state"] == "DRAFT_COMPLETE"
    assert first_path.read_bytes() == initial
    updated = json.loads(gate_path.read_text(encoding="utf-8"))
    assert "flow1c_interview" in updated["available_actions"]
    return {"schema_version": 1, "state": "PASSED", "isolated_bootstrap": bootstrap,
            "clean_copy_without_config_or_project": True, "doctor_ready": True,
            "saved_gate_actions_updated": True, "source_preserved": True,
            "answer_requirement_traceability": True, "completion": done["state"],
            "document_status": done["document_status"],
            "limitations": ["Visual layout and a live agent conversation were not evaluated.",
                            "This does not exercise the remote Git updater."]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="flow1c-interview-smoke-") as directory:
        result = smoke(Path(directory), args.bootstrap)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
