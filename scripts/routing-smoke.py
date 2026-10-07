#!/usr/bin/env python3
"""Compare annotated structured routes and legacy defaults without creating gates."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flow1c.routing import load_rules
from flow1c.routing_policy import check_legacy_route, check_route


def integration_smoke() -> dict:
    """Separate processes and package reinstall with synthetic legacy/new requests."""
    with tempfile.TemporaryDirectory(prefix="flow1c-routing-smoke-") as directory:
        checkout = Path(directory) / "product"
        outside = Path(directory) / "outside"
        outside.mkdir()
        for name in ("flow1c", "scripts", "config", "schemas", "docs", ".agents"):
            shutil.copytree(ROOT / name, checkout / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        def call(command: str, request: dict | None = None, *, expected: int = 0) -> dict:
            result = subprocess.run(
                [sys.executable, "-B", str(checkout / "scripts/flow1c.py"), command,
                 "--json" if command == "route-catalog" else "--json-stdin"],
                cwd=outside, input=json.dumps(request, ensure_ascii=False) if request is not None else None,
                capture_output=True, encoding="utf-8", timeout=30,
            )
            if result.returncode != expected:
                raise AssertionError(f"{command} returned {result.returncode}; expected {expected}: {result.stdout}")
            return json.loads(result.stdout)

        assert len(call("route-catalog")["routes"]) == 19
        proposal = {"schema_version": 1, "operation": "functional-spec", "mode": "draft",
                    "expected_outcome": "Synthetic saved draft", "sources": []}
        assert call("route-check", proposal)["status"] == "VALID"
        ambiguous = {**proposal, "ambiguities": ["source"]}
        assert call("route-check", ambiguous, expected=1)["status"] == "CLARIFICATION_REQUIRED"
        assert call("agent-begin", {"operation": "functional-spec", "mode": "draft",
                                   "summary": proposal["expected_outcome"], "route_proposal": ambiguous},
                    expected=1)["status"] == "CLARIFICATION_REQUIRED"
        assert not (checkout / ".workspace").exists()
        local_path = checkout / ".flow1c.local.json"
        local_path.write_text(json.dumps({"user_settings": {"synthetic_saved": True}}), encoding="utf-8")
        local_before = local_path.read_bytes()
        results = []
        for kind in ("structured", "legacy"):
            payload = {"operation": "functional-spec", "mode": "draft", "summary": proposal["expected_outcome"]}
            if kind == "structured":
                payload["route_proposal"] = proposal
            gate = call("agent-begin", payload)
            gate_id = gate["gate_id"]
            if kind == "legacy":
                for field in ("route_origin", "route_decision", "route_proposal"):
                    gate.pop(field, None)
                (checkout / ".workspace/agent-gates" / f"{gate_id}.json").write_text(json.dumps(gate), encoding="utf-8")
            evidence_path = Path(gate["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence.update(schema_version=1, synthetic_saved="preserved")
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            # Reinstall only product code, retaining all saved request/config data.
            shutil.copytree(ROOT / "flow1c", checkout / "flow1c", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            call("agent-dialogue", {"gate_id": gate_id, "action": "record", "answer": "Synthetic saved answer"})
            call("agent-write", {"gate_id": gate_id, "target": "draft", "path": "result.md",
                                 "content": "Synthetic draft result"})
            completed = call("agent-complete", {"gate_id": gate_id})
            assert completed["state"] == "DRAFT_COMPLETE"
            saved = json.loads((checkout / ".workspace/agent-gates" / f"{gate_id}.json").read_text(encoding="utf-8"))
            assert saved["notes"][0]["text"] == "Synthetic saved answer"
            assert json.loads(evidence_path.read_text(encoding="utf-8"))["synthetic_saved"] == "preserved"
            assert local_path.read_bytes() == local_before
            results.append({"origin": kind, "state": completed["state"], "answers_preserved": True,
                            "evidence_preserved": True, "local_settings_preserved": True})
        assert len(list((checkout / ".workspace/agent-gates").glob("*.json"))) == 2
        return {"state": "PASSED", "gate_count": 2, "saved_requests": results,
                "scope": "standalone CLI, process restart and same-version package reinstall",
                "bootstrap_and_git_update_evaluated": False}


def smoke(*, integration: bool = False) -> dict:
    rules = load_rules(ROOT)
    corpus = json.loads((ROOT / "evals/routing-cases.json").read_text(encoding="utf-8"))
    baseline = json.loads((ROOT / "tests/fixtures/routing-baseline.json").read_text(encoding="utf-8"))
    failures = []
    for case in corpus["cases"]:
        expected = case["expected"]
        proposal = {
            "schema_version": 1, "expected_outcome": case["prompt"],
            "operation": expected["operation"], "mode": expected["mode"],
            "sources": expected["sources"], "source_relation": expected["source_relation"],
            "ambiguities": case.get("ambiguities", []), "substeps": case["allowed_substeps"],
        }
        decision = check_route(proposal, rules)
        fields = ["status"] + (["operation", "mode", "primary_skill", "role"] if expected["operation"] else [])
        if any(decision[field] != expected[field] for field in fields):
            failures.append(case["case_id"])
    for case in baseline["defaults"]:
        decision = check_legacy_route(case["operation"], case["summary"], case["explicit_mode"], rules)
        if any(decision[field] != expected for field, expected in case["expected"].items()):
            failures.append(case["case_id"])
    integrated = None
    if integration:
        try:
            integrated = integration_smoke()
        except (AssertionError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            failures.append("cli-integration")
            integrated = {"state": "FAILED", "error": str(exc)}
    return {
        "schema_version": 1, "state": "FAILED" if failures else "PASSED",
        "scope": "route catalog and pure contract comparison",
        "policy_version": rules["policy_version"], "catalog_digest": rules["digest"],
        "annotation_version": corpus["annotation_version"], "baseline_commit": baseline["baseline_commit"],
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "routes": len(rules["routes"]), "structured_cases": len(corpus["cases"]),
        "legacy_cases": len(baseline["defaults"]), "failures": failures,
        "gate_integration_enabled": True, "cli_integration": integrated,
        "language_accuracy_evaluated": False, "release_ready": False,
        "limitations": [
            "Structured proposals are annotations; this is not a model routing evaluation.",
            "Live client/model acceptance, Git update, handoff and compact context are separate checks.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--integration", action="store_true", help="Also check isolated CLI and saved legacy/new requests")
    args = parser.parse_args()
    result = smoke(integration=args.integration)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["state"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
