#!/usr/bin/env python3
"""Standalone processes, interrupted completion and preserved package reinstall."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run() -> dict:
    with tempfile.TemporaryDirectory(prefix="f1h-") as directory:
        checkout = Path(directory) / "product"
        for name in ("flow1c", "scripts", "schemas", "config", "docs", ".agents"):
            shutil.copytree(ROOT / name, checkout / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        local = checkout / ".flow1c.local.json"
        local.write_text(json.dumps({"synthetic_user_setting": "keep"}), encoding="utf-8")
        local_before = local.read_bytes()

        def call(command: str, payload: dict, *, crash: bool = False) -> tuple[int, dict]:
            if crash:
                program = (
                    "from pathlib import Path; from unittest import mock; from flow1c import cli, storage; "
                    "original=storage.write_json; "
                    "fail=lambda p,v: (_ for _ in ()).throw(OSError('synthetic interruption')) "
                    "if p.parent.name == 'handoffs' else original(p,v); "
                    "patch=mock.patch.object(storage,'write_json',side_effect=fail); patch.start(); "
                    "raise SystemExit(cli.main(['agent-complete','--json-stdin']))"
                )
                arguments = [sys.executable, "-B", "-c", program]
            else:
                arguments = [sys.executable, "-B", str(checkout / "scripts/flow1c.py"), command, "--json-stdin"]
            result = subprocess.run(arguments, cwd=checkout, input=json.dumps(payload),
                                    text=True, encoding="utf-8", capture_output=True, timeout=60)
            if not result.stdout:
                raise RuntimeError(f"{command}: {result.stderr}")
            return result.returncode, json.loads(result.stdout)

        code, gate = call("agent-begin", {"operation": "consultation", "mode": "draft", "summary": "Synthetic smoke result"})
        assert code == 0, gate
        gate_id = gate["gate_id"]
        assert call("agent-dialogue", {"gate_id": gate_id, "action": "record", "answer": "Saved synthetic decision"})[0] == 0
        assert call("agent-write", {"gate_id": gate_id, "target": "draft", "path": "result.md", "content": "Synthetic result"})[0] == 0
        output = Path(gate["evidence_path"]).parent / "result.md"
        output_before = output.read_bytes()
        output_mtime = output.stat().st_mtime_ns
        code, interrupted = call("agent-complete", {"gate_id": gate_id}, crash=True)
        assert code == 2 and interrupted["state"] == "DRAFT_COMPLETE", interrupted
        assert interrupted["handoff_error"]["code"] == "HANDOFF_RECOVERY_REQUIRED", interrupted
        gate_path = checkout / ".workspace/agent-gates" / f"{gate_id}.json"
        before = json.loads(gate_path.read_text(encoding="utf-8"))
        assert before["completion_record"]["result"]["state"] == "DRAFT_COMPLETE"
        # Replace only executable package files, preserving receipts, answers and local settings.
        shutil.copytree(ROOT / "flow1c", checkout / "flow1c", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        code, recovered = call("agent-handoff", {"gate_id": gate_id, "action": "recover"})
        assert code == 0, recovered
        code, repeated = call("agent-complete", {"gate_id": gate_id})
        assert code == 0 and repeated["state"] == "DRAFT_COMPLETE", repeated
        code, transfer = call("agent-handoff", {"gate_id": gate_id})
        assert code == 0 and transfer["handoff"] == recovered["handoff"], transfer
        after = json.loads(gate_path.read_text(encoding="utf-8"))
        assert before["completed_at"] == after["completed_at"]
        assert after["notes"][0]["text"] == "Saved synthetic decision"
        assert output.read_bytes() == output_before and output.stat().st_mtime_ns == output_mtime
        assert local.read_bytes() == local_before
        assert len(list((checkout / ".workspace/agent-gates").glob("*.json"))) == 1
        output.write_text("UNVERIFIED_DRAFT\nSynthetic tampering", encoding="utf-8")
        code, stale = call("agent-handoff", {"gate_id": gate_id})
        assert code == 2 and stale["code"] == "HANDOFF_STALE", stale
        return {"state": "PASSED", "gate_count": 1, "interrupted_completion_preserved": True,
                "resume_after_package_reinstall": True, "answers_and_local_settings_preserved": True,
                "output_bytes_and_mtime_preserved": True, "changed_output_rejected": True,
                "record_id": transfer["handoff"]["record_id"],
                "scope": "synthetic standalone CLI and same-version package reinstall",
                "bootstrap_git_update_or_model_acceptance": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"schema_version": 1, "python": platform.python_version(), "platform": platform.platform(),
              "product_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                               capture_output=True, text=True).stdout.strip(),
              "working_tree_evaluated": True}
    source_files = (
        "flow1c/handoff.py", "flow1c/handoff_policy.py", "flow1c/workflow/complete.py",
        "flow1c/templates.py", "flow1c/cli.py", "schemas/agent-handoff.schema.json",
        "schemas/agent-gate.schema.json", "schemas/evidence.schema.json",
        "scripts/handoff-smoke.py",
    )
    report["product_sources_sha256"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in source_files
    }
    try:
        report["checks"] = run()
        report["state"] = "PASSED"
    except (AssertionError, OSError, RuntimeError, ValueError) as exc:
        report.update(state="FAILED", error=str(exc))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["state"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
