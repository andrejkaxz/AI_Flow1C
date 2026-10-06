"""Pure policy for reliable, finite Git analysis.

This module intentionally performs no filesystem, subprocess, network, or RLM I/O.
It owns public statuses, evidence ordering, compatibility, fingerprints and the
small state machine used by both the CLI and the OpenCode guard contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 2

EVIDENCE_RANK = {
    "NO_EVIDENCE": 0,
    "HEURISTIC_CANDIDATE": 1,
    "PATCH_EQUIVALENT": 2,
    "EXACT_MESSAGE_AND_PARENT": 3,
    "AUTHORITATIVE_PR": 4,
    "EXACT_TOPOLOGY": 5,
}

TERMINAL_INTEGRATION_STATUSES = {
    "MERGE_FOUND",
    "MULTIPLE_MERGES_FOUND",
    "PARTIALLY_MERGED",
    "FAST_FORWARD",
    "SQUASH_OR_REBASE_MATCH",
    "AMBIGUOUS_CANDIDATES",
    "NO_INTEGRATION_EVIDENCE",
    "REFRESH_REQUIRED",
    "SOURCE_NOT_FOUND",
    "TARGET_NOT_FOUND",
}

ACTION_ALIASES = {"integration": "merge-search"}


def select_issue_branch(issue: str, branches: Iterable[str], prefix: str = "") -> dict[str, Any]:
    """Resolve a numeric issue to one full branch name without guessing on ambiguity."""
    names = sorted(set(branches))
    if not re.fullmatch(r"[0-9]+", issue):
        return {"state": "EXPLICIT", "selected": issue, "candidates": []}
    preferred = f"{prefix.strip('/')}/{issue}" if prefix else ""
    for name, reason in ((issue, "exact"), (preferred, "configured_prefix")):
        if name and name in names:
            return {"state": "FOUND", "selected": name, "reason": reason, "candidates": [name]}
    candidates = [name for name in names if name.rsplit("/", 1)[-1] == issue]
    if len(candidates) == 1:
        return {"state": "FOUND", "selected": candidates[0], "reason": "unique_suffix", "candidates": candidates}
    return {"state": "AMBIGUOUS" if candidates else "NOT_FOUND", "selected": None,
            "candidates": candidates}


@dataclass(frozen=True)
class Candidate:
    commit: str
    target_position: int
    evidence_level: str
    integrated_source_commit: str | None = None
    introduces_commits: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "target_position": self.target_position,
            "evidence_level": self.evidence_level,
            "integrated_source_commit": self.integrated_source_commit,
            "introduces_commits": self.introduces_commits,
        }


def canonical_action(action: str) -> str:
    return ACTION_ALIASES.get(str(action), str(action))


def semantic_fingerprint(payload: Mapping[str, Any]) -> str:
    """Return a stable digest for semantically equivalent calls."""
    normalized = _normalize(payload)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if item is not None and key not in {"max_chars", "created_at", "checked_at"}}
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_normalize(item) for item in value]
        if all(isinstance(item, (str, int, float, bool, type(None))) for item in normalized):
            return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False))
        return normalized
    if isinstance(value, str):
        return value.strip()
    return value


def rank_candidate(candidate: Mapping[str, Any]) -> tuple[int, int, str]:
    """Prefer evidence strength, then newest target first-parent position."""
    evidence = EVIDENCE_RANK.get(str(candidate.get("evidence_level", "NO_EVIDENCE")), -1)
    position = int(candidate.get("target_position", -1))
    return evidence, position, str(candidate.get("commit", ""))


def select_final_merge(candidates: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    material = [dict(item) for item in candidates if item.get("commit")]
    if not material:
        return None, False
    introducing = [item for item in material if item.get("introduces_commits", True)] or material
    best_rank = max(EVIDENCE_RANK.get(str(item.get("evidence_level", "NO_EVIDENCE")), -1) for item in introducing)
    strongest = [item for item in introducing
                 if EVIDENCE_RANK.get(str(item.get("evidence_level", "NO_EVIDENCE")), -1) == best_rank]
    newest_position = max(int(item.get("target_position", -1)) for item in strongest)
    finalists = [item for item in strongest if int(item.get("target_position", -1)) == newest_position]
    finalists.sort(key=lambda item: str(item.get("commit", "")))
    ambiguous = len({str(item.get("commit")) for item in finalists}) > 1
    selected = finalists[-1]
    selected["selection_reason"] = "strongest_evidence_then_latest_first_parent_merge"
    return selected, ambiguous


def integration_status(*, candidates: list[Mapping[str, Any]], source_found: bool,
                       target_found: bool, freshness_known: bool, partial: bool = False,
                       fast_forward: bool = False, patch_coverage: float = 0.0,
                       ambiguous: bool = False) -> str:
    if not target_found:
        return "TARGET_NOT_FOUND"
    if fast_forward:
        return "FAST_FORWARD"
    if ambiguous:
        return "AMBIGUOUS_CANDIDATES"
    exact = [item for item in candidates if EVIDENCE_RANK.get(str(item.get("evidence_level")), 0) >= 3]
    if partial and exact:
        return "PARTIALLY_MERGED"
    if len({str(item.get("commit")) for item in exact}) > 1:
        return "MULTIPLE_MERGES_FOUND"
    if exact:
        return "MERGE_FOUND"
    if patch_coverage >= 1.0:
        return "SQUASH_OR_REBASE_MATCH"
    if patch_coverage > 0.0:
        return "PARTIALLY_MERGED"
    if not freshness_known:
        return "REFRESH_REQUIRED"
    if not source_found and not candidates:
        return "SOURCE_NOT_FOUND"
    return "NO_INTEGRATION_EVIDENCE"


def next_actions_for(status: str, final_merge: Mapping[str, Any] | None = None) -> list[str]:
    if status in {"MERGE_FOUND", "MULTIPLE_MERGES_FOUND", "PARTIALLY_MERGED"}:
        return ["branch-changes", "diff:names", "diff:stat"]
    if status in {"FAST_FORWARD", "SQUASH_OR_REBASE_MATCH"}:
        return ["branch-changes", "diff:names"]
    if status == "REFRESH_REQUIRED":
        return ["git-refresh"]
    if status == "AMBIGUOUS_CANDIDATES":
        return ["review-candidates", "ask-one-material-question"]
    return ["complete-with-limitations"]


def validate_transition(state: Mapping[str, Any], action: str, fingerprint: str) -> str:
    """Return EXECUTE/CACHE_HIT or raise a typed finite-state violation."""
    action = canonical_action(action)
    cache = state.get("cache", {}) if isinstance(state.get("cache"), Mapping) else {}
    if fingerprint in cache:
        return "CACHE_HIT"
    terminal = state.get("terminal", {}) if isinstance(state.get("terminal"), Mapping) else {}
    if action == "latest-merge" and terminal.get("merge-search"):
        raise ValueError("INVALID_NEXT_ACTION: latest-merge is forbidden after terminal merge-search")
    if action == "merge-search" and terminal.get("merge-search"):
        raise ValueError("ALREADY_RESOLVED: equivalent refs already have terminal merge evidence")
    if action == "history-search" and int(state.get("history_search_without_evidence", 0)) >= 2:
        raise ValueError("INVALID_NEXT_ACTION: history-search budget exhausted without new evidence")
    return "EXECUTE"


def migrate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Read v1/legacy evidence while always emitting schema v2."""
    migrated = dict(record)
    version = int(migrated.get("schema_version", 1) or 1)
    if version > SCHEMA_VERSION:
        raise ValueError(f"Unsupported git analysis schema_version {version}")
    legacy_status = migrated.get("status") or migrated.get("integration_status")
    if legacy_status == "NOT_MERGED":
        migrated["legacy_status"] = "NOT_MERGED"
        migrated["integration_status"] = "NO_INTEGRATION_EVIDENCE"
        migrated["status"] = "NO_INTEGRATION_EVIDENCE"
    elif legacy_status == "NO_MERGE_COMMIT":
        migrated["legacy_status"] = "NO_MERGE_COMMIT"
        migrated["integration_status"] = "FAST_FORWARD"
        migrated["status"] = "FAST_FORWARD"
    elif legacy_status and "integration_status" not in migrated:
        migrated["integration_status"] = legacy_status
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def legacy_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """Add old fields without weakening the new contract."""
    result = dict(record)
    status = str(result.get("integration_status", result.get("status", "")))
    result["status"] = status
    final_merge = result.get("final_merge")
    if isinstance(final_merge, Mapping):
        result.setdefault("merge_commit", final_merge.get("commit"))
        result.setdefault("merge_parents", final_merge.get("parents", []))
        result.setdefault("merge_subject", final_merge.get("subject", ""))
        result.setdefault("review_ref", final_merge.get("commit"))
        parents = final_merge.get("parents", [])
        if parents:
            result.setdefault("diff_base_commit", parents[0])
    if status == "FAST_FORWARD":
        result.setdefault("review_ref", result.get("integrated_source_commit") or result.get("source_commit"))
    return result
