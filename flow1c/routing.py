"""Read product-owned route configuration; no gate creation or user-state I/O."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from flow1c.routing_policy import (
    RoutingError, build_rules, check_route, check_begin_route,
)


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


def route_catalog(*, product_root: Path) -> dict[str, Any]:
    """Expose routing metadata without permission owners or user-state reads."""
    rules = load_rules(product_root)
    result = {key: deepcopy(rules[key]) for key in (
        "schema_version", "policy_version", "digest", "routes", "source_rules", "substeps",
    )}
    try:
        result["proposal_schema"] = json.loads(
            (product_root / "schemas/route-proposal.schema.json").read_text(encoding="utf-8-sig")
        )
        if not isinstance(result["proposal_schema"], dict):
            raise ValueError("Expected a schema object")
    except (OSError, UnicodeError, ValueError) as exc:
        raise RoutingError("Product RouteProposal schema cannot be loaded.") from exc
    return result


def begin_route(proposal: Any, inputs: dict[str, Any], *,
                product_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load product rules and return checked route data without creating state."""
    decision = check_begin_route(proposal, inputs, load_rules(product_root))
    metadata = {"route_origin": "legacy" if proposal is None else "structured"}
    if proposal is not None and decision["status"] == "VALID":
        metadata["route_proposal"] = deepcopy(proposal)
    return decision, metadata
