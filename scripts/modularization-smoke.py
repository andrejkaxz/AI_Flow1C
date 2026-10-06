#!/usr/bin/env python3
"""Exercise a real baseline-to-package Git update and saved request in an external clone."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, *, request: dict | None = None) -> dict:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=json.dumps(request) if request else None,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(
            f"{command[0]} failed ({result.returncode}): {result.stdout}\n{result.stderr}"
        )
    return (
        json.loads(result.stdout.lstrip("\ufeff"))
        if result.stdout.strip().startswith(("{", "\ufeff{"))
        else {}
    )


def git(arguments: list[str], cwd: Path) -> None:
    run(["git", *arguments], cwd)


def stub_external_rlm(seed: Path) -> None:
    # Only the external RLM service/index boundary is synthetic. CLI/doctor/updater are real.
    (seed / "scripts/start-rlm-tools-bsl.ps1").write_text(
        "Write-Output 'synthetic RLM boundary'\nexit 0\n", encoding="utf-8"
    )
    (seed / "scripts/rlm-index.ps1").write_text(
        "param($Action,$SourcePath,[switch]$ForceUpdate,$RequestId,[switch]$RetryFailed,[switch]$Json,$WaitSeconds)\n"
        "@{state='FRESH';detail='synthetic RLM boundary';source_path=$SourcePath}|ConvertTo-Json\nexit 0\n",
        encoding="utf-8",
    )


def smoke(base: Path, baseline_ref: str, powershell: str) -> dict:
    archive = base / "baseline.zip"
    git(["archive", "--format=zip", "-o", str(archive), baseline_ref], ROOT)
    seed = base / "seed"
    seed.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(seed)
    stub_external_rlm(seed)
    git(["init", "-b", "main"], seed)
    git(["config", "user.name", "Flow1C smoke"], seed)
    git(["config", "user.email", "smoke@example.invalid"], seed)
    git(["add", "."], seed)
    git(["commit", "-m", "baseline fixture"], seed)
    checkout = base / "user checkout"
    git(["clone", str(seed), str(checkout)], base)
    bootstrap = run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(checkout / "scripts/bootstrap.ps1"),
            "-Profile",
            "conversation",
            "-Python",
            sys.executable,
            "-NoCacheDir",
            "-Json",
        ],
        checkout,
    )
    python = checkout / ".venv/Scripts/python.exe"
    docs = base / "documentation"
    docs.mkdir()
    git(["init", "-b", "main"], docs)
    configuration, extension = base / "configuration", base / "extension"
    for path in (configuration, extension):
        path.mkdir()
        (path / "Module.bsl").write_text("// synthetic fixture\n", encoding="utf-8")
    local = {
        "schema_version": 1,
        "documentation_path": str(docs),
        "configuration_path": str(configuration),
        "extension_path": str(extension),
        "extension_mode": "local-export",
        "setup": {"validation_version": 1, "workflow_root": str(checkout)},
        "custom_user_setting": {"keep": "unchanged"},
    }
    local_path = checkout / ".flow1c.local.json"
    local_path.write_text(json.dumps(local), encoding="utf-8")
    cli = [str(python), str(checkout / "scripts/flow1c.py")]
    begun = run(
        cli
        + [
            "agent-begin",
            "--operation",
            "functional-spec",
            "--mode",
            "draft",
            "--summary",
            "Saved user request",
        ],
        checkout,
    )
    gate_id = begun["gate_id"]
    user_file = docs / "user-document.md"
    user_file.write_text("User document must survive the update.\n", encoding="utf-8")
    before_user = hashlib.sha256(user_file.read_bytes()).hexdigest()
    gate_path = checkout / ".workspace/agent-gates" / (gate_id + ".json")
    before_gate = gate_path.read_bytes()
    before_config = local_path.read_bytes()

    # Overlay current product files onto the independent seed, without copying development state/history.
    for name in ("scripts", "flow1c", "config", "schemas", "standards", ".agents"):
        source = ROOT / name
        if source.exists():
            shutil.copytree(
                source,
                seed / name,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    stale = seed / "scripts/flow1c_templates_cli.py"
    if stale.exists():
        stale.unlink()
    stub_external_rlm(seed)
    git(["add", "."], seed)
    git(["commit", "-m", "modular runtime fixture"], seed)
    updated = run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(checkout / "scripts/update.ps1"),
            "-SkipExternalToolUpdates",
            "-Json",
        ],
        checkout,
    )
    assert updated["state"] == "READY", updated
    assert (checkout / "flow1c/cli.py").is_file()
    assert gate_path.read_bytes() == before_gate
    assert hashlib.sha256(user_file.read_bytes()).hexdigest() == before_user
    assert (Path(updated["backup_directory"]) / ".flow1c.local.json").read_bytes() == before_config
    migrated = json.loads(local_path.read_text(encoding="utf-8-sig"))
    assert migrated["custom_user_setting"] == local["custom_user_setting"]
    assert migrated["setup"] == local["setup"]
    resumed = run(
        cli + ["agent-dialogue", "--json-stdin"],
        base,
        request={"gate_id": gate_id, "action": "record", "answer": "Resume after update"},
    )
    assert resumed["gate_id"] == gate_id and resumed["notes"][-1]["text"] == "Resume after update"
    run(
        cli + ["agent-write", "--json-stdin"],
        base,
        request={
            "gate_id": gate_id,
            "target": "draft",
            "path": "result.md",
            "content": "# Result\n\nChecked synthetic user content.",
        },
    )
    completed = run(cli + ["agent-complete", "--json-stdin"], base, request={"gate_id": gate_id})
    assert completed["state"] == "DRAFT_COMPLETE", completed
    again = run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(checkout / "scripts/update.ps1"),
            "-SkipExternalToolUpdates",
            "-Json",
        ],
        checkout,
    )
    assert again["state"] == "READY" and again["current_commit"] == updated["current_commit"]
    return {
        "state": "PASSED",
        "baseline_ref": baseline_ref,
        "bootstrap_profile": "conversation",
        "bootstrap_ready": bootstrap["ready"],
        "post_update_doctor": "project-basic READY",
        "real_cli_and_updater": True,
        "external_clone": True,
        "same_gate_resumed": True,
        "user_files_and_setup_preserved": True,
        "backup_verified": True,
        "completion": completed["state"],
        "repeat_update": "READY",
        "limitations": [
            "RLM start/index boundaries are synthetic; external updates were explicitly skipped.",
            "Analysis/full profiles and live clients are separate release acceptance.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--powershell", default="powershell.exe")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This smoke targets the Windows/PowerShell updater.")
    with tempfile.TemporaryDirectory(prefix="flow1c-modularization-") as directory:
        result = smoke(Path(directory), args.baseline_ref, args.powershell)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
