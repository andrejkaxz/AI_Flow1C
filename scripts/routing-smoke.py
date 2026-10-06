#!/usr/bin/env python3
"""Compare annotated structured routes and legacy defaults without creating gates."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flow1c.routing import load_rules
from flow1c.routing_policy import check_legacy_route, check_route


def smoke() -> dict:
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
    return {
        "schema_version": 1, "state": "FAILED" if failures else "PASSED",
        "scope": "route catalog and pure contract comparison",
        "policy_version": rules["policy_version"], "catalog_digest": rules["digest"],
        "annotation_version": corpus["annotation_version"], "baseline_commit": baseline["baseline_commit"],
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "routes": len(rules["routes"]), "structured_cases": len(corpus["cases"]),
        "legacy_cases": len(baseline["defaults"]), "failures": failures,
        "gate_integration_enabled": False, "language_accuracy_evaluated": False, "release_ready": False,
        "limitations": [
            "Structured proposals are annotations; this is not a model routing evaluation.",
            "CLI/adapter integration, projections, guard, handoff and compact context are subsequent stages.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = smoke()
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["state"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
