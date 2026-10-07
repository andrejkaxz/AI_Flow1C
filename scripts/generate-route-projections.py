"""Generate marked route instructions from product owners; --check never writes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flow1c.routing import load_rules

START = "<!-- flow1c:routes:start -->"
END = "<!-- flow1c:routes:end -->"


def cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render(routes: list[dict]) -> str:
    lines = [START, "Generated from `config/intent-routes.json` and `config/stages.json`.", "",
             "Interpret the user's goal, then check a RouteProposal before begin. A route grants no permissions.",
             "CLI: `route-catalog --json`, `route-check --json-stdin` (direct proposal), then `agent-begin --json-stdin` (nested `route_proposal`).",
             "OpenCode: `flow1c_route_catalog`, `flow1c_route_check(proposal_json)`, then `flow1c_begin(route_proposal_json)`; operation/mode/summary must match.",
             'Minimal proposal shape: `{"schema_version":1,"expected_outcome":"user goal","operation":"consultation","mode":"explore","sources":[{"kind":"chat","version":"provided"}]}`. Replace operation/mode/sources for the actual request; `summary` and `route_id` are not proposal fields.',
             "The pre-gate catalog returns `proposal_schema`; use it to correct invalid inputs without read/grep/bash. Do not retry the same invalid proposal unchanged.",
             "Reuse saved answers; clarify one unresolved choice before gate. Do not launch subagents or a next formal stage automatically.",
             "For large accepted documents use `flow1c_context(view=compact)` / `agent-context --view compact`. Read entries with `flow1c_context(action=read,entry_id=...,cursor=...)` / `context-read`; `scope` contains exact saved decisions and `index` lists every source/part. Continue cursors until mandatory coverage is complete. Summaries grant no evidence or permissions; changed sources require rebuilding on the same gate. Legacy full view remains the default.",
             "", "| Operation | Mode → primary skill / role | Apply when | Exclude | Sources / output |",
             "|---|---|---|---|---|"]
    for route in routes:
        modes = "; ".join(f"{mode} → {route['primary_skills'][mode]} / {route['roles'][mode] or '—'}"
                          for mode in route["modes"])
        fields = [route["operation"], modes, "; ".join(route["activation"]),
                  "; ".join(route["exclusions"]),
                  ", ".join(route["sources"]) + " / " + route["output_contract"]]
        lines.append("| " + " | ".join(cell(field) for field in fields) + " |")
    return "\n".join([*lines, END])


def replace_block(text: str, block: str) -> str:
    if START not in text and END not in text:
        return text.rstrip("\n") + "\n\n" + block + "\n"
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) >= text.index(END):
        raise ValueError("Malformed generated route markers; preserve the file and repair its markers")
    start, end = text.index(START), text.index(END) + len(END)
    return text[:start] + block + text[end:]


def generate(product_root: Path, *, check: bool) -> list[str]:
    routes = list(load_rules(product_root)["routes"].values())
    targets = {name: routes for name in (
        "AGENTS.md", "CLAUDE.md", "docs/intent-routing.md", ".opencode/agents/flow1c-controller.md",
    )}
    skills = {skill for route in routes for skill in route["primary_skills"].values()}
    for skill in sorted(skills):
        matching = [route for route in routes if skill in route["primary_skills"].values()]
        for directory in (".agents", ".claude"):
            targets[f"{directory}/skills/{skill}/SKILL.md"] = matching
    drift = []
    updates = []
    for relative, subset in targets.items():
        path = product_root / relative
        text = path.read_text(encoding="utf-8")
        updated = replace_block(text, render(subset))
        if text != updated:
            drift.append(relative)
            updates.append((path, updated))
    if not check:
        for path, updated in updates:
            path.write_text(updated, encoding="utf-8")
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    drift = generate(ROOT, check=args.check)
    if drift:
        print(("Route projection drift: " if args.check else "Updated route projections: ") + ", ".join(drift))
    return 1 if args.check and drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
