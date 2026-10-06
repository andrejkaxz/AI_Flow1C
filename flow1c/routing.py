"""Read product-owned route configuration; no gate creation or user-state I/O."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flow1c.routing_policy import RoutingError, build_rules, check_route


def load_rules(product_root: Path) -> dict[str, Any]:
    try:
        catalog = json.loads((product_root / "config/intent-routes.json").read_text(encoding="utf-8-sig"))
        stages = json.loads((product_root / "config/stages.json").read_text(encoding="utf-8-sig"))
        skills = {path.parent.name for path in (product_root / ".agents/skills").glob("flow1c-*/SKILL.md")
                  if path.is_file()}
        rules = build_rules(catalog, stages, skills)
        if any(not (product_root / route["output_contract"]).is_file()
               for route in rules["routes"].values()):
            raise RoutingError("A referenced product output contract is unavailable.")
        return rules
    except RoutingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RoutingError("Product route configuration cannot be loaded or composed.") from exc


def check_proposal(proposal: Any, *, product_root: Path) -> dict[str, Any]:
    return check_route(proposal, load_rules(product_root))
