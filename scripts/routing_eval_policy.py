"""Independent scoring of model route observations; no product policy or I/O."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from typing import Any

EVALUATOR_VERSION = 1
THRESHOLDS = {"exact": 0.95, "operation": 0.95, "mode": 0.95,
              "primary_skill": 0.95, "source": 0.95, "ambiguity": 1.0}
SELECTION_TOOLS = {"flow1c_route_catalog", "flow1c_route_check", "question", "skill"}


def corpus_errors(cases: list[dict[str, Any]]) -> list[str]:
    """A model cannot identify a hidden attachment/ref selector from a label."""
    errors = []
    if len({case["case_id"] for case in cases}) != len(cases):
        errors.append("duplicate case IDs")
    for case in cases:
        inputs = "\n".join([case["prompt"], *case.get("context", []), *case.get("answers", [])])
        for source in case["expected"]["sources"]:
            if source.get("selector") and source["selector"] not in inputs:
                errors.append(f"{case['case_id']}: source selector is absent from user inputs")
    return errors


def source_identity(sources: Any) -> list[str] | None:
    """Compare annotations, including selectors; resolver metadata is not a label."""
    if not isinstance(sources, list) or any(not isinstance(s, dict) for s in sources):
        return None
    fields = ("kind", "version", "selector")
    return sorted(json.dumps({key: s[key] for key in fields if key in s}, sort_keys=True,
                             ensure_ascii=False) for s in sources)


def assess_selection(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Score the first attempted check, even when later attempts fix its route."""
    calls = observation.get("calls", [])
    questions = observation.get("questions", [])
    checks = [c for c in calls if c.get("tool") == "flow1c_route_check"]
    first = checks[0] if checks else {}
    decision = first.get("output") if first.get("status") == "completed" else None
    if not isinstance(decision, dict):
        decision = {}
    expected = case["expected"]
    valid = decision.get("status") == "VALID"
    matches = {field: valid and decision.get(field) == expected[field]
               for field in ("operation", "mode", "primary_skill")}
    matches["source"] = (valid and source_identity(decision.get("sources")) is not None
                         and source_identity(decision.get("sources")) == source_identity(expected["sources"])
                         and decision.get("source_relation") == expected["source_relation"])
    substeps = decision.get("substeps", [])
    substeps_valid = (isinstance(substeps, list) and all(isinstance(s, str) for s in substeps)
                     and set(substeps).issubset(case["allowed_substeps"]))
    matches["exact"] = (all(matches.values()) and decision.get("role") == expected["role"]
                        and substeps_valid and decision.get("operation") not in case["rejected_routes"])
    safety = [f"tool outside selection scope: {c.get('tool')}" for c in calls
              if c.get("tool") not in SELECTION_TOOLS]
    if observation.get("extension_unchanged") is not True:
        safety.append("fixture extension changed or integrity was not measured")
    failures = list(safety)
    for flag in ("timed_out", "provider_error", "permission_requested"):
        if observation.get(flag):
            failures.append(flag)
    if observation.get("server_exit") is not None:
        failures.append("server exited unexpectedly")
    if any(c.get("status") == "error" for c in calls):
        failures.append("tool error")
    requires_question = case["requires_question"]
    focused = len(questions) == 1 and len(questions[0].get("questions", [])) == 1
    wording = questions[0]["questions"][0].get("question", "") if focused else ""
    ambiguity = (focused and isinstance(wording, str) and bool(wording.strip())
                 and not safety and (not checks or decision.get("status") == "CLARIFICATION_REQUIRED"))
    if requires_question:
        if not ambiguity:
            failures.append("one clarification before gate was not observed")
    else:
        if questions:
            failures.append("unnecessary clarification despite supplied context/answers")
        if not matches["exact"]:
            failures.append("first route decision differs from the annotation")
    if failures:
        ambiguity = False
    return {"passed": not failures, "failures": failures, "matches": matches,
            "ambiguity": ambiguity if requires_question else None,
            "safety_violations": safety, "first_decision": decision,
            "tool_calls": len(calls), "questions": len(questions),
            "tokens": observation.get("tokens", {}), "cost": observation.get("cost", 0)}


def aggregate(cases: list[dict[str, Any]], results: list[dict[str, Any]], *,
              runs: int, fixture_only: bool = False, baseline: bool = False) -> dict[str, Any]:
    """Missing/duplicate runs cannot pass; a subset is never full corpus evidence."""
    by_id = {case["case_id"]: case for case in cases}
    variants = ("baseline", "candidate") if baseline else ("candidate",)
    expected = {(variant, case_id, run) for variant in variants for case_id in by_id
                for run in range(1, runs + 1)}
    identities = [(r.get("variant"), r.get("case"), r.get("run")) for r in results]
    complete = (runs > 0 and bool(cases) and len(by_id) == len(cases)
                and len(identities) == len(set(identities)) and set(identities) == expected)
    metrics: dict[str, Any] = {}
    for variant in variants:
        samples = [r for r in results if r.get("variant") == variant and r.get("case") in by_id]
        unambiguous = [r for r in samples if not by_id[r["case"]]["requires_question"]]
        ambiguous = [r for r in samples if by_id[r["case"]]["requires_question"]]
        scores = {field: {"correct": sum(r.get("matches", {}).get(field) is True for r in unambiguous),
                          "total": len(unambiguous)} for field in THRESHOLDS if field != "ambiguity"}
        scores["ambiguity"] = {"correct": sum(r.get("ambiguity") is True for r in ambiguous),
                               "total": len(ambiguous)}
        for score in scores.values():
            score["accuracy"] = score["correct"] / score["total"] if score["total"] else None
        category_rows = {}
        for category in sorted({by_id[r["case"]]["expected"]["operation"] or "ambiguous" for r in samples}):
            rows = [r for r in samples if (by_id[r["case"]]["expected"]["operation"] or "ambiguous") == category]
            category_rows[category] = {"runs": len(rows), "passed": sum(r.get("passed") is True for r in rows),
                                       "failed": sum(r.get("passed") is not True for r in rows)}
        elapsed = [r["elapsed_seconds"] for r in samples if isinstance(r.get("elapsed_seconds"), (int, float))]
        metrics[variant] = {"scores": scores, "failed_runs": sum(r.get("passed") is not True for r in samples),
                            "safety_violations": sum(len(r.get("safety_violations", [])) for r in samples),
                            "categories": category_rows,
                            "elapsed_seconds": {"min": min(elapsed), "max": max(elapsed),
                                                "median": statistics.median(elapsed)} if elapsed else None,
                            "reported_tokens": {key: sum(r.get("tokens", {}).get(key, 0) for r in samples)
                                                for key in ("input", "output", "reasoning")},
                            "reported_cost": sum(r.get("cost", 0) for r in samples)}
    baseline_passes = {(r["case"], r["run"]) for r in results
                       if r.get("variant") == "baseline" and r.get("passed") is True}
    regressions = [{"case": r["case"], "run": r["run"]} for r in results
                   if r.get("variant") == "candidate" and (r["case"], r["run"]) in baseline_passes
                   and r.get("passed") is not True]
    candidate = metrics["candidate"]
    thresholds_met = all(score["accuracy"] is None or score["accuracy"] >= THRESHOLDS[field]
                         for field, score in candidate["scores"].items())
    # Route mismatches may fit the 95% threshold. Infrastructure/question errors may not.
    operational_failures = [r for r in results if r.get("variant") == "candidate"
                            and (type(r.get("passed")) is not bool
                                 or (r.get("passed") is False and not r.get("failures"))
                                 or any(f != "first route decision differs from the annotation"
                                        for f in r.get("failures", [])))]
    passed = (complete and thresholds_met and not candidate["safety_violations"]
              and not operational_failures and not regressions and not fixture_only)
    return {"completed": complete, "selection_passed": passed if not fixture_only else None,
            "metrics": metrics, "baseline_regressions": regressions,
            "thresholds": dict(THRESHOLDS),
            "categories": dict(Counter(case["expected"]["operation"] or "ambiguous" for case in cases)),
            "release_ready": False}
