"""Live OpenCode route selection on frozen annotations in disposable fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.opencode_evals import build_fixture, git, run_case, tool_calls, write_json
from scripts.routing_eval_policy import EVALUATOR_VERSION, aggregate, assess_selection, corpus_errors

PROTOCOL = (
    "Проверь только выбор маршрута для запроса выше через flow1c_route_catalog и "
    "flow1c_route_check. После первого решения остановись. Если цель существенно "
    "неоднозначна, задай один предметный вопрос через question и жди ответа. "
    "Не открывай gate и не выполняй задачу: это отдельная проверка маршрутизации."
)


def selection_prompt(case: dict[str, Any]) -> str:
    """Only user inputs reach the model; expected labels and split stay private."""
    parts = []
    if case.get("context"):
        parts.append("Предыдущий контекст и предоставленные материалы:\n" + "\n".join(case["context"]))
    if case.get("answers"):
        parts.append("Уже полученные ответы пользователя:\n" + "\n".join(case["answers"]))
    parts.extend(("Текущий запрос:\n" + case["prompt"], PROTOCOL))
    return "\n\n".join(parts)


def observe(messages: list[dict[str, Any]], questions: list[dict[str, Any]], **runtime: Any) -> dict[str, Any]:
    calls = []
    for call in tool_calls(messages):
        state = call.get("state", {})
        output = state.get("output")
        try:
            output = json.loads(output) if isinstance(output, str) else output
        except ValueError:
            output = None
        calls.append({"tool": call.get("tool"), "status": state.get("status"),
                      "input": state.get("input", {}), "output": output,
                      "started": state.get("time", {}).get("start")})
    calls.sort(key=lambda c: c["started"] if isinstance(c["started"], (int, float)) else float("inf"))
    assistant = [m.get("info", {}) for m in messages if m.get("info", {}).get("role") == "assistant"]
    tokens = {key: sum(m.get("tokens", {}).get(key, 0) for m in assistant)
              for key in ("input", "output", "reasoning")}
    return {"calls": calls, "questions": questions, "tokens": tokens,
            "cost": sum(m.get("cost", 0) for m in assistant),
            "provider_error": any(m.get("error") for m in assistant), **runtime}


def selection_observed(messages: list[dict[str, Any]], questions: list[dict[str, Any]]) -> bool:
    if questions:
        return True
    checks = [c for c in observe(messages, questions)["calls"] if c["tool"] == "flow1c_route_check"]
    if not checks or checks[0]["status"] not in {"completed", "error"}:
        return False
    # A clarification decision must be followed by an actual question, not prose.
    output = checks[0]["output"]
    return not isinstance(output, dict) or output.get("status") != "CLARIFICATION_REQUIRED"


def assess_opencode_selection(case: dict[str, Any], messages: list[dict[str, Any]],
                             questions: list[dict[str, Any]], **runtime: Any) -> dict[str, Any]:
    result = assess_selection(case["annotation"], observe(messages, questions, **runtime))
    models = [{"model": f"{m['info'].get('providerID', '')}/{m['info'].get('modelID', '')}",
               "variant": m["info"].get("variant")} for m in messages
              if m.get("info", {}).get("role") == "assistant"]
    result["observed_models"] = models
    if case.get("target_model") and (not models or any(m["model"] != case["target_model"] for m in models)):
        result["failures"].append("configured target model was not observed")
    if case.get("target_variant") and (not models or any(m["variant"] != case["target_variant"] for m in models)):
        result["failures"].append("configured target variant was not observed")
    result["passed"] = not result["failures"]
    return result


def artifact_hashes(root: Path) -> dict[str, str]:
    """Freeze evaluator, annotations and client instructions, including dirty code."""
    paths = {Path(name) for name in ("scripts/routing_eval_policy.py", "scripts/routing_evals.py",
                                   "scripts/opencode_evals.py", "evals/routing-cases.json",
                                   "config/intent-routes.json", "config/stages.json", "AGENTS.md",
                                   "CLAUDE.md", "opencode.json")}
    for directory in ("flow1c", "scripts", ".agents", ".claude", ".opencode", "schemas",
                      "config", "docs", "standards", "templates"):
        paths.update(p.relative_to(root) for p in (root / directory).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and "node_modules" not in p.parts
                     and p.suffix != ".pyc")
    return {p.as_posix(): hashlib.sha256((root / p).read_bytes()).hexdigest()
            for p in sorted(paths) if (root / p).is_file()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact configured provider/model ID")
    parser.add_argument("--variant", help="Configured reasoning variant, e.g. medium")
    parser.add_argument("--opencode", default=shutil.which("opencode"))
    parser.add_argument("--runs", type=int, default=1, help="Selection repeats; E2E acceptance is separate")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--split", choices=("all", "development", "held-out"), default="all")
    parser.add_argument("--baseline-ref", help="Local product Git ref on the identical model/variant")
    parser.add_argument("--output-dir", type=Path, required=True, help="New disposable directory outside this checkout")
    parser.add_argument("--fixture-only", action="store_true")
    args = parser.parse_args(argv)
    provider, separator, model = args.model.partition("/")
    if not separator or not provider.strip() or not model.strip() or args.runs < 1 or args.timeout < 1:
        parser.error("exact provider/model ID and positive runs/timeout are required")
    if not args.fixture_only and (not args.opencode or not Path(args.opencode).is_file()):
        parser.error("native OpenCode CLI executable is required")
    output = args.output_dir.resolve()
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output) or output.exists():
        parser.error("output must be a new disposable directory outside the product checkout")
    corpus = json.loads((ROOT / "evals/routing-cases.json").read_text(encoding="utf-8"))
    errors = corpus_errors(corpus["cases"])
    if errors:
        parser.error("invalid corpus: " + "; ".join(errors))
    cases = [c for c in corpus["cases"] if args.split == "all" or c["split"] == args.split]
    if args.case_ids:
        unknown = set(args.case_ids) - {c["case_id"] for c in cases}
        if unknown:
            parser.error("unknown cases in selected split: " + ", ".join(sorted(unknown)))
        cases = [c for c in cases if c["case_id"] in args.case_ids]
    if not cases:
        parser.error("no cases selected")
    frozen = artifact_hashes(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "annotations.json", corpus)
    write_json(output / "artifacts.json", frozen)
    baseline_commit = git(ROOT, "rev-parse", "--verify", args.baseline_ref + "^{commit}").decode().strip() if args.baseline_ref else None
    summary = {"schema_version": 1, "evaluator_version": EVALUATOR_VERSION,
               "scope": "route selection only; no execution/result/resume acceptance",
               "run_id": uuid.uuid4().hex, "model": args.model, "variant": args.variant,
               "python": platform.python_version(), "platform": platform.platform(),
               "candidate_commit": git(ROOT, "rev-parse", "HEAD").decode().strip(),
               "candidate_dirty": bool(git(ROOT, "status", "--porcelain").strip()),
               "baseline_commit": baseline_commit, "annotation_version": corpus["annotation_version"],
               "split": args.split, "runs_per_case": args.runs, "fixture_only": args.fixture_only,
               "full_corpus": len(cases) == len(corpus["cases"]), "results": [],
               "completed": False, "selection_passed": None, "release_ready": False}
    variants = [("baseline", baseline_commit), ("candidate", None)] if baseline_commit else [("candidate", None)]
    write_json(output / "summary.json", summary)
    for case in cases:
        protocol_case = {"id": case["case_id"], "prompt": selection_prompt(case), "annotation": case,
                         "target_model": args.model, "target_variant": args.variant}
        for run in range(1, args.runs + 1):
            for variant, revision in variants:
                destination = output / f"{case['case_id']}-{variant}-{run:02}"
                if args.fixture_only:
                    build_fixture(destination, protocol_case, baseline_ref=revision)
                    result = {"fixture_created": True, "passed": None}
                else:
                    result = run_case(args.opencode, args.model, protocol_case, destination,
                                      timeout=args.timeout, baseline_ref=revision,
                                      assessor=assess_opencode_selection,
                                      observation_complete=selection_observed, model_variant=args.variant)
                write_json(destination / "prompt.json", {"protocol_version": 1, "prompt": protocol_case["prompt"]})
                summary["results"].append({"case": case["case_id"], "variant": variant, "run": run, **result})
                summary.update(aggregate(cases, summary["results"], runs=args.runs,
                                         fixture_only=args.fixture_only, baseline=bool(baseline_commit)))
                write_json(output / "summary.json", summary)
                print(json.dumps({"case": case["case_id"], "variant": variant, "run": run,
                                  "passed": result["passed"], "failures": result.get("failures", [])},
                                 ensure_ascii=False), flush=True)
    summary["artifacts_unchanged"] = frozen == artifact_hashes(ROOT)
    if not summary["artifacts_unchanged"]:
        summary["selection_passed"] = False
    summary["corpus_acceptance_passed"] = (summary["selection_passed"] is True and summary["full_corpus"]
                                            and bool(baseline_commit) and summary["artifacts_unchanged"])
    summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(output / "summary.json", summary)
    print(f"Summary: {output / 'summary.json'}", flush=True)
    return 0 if args.fixture_only or summary["selection_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
