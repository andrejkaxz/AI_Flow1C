"""Pure selection, source identities and range coverage for context v1."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from flow1c.errors import WorkflowError

ROLE_FILES = {
    "analyst": ["analysis/questions.md", "analysis/answers.md", "analysis/decisions.md"],
    "functional-architect": ["analysis/traceability.md", "specification/functional-spec.md"],
    "technical-architect": ["analysis/traceability.md", "specification/functional-spec.md",
                            "specification/technical-design.md"],
    "tester": ["analysis/traceability.md", "specification/functional-spec.md", "testing/test-plan.md"],
}


class ContextError(WorkflowError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "component": "context", "message": str(self),
                "recoverable": True, "preserved_state": ["gate", "answers", "context coverage"],
                "next_action": "Rebuild compact context on the same gate if sources changed; otherwise read the remaining manifest parts."}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_limits(value: dict[str, Any]) -> dict[str, int]:
    ceilings = {"compact_chars": 12000, "page_chars": 8000, "response_chars": 24000,
                "max_source_bytes": 10485760, "max_entries": 1000}
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ContextError("CONTEXT_INVALID", "Unsupported context configuration version")
    for key, ceiling in ceilings.items():
        if type(value.get(key)) is not int or not 1 <= value[key] <= ceiling:
            raise ContextError("CONTEXT_INVALID", f"Invalid context limit: {key}")
    if value["compact_chars"] < 1000 or value["response_chars"] < 1000:
        raise ContextError("CONTEXT_INVALID", "Context envelope limits must be at least 1000 characters")
    return {key: value[key] for key in ceilings}


def source_specs(gate: dict[str, Any], manifest: dict[str, Any],
                 artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select only managed documents; XML/BSL and arbitrary paths never enter here."""
    result: dict[str, dict[str, Any]] = {}
    review = gate.get("operation") == "functional-review"
    formal = gate.get("mode", "formal") == "formal"
    if formal:
        basis = ("input/user-brief.md" if manifest.get("traceability_mode") == "provisional"
                 else "input/requirements.snapshot.yaml")
        for path in [basis, *ROLE_FILES.get(str(gate.get("context_role")), [])]:
            result[path] = {"path": path, "reason": "requirements basis" if path == basis else "role document",
                            "required": review and path in {basis, "analysis/traceability.md", "specification/functional-spec.md"}}
    for artifact in artifacts:
        if formal and artifact.get("category") == "requirements_workbook":
            # The managed normalized requirements basis already owns this input.
            continue
        original = str(artifact.get("relative_path") or artifact.get("path") or "")
        path = str(artifact.get("derived_path") or original)
        if not path:
            continue
        result[path] = {"path": path, "reason": "accepted artifact", "required": review,
                        "accepted_sha256": artifact.get("derived_sha256") if artifact.get("derived_path") else artifact.get("sha256"),
                        "original_path": original if path != original else None,
                        "original_sha256": artifact.get("sha256") if path != original else None}
    if review and not result:
        result["input/functional-spec.md"] = {"path": "input/functional-spec.md", "reason": "functional review requires accepted specification", "required": True}
    return list(result.values())


def parts(length: int, page_chars: int, required: bool) -> list[dict[str, Any]]:
    """Exact character ranges cover every byte-decoded part, including headings/code."""
    return [{"section_id": f"part-{i // page_chars + 1:05d}", "start": i,
             "end": min(i + page_chars, length), "required": required}
            for i in range(0, length, page_chars)]


def merge_ranges(ranges: list[list[int]], start: int, end: int) -> list[list[int]]:
    merged: list[list[int]] = []
    for left, right in sorted([*ranges, [start, end]]):
        if left == right:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(right, merged[-1][1])
        else:
            merged.append([left, right])
    return merged


def unread_parts(manifest: dict[str, Any], coverage: dict[str, Any]) -> list[str]:
    unread = []
    for entry in manifest["entries"]:
        ranges = coverage.get("ranges", {}).get(entry["entry_id"], [])
        if entry["required"] and not entry["readable"]:
            unread.append(entry["entry_id"] + ":unavailable")
        for part in entry["parts"]:
            if part["required"] and not any(left <= part["start"] and right >= part["end"] for left, right in ranges):
                unread.append(entry["entry_id"] + ":" + part["section_id"])
    return unread


def coverage_summary(manifest: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    unread = unread_parts(manifest, coverage)
    return {"complete": not unread, "unread_required_count": len(unread),
            "unread_required_parts": unread[:20], "unread_list_truncated": len(unread) > 20}


def validate_records(manifest: dict[str, Any], coverage: dict[str, Any]) -> None:
    """Validate nested persisted contracts even when optional JSON Schema is absent."""
    try:
        validate_limits({"schema_version": 1, **manifest["limits"]})
        if not isinstance(manifest["entries"], list) or len(manifest["entries"]) > manifest["limits"]["max_entries"]:
            raise ValueError("invalid entries")
        entries = {}
        for entry in manifest["entries"]:
            if (type(entry["length"]) is not int or entry["length"] < 0
                    or type(entry["required"]) is not bool or type(entry["readable"]) is not bool
                    or not isinstance(entry["path"], str) or len(entry["path"]) > 1024
                    or entry["length"] > manifest["limits"]["max_source_bytes"]
                    or entry["sha256"] is not None and (not isinstance(entry["sha256"], str) or not re.fullmatch("[a-f0-9]{64}", entry["sha256"]))):
                raise ValueError("invalid entry")
            original = entry["original"]
            if original is not None and (not isinstance(original, dict) or not isinstance(original.get("path"), str)
                                         or not isinstance(original.get("sha256"), str) or not re.fullmatch("[a-f0-9]{64}", original["sha256"])):
                raise ValueError("invalid provenance")
            identity = digest({"scope_id": manifest["scope_id"], "path": entry["path"],
                               "sha256": entry["sha256"], "original": entry["original"]})
            if (entry["identity"] != identity or entry["entry_id"] != "src-" + identity[:24]
                    or entry["entry_id"] in entries
                    or entry["parts"] != (parts(entry["length"], manifest["limits"]["page_chars"], entry["required"]) if entry["readable"] else [])):
                raise ValueError("invalid entry identity/parts")
            entries[entry["entry_id"]] = entry
        if not isinstance(coverage["ranges"], dict) or not isinstance(coverage["cursors"], dict):
            raise ValueError("invalid coverage")
        for entry_id, ranges in coverage["ranges"].items():
            if entry_id not in entries or not isinstance(ranges, list):
                raise ValueError("unknown coverage entry")
            previous = -1
            for interval in ranges:
                if (not isinstance(interval, list) or len(interval) != 2
                        or any(type(v) is not int for v in interval)
                        or not previous < interval[0] < interval[1] <= entries[entry_id]["length"]):
                    raise ValueError("invalid coverage range")
                previous = interval[1]
        for token, cursor in coverage["cursors"].items():
            if (not isinstance(cursor, dict) or type(cursor.get("offset")) is not int or cursor["offset"] < 0
                    or cursor.get("entry_id") not in {*entries, "scope", "index"}
                    or not isinstance(cursor.get("section_id"), str)
                    or not isinstance(cursor.get("sha256"), str)
                    or token != digest({"manifest": coverage["manifest_sha256"], **cursor})):
                raise ValueError("invalid cursor")
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextError("CONTEXT_INVALID", "Invalid nested context manifest or coverage contract") from exc


def scope_record(gate: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    return {"goal": gate.get("summary") or manifest.get("title") or "",
            "operation": gate.get("operation"), "mode": gate.get("mode", "formal"),
            "role": gate.get("context_role"), "route": gate.get("route_decision"),
            "source_scope": (gate.get("route_decision") or {}).get("sources", []),
            "status": gate.get("state"), "work_item_status": manifest.get("status"),
            "work_reference": gate.get("work_reference") or gate.get("code"),
            "project_reference": gate.get("project_reference"), "task_reference": gate.get("task_reference"),
            "requirements": manifest.get("requirements", gate.get("requirements", [])),
            "traceability_mode": manifest.get("traceability_mode"),
            "approvals": manifest.get("approvals", {}),
            "answers": gate.get("answers", []), "decisions": gate.get("notes", []),
            "assumptions": gate.get("assumptions", []), "open_questions": gate.get("open_questions", []),
            "constraints": gate.get("conditions", []), "deviations": gate.get("deviations", []),
            "blockers": gate.get("remaining_blockers", [])}


def compact_text(manifest: dict[str, Any], scope: dict[str, Any], coverage: dict[str, Any],
                 limit: int) -> str:
    lines = ["# Flow1C compact context v1", "Summary/index only; source text and evidence require gated reads.",
             "Read entry_id=scope for the complete goal, answers, decisions, constraints and blockers.",
             "Read entry_id=index for all source/part IDs. XML/BSL facts still require source tools."]
    for key in ("goal", "operation", "mode", "role", "status", "route", "answers", "decisions", "constraints", "blockers"):
        text = json.dumps(scope.get(key), ensure_ascii=False, separators=(",", ":"))
        lines.append(f"{key}: {text[:300]}" + (" [abbreviated; read scope]" if len(text) > 300 else ""))
    summary = coverage_summary(manifest, coverage)
    compact_coverage = {key: summary[key] for key in ("complete", "unread_required_count")}
    lines.append("Coverage: " + json.dumps(compact_coverage, ensure_ascii=False))
    prefix = "\n".join(lines) + "\n"
    # Small configured budgets retain exact metadata through the scope entry.
    if len(prefix) > limit - 150:
        prefix = "# Flow1C compact context v1\nMetadata abbreviated: read scope; full source/part index: read index.\n"
        prefix += "Coverage: " + json.dumps(compact_coverage, ensure_ascii=False) + "\n"
    shown = 0
    for entry in manifest["entries"]:
        row = json.dumps({key: entry[key] for key in ("entry_id", "path", "reason", "required", "readable", "length")}, ensure_ascii=False) + "\n"
        if len(prefix) + len(row) > limit - 100:
            break
        prefix += row
        shown += 1
    return prefix + f"Index continuation: read index ({len(manifest['entries']) - shown} entries omitted).\n"
