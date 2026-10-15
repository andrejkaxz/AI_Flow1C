"""Satisfy memory preflight in synthetic tests of unrelated workflow behavior.

Memory omission/quality/recovery tests deliberately do not use this helper.
No real project findings exist in these output/query/interview fixtures.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from flow1c import storage
from flow1c.workflow import memory_actions, state


def review_before_complete(product_root: Path, gate_id: str) -> None:
    path = state.gate_path(gate_id, product_root=product_root)
    if not path.exists():
        return
    gate = storage.read_json(path)
    if (not gate.get("memory_review_required") or gate.get("completed_at")
            or gate.get("policy_version") != state.POLICY_VERSION
            or gate.get("state") not in {"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"}):
        return
    memory_actions.command(argparse.Namespace(gate_id=gate_id, action="overview",
                           request={"query": "synthetic fixture"}), product_root=product_root)
    memory_actions.command(argparse.Namespace(gate_id=gate_id, action="review", request={"findings": [{
        "kind": "none", "topic": "Synthetic workflow regression",
        "disposition": "not_reusable", "records": [],
        "reason": "Synthetic data exercises an unrelated workflow contract; it supplies no real reusable project findings."
    }]}), product_root=product_root)
