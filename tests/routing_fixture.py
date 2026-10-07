"""Minimal product-owned route assets for isolated checkout fixtures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install_route_assets(root: Path) -> None:
    catalog = json.loads((ROOT / "config/intent-routes.json").read_text(encoding="utf-8"))
    stages = json.loads((ROOT / "config/stages.json").read_text(encoding="utf-8"))
    files = {"config/intent-routes.json", "config/stages.json"}
    files.update(route["output_contract"] for route in catalog["routes"])
    for relative in files:
        destination = root / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
    skills = {route["free_primary_skill"] for route in catalog["routes"]}
    skills.update(stage["skill"] for stage in stages["operations"].values())
    for skill in skills - {None}:
        destination = root / ".agents/skills" / skill / "SKILL.md"
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                "Discuss the goal; use chat sources; draft without changing formal status.",
                encoding="utf-8",
            )
