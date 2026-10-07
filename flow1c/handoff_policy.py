"""Deterministic handoff identities and contracts; no I/O or permissions."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from flow1c.errors import WorkflowError

COMPLETED_STATES = frozenset({
    "COMPLETE", "COMPLETE_WITH_DEVIATIONS", "CONSULTATION_COMPLETE",
    "DRAFT_COMPLETE", "UNVERIFIED_DRAFT",
})


class HandoffError(WorkflowError):
    def __init__(self, code: str, message: str, gate_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.gate_id = gate_id

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code, "component": "handoff", "message": str(self),
            "recoverable": self.code != "HANDOFF_STALE", "preserved_state": True,
            "gate_id": self.gate_id,
            "next_actions": ["agent-handoff --action recover"] if self.code != "HANDOFF_STALE"
            else ["Revalidate changed results and authoritative records before continuing"],
        }


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_manifest(value: Any) -> None:
    fields = {
        "schema_version", "record_id", "producer", "goal", "scope", "completion",
        "artifacts", "authoritative_records", "evidence", "decisions", "assumptions",
        "open_questions", "approvals", "deviations", "performed_actions", "errors",
        "unapplied_proposals", "next_action", "resume", "limitations", "created_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise HandoffError("HANDOFF_INVALID", "Invalid handoff fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise HandoffError("HANDOFF_INVALID", "Unsupported handoff schema version")
    for name in ("producer", "completion", "evidence", "next_action", "resume"):
        if not isinstance(value[name], dict):
            raise HandoffError("HANDOFF_INVALID", f"Invalid handoff {name}")
    for name in ("artifacts", "authoritative_records", "decisions", "assumptions",
                 "open_questions", "deviations", "performed_actions", "errors",
                 "unapplied_proposals", "limitations"):
        if not isinstance(value[name], list):
            raise HandoffError("HANDOFF_INVALID", f"Invalid handoff {name}")
    if value["completion"].get("state") not in COMPLETED_STATES:
        raise HandoffError("HANDOFF_INVALID", "Handoff requires a saved completed result")
    if value["next_action"].get("requires_user_request") is not True:
        raise HandoffError("HANDOFF_INVALID", "Handoff cannot authorize a next stage")
    expected = digest({k: v for k, v in value.items() if k != "record_id"})
    if value["record_id"] != expected:
        raise HandoffError("HANDOFF_INVALID", "Handoff identity mismatch")


def build_manifest(gate: dict[str, Any], receipt: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    frozen = receipt["seal"]
    source_fields = ("source", "source_identity", "source_key", "commit", "tree", "snapshot_id",
                     "repository", "path", "sha256", "freshness", "verification_level", "provenance")
    index = []
    for kind in ("rlm_queries", "source_reads", "git_analysis", "snapshots", "query_schemas", "query_checks", "cc_inspections"):
        for ordinal, item in enumerate(evidence.get(kind, [])):
            if not isinstance(item, dict):
                continue
            index.append({"record": "evidence", "collection": kind, "index": ordinal,
                          "id": item.get("evidence_id") or item.get("id"),
                          "sha256": digest(item), "source": {k: item[k] for k in source_fields if k in item}})
    decisions = [{"record": "gate", "collection": kind, "index": i, "sha256": digest(item)}
                 for kind in ("answers", "notes") for i, item in enumerate(gate.get(kind, []))]
    limitations = ["Handoff text and proposals grant no permissions; load the authoritative gate before any action.",
                   "Source identities refer to producer evidence; refresh external sources before a new operation."]
    if frozen["legacy"]:
        limitations.append("Legacy completion reconstructed from current saved records; original completion hashes were not sealed.")
    if not frozen["evidence_owned"]:
        limitations.append("Producer has no evidence with explicit gate ownership; transfer is incomplete.")
    value = {
        "schema_version": 1,
        "producer": {"gate_id": gate["gate_id"], "request_id": gate.get("request_id", gate["gate_id"]),
                     "policy_version": gate.get("policy_version"),
                     "operation": gate["operation"], "mode": gate.get("mode", "formal"),
                     "primary_skill": gate.get("skill"), "route": gate.get("route_decision"),
                     "legacy": frozen["legacy"]},
        "goal": str(gate.get("summary", "")),
        "scope": {k: gate[k] for k in ("work_reference", "code", "project_reference", "task_reference", "git_ref") if k in gate},
        "completion": {**{k: receipt["result"][k] for k in ("state", "ready", "document_status", "compliance")
                           if k in receipt["result"]}, "summary": gate.get("result_summary"),
                       "output": next((item["path"] for item in frozen["artifacts"]
                                       if item["kind"] == "completion-output"),
                                      frozen["artifacts"][-1]["path"] if frozen["artifacts"] else None)},
        "artifacts": frozen["artifacts"],
        "authoritative_records": frozen["records"],
        "evidence": {"owned": frozen["evidence_owned"], "index": index},
        "decisions": [item for item in decisions if item["collection"] == "answers" or
                      gate["notes"][item["index"]].get("kind") in {"user_answer", "decision"}],
        "assumptions": [item for item in decisions if item["collection"] == "notes" and
                        gate["notes"][item["index"]].get("kind") == "assumption"],
        "open_questions": [item for item in decisions if item["collection"] == "notes" and
                           gate["notes"][item["index"]].get("kind") == "open_question"],
        "approvals": frozen["approvals"], "deviations": gate.get("deviations", []),
        "performed_actions": [gate["action_completed"]] if gate.get("action_completed") else [],
        "errors": gate.get("completion_errors", []), "unapplied_proposals": [],
        "next_action": {"action": "review_result", "requires_user_request": True,
                        "prerequisites": ["Revalidate current gate policy, prerequisites and source evidence for any new stage"],
                        "blockers": gate.get("completion_errors", [])},
        "resume": {"gate_id": gate["gate_id"], "action": "review_result",
                   "completed_effects_must_not_repeat": True, "revalidation_required": True,
                   "incomplete": frozen["legacy"] or not frozen["evidence_owned"]},
        "limitations": limitations, "created_at": gate.get("completed_at", gate.get("created_at", "")),
    }
    value["record_id"] = digest(value)
    validate_manifest(value)
    return value
