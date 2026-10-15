"""Pure contract for an agent's explicit assessment of reusable findings."""
from __future__ import annotations

from typing import Any

from flow1c import memory_policy as policy
from flow1c.handoff_policy import digest
from flow1c.memory_policy import ProjectMemoryError

LIFECYCLE_VERSION = 1


def requires_review(operation: str, *, project_bound: bool, allowed_tools: list[str]) -> bool:
    return project_bound and "flow1c_memory" in allowed_tools and operation not in {"setup", "update"}


def findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ProjectMemoryError("MEMORY_REVIEW_INVALID: supply 1 to 20 assessed topics")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "topic", "disposition", "records", "reason"}:
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid finding fields")
        kind, disposition = item["kind"], item["disposition"]
        if (not isinstance(kind, str) or kind not in policy.KINDS | {"none"}
                or not isinstance(disposition, str) or disposition not in {"saved", "already_covered", "not_reusable"}):
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid kind or disposition")
        topic = policy.bounded_text(item["topic"], "topic", 160)
        reason = policy.bounded_text(item["reason"], "reason", 800)
        if len(reason) < 20:
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: give a specific reason of at least 20 characters")
        records = item["records"]
        if not isinstance(records, list) or len(records) > 5:
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: at most five records per topic")
        refs = []
        for ref in records:
            if not isinstance(ref, dict) or set(ref) != {"memory_id", "revision"}:
                raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid record reference")
            refs.append({"memory_id": policy.memory_id(ref["memory_id"]),
                         "revision": policy.revision(ref["revision"])})
        if (disposition == "not_reusable" and refs or disposition != "not_reusable" and (not refs or kind == "none")):
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: saved/covered topics need typed records; skipped topics need reasons only")
        result.append({"kind": kind, "topic": topic, "disposition": disposition,
                       "records": refs, "reason": reason})
    if policy.response_size(result) > 24000:
        raise ProjectMemoryError("MEMORY_REVIEW_INVALID: review exceeds 24000 characters")
    return result


def status(items: list[dict[str, Any]]) -> str:
    dispositions = {item["disposition"] for item in items}
    return ("UPDATED" if "saved" in dispositions else
            "ALREADY_COVERED" if "already_covered" in dispositions else "NO_NEW_FINDINGS")


def validate_receipt(receipt: Any) -> None:
    fields = {"schema_version", "project_id", "scope_sha256", "findings", "records", "status", "reviewed_at"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1):
        raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid receipt version or fields")
    items = findings(receipt["findings"])
    if receipt["status"] != status(items) or not isinstance(receipt["reviewed_at"], str) or not receipt["reviewed_at"]:
        raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid receipt status")
    for key in ("project_id", "scope_sha256"):
        value = receipt[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid receipt identity")
    refs = receipt["records"]
    if not isinstance(refs, list) or len(refs) > 100:
        raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid receipt records")
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"memory_id", "revision", "sha256"}:
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid checked reference")
        policy.memory_id(ref["memory_id"])
        policy.revision(ref["revision"])
        sha = ref["sha256"]
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ProjectMemoryError("MEMORY_REVIEW_INVALID: invalid checked digest")
    expected = {(r["memory_id"], r["revision"]) for item in items for r in item["records"]}
    if len(refs) != len(expected) or {(r["memory_id"], r["revision"]) for r in refs} != expected:
        raise ProjectMemoryError("MEMORY_REVIEW_INVALID: checked records differ from assessed topics")


def observation(record: dict[str, Any], *, saved: bool) -> dict[str, Any]:
    return {"revision": record["revision"], "sha256": digest(record), "saved": saved}


def summary(gate: dict[str, Any]) -> dict[str, Any]:
    receipt = gate.get("memory_review")
    if not receipt:
        return {"status": "NOT_APPLICABLE"}
    return {"status": receipt["status"], "records": len(receipt["records"]),
            "topics": len(receipt["findings"])}
