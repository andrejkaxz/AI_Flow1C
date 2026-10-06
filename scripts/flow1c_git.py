"""Bounded, read-only Git execution for Flow1C.

All Git invocations are argument arrays. Analysis commands disable prompts,
pagers, external diff drivers and optional locks. No function changes HEAD,
index, tracked files, untracked files, or repository configuration.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

try:
    from flow1c_git_policy import (integration_status, legacy_view, next_actions_for,
                                   select_final_merge, semantic_fingerprint, select_issue_branch)
except ModuleNotFoundError:
    from scripts.flow1c_git_policy import (integration_status, legacy_view, next_actions_for,
                                           select_final_merge, semantic_fingerprint, select_issue_branch)


SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$", re.IGNORECASE)
SAFE_REF_PATTERN = re.compile(r"^[^\x00-\x20~^:?*\\\[\]]+$")
READABLE_EXTENSIONS = {".bsl", ".xml", ".json", ".md"}
MAX_GIT_OUTPUT = 16 * 1024 * 1024
READ_ONLY_GIT_COMMANDS = {
    "cherry", "diff", "diff-tree", "log", "ls-tree", "merge-base",
    "patch-id", "remote", "rev-list", "rev-parse", "show", "ls-remote",
}


class GitAnalysisError(RuntimeError):
    def __init__(self, code: str, message: str, *, recoverable: bool = False,
                 next_action: str = "complete-with-limitations", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = {
            "code": code,
            "component": "git-analysis",
            "message": message,
            "recoverable": recoverable,
            "next_action": next_action,
            "repository_changed": False,
            "details": dict(details or {}),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_ref(raw_ref: str) -> str:
    ref = str(raw_ref or "").strip()
    if not ref or len(ref) > 512 or ref.startswith("-") or ref.endswith("/") or ".." in ref or "@{" in ref:
        raise GitAnalysisError("GIT_REF_NOT_FOUND", "Invalid or empty Git ref", details={"requested_ref": ref})
    if not SAFE_REF_PATTERN.fullmatch(ref):
        raise GitAnalysisError("GIT_REF_NOT_FOUND", "Git ref contains forbidden characters", details={"requested_ref": ref})
    return ref


def run_git(repository: Path, arguments: list[str], *, timeout: int = 45,
            max_output: int = MAX_GIT_OUTPUT, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise TypeError("Git arguments must be a list of strings")
    if not arguments or arguments[0] not in READ_ONLY_GIT_COMMANDS | {"fetch"}:
        raise GitAnalysisError("GIT_COMMAND_REJECTED", "Git command is outside the analysis allowlist",
                               details={"command": arguments[0] if arguments else None})
    if arguments[0] == "remote" and arguments[1:2] != ["get-url"]:
        raise GitAnalysisError("GIT_COMMAND_REJECTED", "Only git remote get-url is allowed")
    if arguments[0] == "ls-remote" and arguments != ["ls-remote", "--heads", "origin"]:
        raise GitAnalysisError("GIT_COMMAND_REJECTED", "Only origin branch discovery is allowed")
    if arguments[0] == "fetch":
        prefix = ["fetch", "--no-tags", "--no-recurse-submodules", "origin"]
        if arguments[:4] != prefix or len(arguments) < 5:
            raise GitAnalysisError("GIT_COMMAND_REJECTED", "Only targeted origin fetch is allowed")
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    command = ["git", "-c", f"safe.directory={repository.resolve()}", "-c", "core.pager=cat", "-c", "core.quotepath=false",
               "-c", "pager.branch=false", "-c", "diff.external=",
               "--no-pager", *arguments]
    try:
        result = subprocess.run(command, cwd=repository, env=env, input=input_bytes, capture_output=True,
                                check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitAnalysisError("GIT_COMMAND_TIMEOUT", f"Git command exceeded {timeout}s",
                               recoverable=True, next_action="narrow-query") from exc
    if len(result.stdout) > max_output or len(result.stderr) > max_output:
        raise GitAnalysisError("GIT_OUTPUT_LIMIT", "Git output exceeded the configured limit",
                               recoverable=True, next_action="paginate-or-narrow-query")
    return result


def _text(result: subprocess.CompletedProcess[bytes], stream: str = "stdout") -> str:
    return getattr(result, stream).decode("utf-8", errors="replace")


def ensure_repository(repository: Path) -> dict[str, Any]:
    repository = repository.resolve()
    probe = run_git(repository, ["rev-parse", "--show-toplevel"])
    if probe.returncode:
        raise GitAnalysisError("GIT_REPOSITORY_INVALID", _text(probe, "stderr").strip() or "Not a Git repository")
    top = Path(_text(probe).strip()).resolve()
    if top != repository:
        repository = top
    common = run_git(repository, ["rev-parse", "--git-common-dir"])
    identity = hashlib.sha256((str(repository).casefold() + "\0" + _text(common).strip()).encode("utf-8")).hexdigest()
    shallow = run_git(repository, ["rev-parse", "--is-shallow-repository"])
    return {"path": str(repository), "identity": identity,
            "shallow": _text(shallow).strip().lower() == "true"}


def _candidate_refs(ref: str) -> list[str]:
    if ref.startswith("refs/") or SHA_PATTERN.fullmatch(ref):
        return [ref]
    return [f"refs/remotes/origin/{ref}", f"refs/heads/{ref}", f"refs/tags/{ref}", ref]


def discover_issue_refs(repository: Path, refs: Iterable[str], *, prefix: str = "") -> dict[str, Any]:
    """Read remote branch names once and resolve numeric issue identifiers."""
    requested = list(dict.fromkeys(validate_ref(item) for item in refs))
    if not any(item.isdecimal() for item in requested):
        return {"state": "READ", "resolved": {item: item for item in requested}, "matches": {}}
    result = run_git(repository, ["ls-remote", "--heads", "origin"], timeout=45)
    if result.returncode:
        stderr = _text(result, "stderr")[-2000:]
        lowered = stderr.casefold()
        if any(token in lowered for token in ("authentication", "permission denied", "could not read username")):
            code = "GIT_BRANCH_AUTH_REQUIRED"
        elif any(token in lowered for token in ("could not resolve", "unable to access", "connection", "network")):
            code = "GIT_BRANCH_NETWORK_ERROR"
        else:
            code = "GIT_BRANCH_DISCOVERY_FAILED"
        return {"state": "BLOCKED", "code": code, "stderr": stderr, "resolved": {}, "matches": {}}
    branches = [line.split("\t", 1)[1].removeprefix("refs/heads/")
                for line in _text(result).splitlines() if "\trefs/heads/" in line]
    matches = {item: select_issue_branch(item, branches, prefix) for item in requested}
    unresolved = {item: match for item, match in matches.items()
                  if match["state"] in {"AMBIGUOUS", "NOT_FOUND"}}
    return {"state": "NEEDS_INPUT" if unresolved else "READ",
            "code": "GIT_BRANCH_AMBIGUOUS" if any(m["state"] == "AMBIGUOUS" for m in unresolved.values())
                    else "GIT_BRANCH_NOT_FOUND" if unresolved else None,
            "resolved": {item: match["selected"] for item, match in matches.items() if match["selected"]},
            "matches": matches}


def resolve_ref(repository: Path, raw_ref: str, *, role: str = "source") -> dict[str, Any]:
    ref = validate_ref(raw_ref)
    candidates: list[dict[str, str]] = []
    for candidate in dict.fromkeys(_candidate_refs(ref)):
        symbolic = run_git(repository, ["rev-parse", "--symbolic-full-name", candidate])
        verified = run_git(repository, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
        if verified.returncode:
            continue
        commit = _text(verified).strip().splitlines()[0].lower()
        if not SHA_PATTERN.fullmatch(commit):
            continue
        full_ref = _text(symbolic).strip() if not symbolic.returncode else candidate
        if not full_ref:
            full_ref = candidate
        item = {"full_ref": full_ref, "commit": commit}
        if item not in candidates:
            candidates.append(item)
    selected = None
    if candidates:
        if role == "target":
            selected = next((item for item in candidates if item["full_ref"].startswith("refs/remotes/origin/")), candidates[0])
            reason = "remote_tracking_target_preferred" if selected["full_ref"].startswith("refs/remotes/") else "only_available_target"
        else:
            selected = next((item for item in candidates if item["full_ref"].startswith("refs/remotes/origin/")), candidates[0])
            reason = "remote_tracking_source_preferred" if selected["full_ref"].startswith("refs/remotes/") else "only_available_source"
        selected = {**selected, "selection_reason": reason}
    remote_exists = run_git(repository, ["remote", "get-url", "origin"]).returncode == 0
    freshness_state = "LOCAL_ONLY" if not remote_exists else "UNKNOWN"
    return {
        "requested_ref": ref,
        "selected": selected,
        "candidates": candidates,
        "freshness": {"state": freshness_state, "checked_at": utc_now(), "fetch_performed": False},
    }


def refresh_refs(repository: Path, *, remote: str, refs: Iterable[str]) -> dict[str, Any]:
    if remote != "origin" or not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
        raise GitAnalysisError("GIT_COMMAND_REJECTED", "Only the configured origin remote may be refreshed")
    material = list(dict.fromkeys(validate_ref(item) for item in refs))
    if not material or len(material) > 20:
        raise GitAnalysisError("GIT_COMMAND_REJECTED", "Refresh requires between 1 and 20 explicit refs")
    before = {item: resolve_ref(repository, item) for item in material}
    refspecs = []
    for item in material:
        short = item.removeprefix("refs/heads/").removeprefix(f"refs/remotes/{remote}/")
        if SHA_PATTERN.fullmatch(short):
            refspecs.append(short)
        else:
            refspecs.append(f"+refs/heads/{short}:refs/remotes/{remote}/{short}")
    started = dt.datetime.now(dt.timezone.utc)
    result = run_git(repository, ["fetch", "--no-tags", "--no-recurse-submodules", remote, *refspecs], timeout=120)
    stderr = _text(result, "stderr")
    if result.returncode:
        lowered = stderr.casefold()
        if any(token in lowered for token in ("authentication", "permission denied", "could not read username")):
            code, state, next_action = "GIT_FETCH_AUTH_REQUIRED", "AUTH_REQUIRED", "configure-git-credentials"
        elif any(token in lowered for token in ("could not resolve", "unable to access", "connection", "network")):
            code, state, next_action = "GIT_FETCH_NETWORK_ERROR", "NETWORK_UNAVAILABLE", "retry-when-network-available"
        else:
            code, state, next_action = "GIT_FETCH_FAILED", "BLOCKED", "inspect-remote-and-refspec"
        return {"state": state, "code": code, "before": before, "after": before,
                "refspecs": refspecs, "stderr": stderr[-4000:], "recoverable": True,
                "next_action": next_action, "repository_changed": False,
                "remote_tracking_refs_changed": False}
    after = {item: resolve_ref(repository, item) for item in material}
    states = {}
    changed = False
    for item in material:
        old = (before[item].get("selected") or {}).get("commit")
        new = (after[item].get("selected") or {}).get("commit")
        states[item] = "REF_NOT_FOUND" if not new else ("UPDATED" if old != new else "UNCHANGED")
        changed = changed or bool(new and old != new)
        after[item]["freshness"] = {"state": "FRESH", "checked_at": utc_now(), "fetch_performed": True}
    remote_url = run_git(repository, ["remote", "get-url", remote])
    return {"state": "UPDATED" if changed else "UNCHANGED", "before": before, "after": after,
            "refs": states, "refspecs": refspecs,
            "remote_identity": hashlib.sha256(_text(remote_url).strip().encode("utf-8")).hexdigest(),
            "duration_ms": int((dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000),
            "repository_changed": False, "remote_tracking_refs_changed": changed}


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    result = run_git(repository, ["merge-base", "--is-ancestor", ancestor, descendant])
    if result.returncode not in {0, 1}:
        message = _text(result, "stderr").strip()
        code = "GIT_UNRELATED_HISTORIES" if "no merge base" in message.casefold() else "GIT_COMMAND_FAILED"
        raise GitAnalysisError(code, message or "git merge-base failed")
    return result.returncode == 0


def commit_record(repository: Path, commit: str) -> dict[str, Any]:
    result = run_git(repository, ["show", "-s", "--format=%H%x00%P%x00%aI%x00%cI%x00%s", commit])
    if result.returncode:
        raise GitAnalysisError("GIT_REF_NOT_FOUND", _text(result, "stderr").strip() or "Commit unavailable")
    fields = _text(result).rstrip("\r\n").split("\x00", 4)
    return {"commit": fields[0].lower(), "parents": fields[1].split() if len(fields) > 1 else [],
            "author_date": fields[2] if len(fields) > 2 else "", "committer_date": fields[3] if len(fields) > 3 else "",
            "subject": fields[4] if len(fields) > 4 else ""}


def _first_parent_merges(repository: Path, target: str) -> list[dict[str, Any]]:
    result = run_git(repository, ["log", "--first-parent", "--merges", "--format=%H%x00%P%x00%s", target], timeout=90)
    if result.returncode:
        raise GitAnalysisError("GIT_COMMAND_FAILED", _text(result, "stderr").strip() or "Cannot read target history")
    records = []
    lines = [line for line in _text(result).splitlines() if line]
    total = len(lines)
    for index, line in enumerate(lines):
        fields = line.split("\x00", 2)
        parents = fields[1].split() if len(fields) > 1 else []
        records.append({"commit": fields[0].lower(), "parents": parents,
                        "subject": fields[2] if len(fields) > 2 else "", "target_position": total - index})
    return records


def _message_matches(subject: str, source_ref: str) -> bool:
    variants = {source_ref, source_ref.removeprefix("refs/heads/"), source_ref.rsplit("/", 1)[-1]}
    for variant in variants:
        if variant and re.search(rf"(?<![\w./-]){re.escape(variant)}(?![\w./-])", subject, re.IGNORECASE):
            return True
    return False


def _patch_id(repository: Path, commit: str) -> str | None:
    shown = run_git(repository, ["show", "--pretty=format:", "--no-ext-diff", "--no-textconv", commit], timeout=60)
    if shown.returncode or not shown.stdout:
        return None
    patch = run_git(repository, ["patch-id", "--stable"], input_bytes=shown.stdout, timeout=60)
    if patch.returncode or not _text(patch).strip():
        return None
    return _text(patch).split()[0]


def merge_search(repository: Path, source_refs: Iterable[str], target_ref: str, *,
                 freshness_known: bool | None = None, include_patch_evidence: bool = True,
                 pr_evidence: Mapping[str, list[Mapping[str, Any]]] | None = None) -> list[dict[str, Any]]:
    identity = ensure_repository(repository)
    target = resolve_ref(repository, target_ref, role="target")
    target_selected = target.get("selected")
    remote_present = run_git(repository, ["remote", "get-url", "origin"]).returncode == 0
    known = bool(freshness_known) if freshness_known is not None else not remote_present
    if target_selected and str(target_selected.get("full_ref", "")).startswith("refs/remotes/"):
        # A remote-tracking ref is usable evidence, but is not claimed fresh unless a refresh was recorded.
        known = bool(freshness_known)
    target_commit = str((target_selected or {}).get("commit", ""))
    merges = _first_parent_merges(repository, target_commit) if target_commit else []
    first_parent_history = set(_text(run_git(repository, ["rev-list", "--first-parent", target_commit])).splitlines()) if target_commit else set()
    ancestor_cache: dict[tuple[str, str], bool] = {}

    def is_ancestor(ancestor: str, descendant: str) -> bool:
        key = (ancestor, descendant)
        if key not in ancestor_cache:
            ancestor_cache[key] = _is_ancestor(repository, ancestor, descendant)
        return ancestor_cache[key]

    results = []
    for requested in dict.fromkeys(str(item) for item in source_refs):
        source = resolve_ref(repository, requested, role="source")
        selected = source.get("selected")
        source_tip = str((selected or {}).get("commit", ""))
        candidates: list[dict[str, Any]] = []
        integrated = None
        post_merge: list[str] = []
        fast_forward = False
        patch_coverage = 0.0
        for pr in (pr_evidence or {}).get(requested, []):
            sha = str(pr.get("merge_commit_sha", "")).lower()
            if SHA_PATTERN.fullmatch(sha):
                try:
                    record = commit_record(repository, sha)
                except GitAnalysisError:
                    record = {"commit": sha, "parents": [], "subject": str(pr.get("title", ""))}
                candidates.append({**record, "target_position": int(pr.get("target_position", 0)),
                                   "integrated_source_commit": pr.get("head_sha"),
                                   "evidence_level": "AUTHORITATIVE_PR", "introduces_commits": True,
                                   "pr_url": pr.get("url")})
        if source_tip and target_commit:
            source_history = run_git(repository, ["rev-list", source_tip])
            source_commits = [line.lower() for line in _text(source_history).splitlines() if SHA_PATTERN.fullmatch(line)]
            outstanding = run_git(repository, ["rev-list", source_tip, "--not", target_commit])
            outstanding_commits = set(_text(outstanding).splitlines())
            integrated = next((commit for commit in source_commits if commit not in outstanding_commits), None)
            if integrated:
                post_merge = source_commits[:source_commits.index(integrated)]
                fast_forward = source_tip == integrated and integrated in first_parent_history
                if not fast_forward and not candidates:
                    source_root = next((commit for commit in reversed(source_commits)
                                        if commit not in first_parent_history), integrated)
                    ancestry = run_git(repository, ["log", "--ancestry-path", "--first-parent", "--merges",
                                                    "--format=%H", f"{source_root}..{target_commit}"], timeout=90)
                    ancestry_merges = set(_text(ancestry).splitlines()) if not ancestry.returncode else set()
                    for merge in merges:
                        if merge["commit"] not in ancestry_merges:
                            continue
                        parents = merge["parents"]
                        if len(parents) < 2:
                            continue
                        introduced_result = run_git(repository, ["rev-list", *parents[1:], "--not", parents[0]])
                        introduced_set = set(_text(introduced_result).splitlines())
                        introduced = next((commit for commit in source_commits if commit in introduced_set), None)
                        if introduced:
                            candidates.append({**merge, "integrated_source_commit": introduced,
                                               "evidence_level": "EXACT_TOPOLOGY", "introduces_commits": True})
            if not candidates and not fast_forward:
                for merge in merges:
                    if not _message_matches(str(merge.get("subject", "")), requested):
                        continue
                    parent_match = bool(integrated and any(is_ancestor(integrated, parent)
                                                           for parent in merge.get("parents", [])[1:]))
                    candidates.append({**merge, "integrated_source_commit": integrated,
                                       "evidence_level": "EXACT_MESSAGE_AND_PARENT" if parent_match else "HEURISTIC_CANDIDATE",
                                       "introduces_commits": parent_match})
            if include_patch_evidence and not candidates and not fast_forward:
                merge_base = run_git(repository, ["merge-base", source_tip, target_commit])
                base = _text(merge_base).strip() if not merge_base.returncode else ""
                unique = run_git(repository, ["rev-list", "--reverse", f"{base}..{source_tip}"]) if base else None
                source_unique = [line for line in _text(unique).splitlines() if line] if unique else []
                cherry = run_git(repository, ["cherry", target_commit, source_tip], timeout=90)
                cherry_rows = [line.split(maxsplit=1) for line in _text(cherry).splitlines() if line[:1] in {"+", "-"}]
                matched = [fields[1] for fields in cherry_rows if fields[0] == "-" and len(fields) == 2]
                unmatched = [fields[1] for fields in cherry_rows if fields[0] == "+" and len(fields) == 2]
                denominator = len(matched) + len(unmatched)
                patch_coverage = len(matched) / denominator if denominator else 0.0
                if matched:
                    record = commit_record(repository, target_commit)
                    candidates.append({**record, "target_position": 0, "integrated_source_commit": source_tip,
                                       "evidence_level": "PATCH_EQUIVALENT", "introduces_commits": True,
                                       "patch_coverage": patch_coverage, "matched_commits": matched,
                                       "unmatched_commits": unmatched})
        else:
            for merge in merges:
                if _message_matches(str(merge.get("subject", "")), requested):
                    candidates.append({**merge, "integrated_source_commit": None,
                                       "evidence_level": "HEURISTIC_CANDIDATE", "introduces_commits": False})
        deduplicated: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            existing = deduplicated.get(candidate["commit"])
            ranks = {"EXACT_TOPOLOGY": 5, "AUTHORITATIVE_PR": 4, "EXACT_MESSAGE_AND_PARENT": 3,
                     "PATCH_EQUIVALENT": 2, "HEURISTIC_CANDIDATE": 1}
            if not existing or ranks.get(candidate["evidence_level"], 0) > ranks.get(existing["evidence_level"], 0):
                deduplicated[candidate["commit"]] = candidate
        candidates = sorted(deduplicated.values(), key=lambda item: int(item.get("target_position", 0)))
        final, ambiguous = select_final_merge(candidates)
        partial = bool(integrated and source_tip != integrated)
        status = integration_status(candidates=candidates, source_found=bool(selected), target_found=bool(target_selected),
                                    freshness_known=known, partial=partial, fast_forward=fast_forward,
                                    patch_coverage=patch_coverage, ambiguous=ambiguous)
        ranges: dict[str, str] = {}
        if final and final.get("parents"):
            first_parent = final["parents"][0]
            integrated_for_range = str(final.get("integrated_source_commit") or integrated or source_tip)
            merge_base = run_git(repository, ["merge-base", first_parent, integrated_for_range])
            base = _text(merge_base).strip() if not merge_base.returncode else first_parent
            ranges = {"merge_result_diff": f"{first_parent}..{final['commit']}",
                      "source_delta": f"{base}..{integrated_for_range}"}
        elif fast_forward and integrated:
            merge_base = run_git(repository, ["merge-base", f"{integrated}^", integrated])
            base = _text(merge_base).strip() if not merge_base.returncode else f"{integrated}^"
            ranges = {"source_delta": f"{base}..{integrated}"}
        limitations = []
        if identity["shallow"]:
            limitations.append({"code": "GIT_SHALLOW_HISTORY", "message": "History is shallow; negative evidence is not conclusive"})
            if status == "NO_INTEGRATION_EVIDENCE":
                status = "REFRESH_REQUIRED"
        if status == "REFRESH_REQUIRED":
            limitations.append({"code": "GIT_REF_STALE", "message": "Remote freshness is unknown; no negative conclusion was emitted"})
        evidenced_integrated = (
            str(final.get("integrated_source_commit"))
            if final and final.get("integrated_source_commit")
            else (integrated if fast_forward else None)
        )
        record = {
            "schema_version": 2,
            "source": {"requested": requested, "resolved_ref": (selected or {}).get("full_ref"), "current_tip": source_tip or None},
            "target": {"requested": target_ref, "resolved_ref": (target_selected or {}).get("full_ref"),
                       "commit": target_commit or None, "freshness": target["freshness"]},
            "git_ref": requested,
            "source_commit": source_tip or None,
            "target_ref": target_ref,
            "target_commit": target_commit or None,
            "integration_status": status,
            "merge_candidates": candidates,
            "final_merge": final,
            "integrated_source_commit": evidenced_integrated,
            "post_merge_commits": post_merge,
            "patch_coverage": patch_coverage,
            "ranges": ranges,
            "ambiguity": ambiguous,
            "limitations": limitations,
            "next_actions": next_actions_for(status, final),
            "repository_identity": identity["identity"],
        }
        results.append(legacy_view(record))
    return results


def _issue_references(subject: str) -> list[str]:
    patterns = [r"(?<![\w-])(?:G|ISS)[-/]?(\d+)(?!\w)", r"(?<!\w)#(\d+)(?!\w)"]
    values = []
    for pattern in patterns:
        values.extend(match.group(0) for match in re.finditer(pattern, subject, re.IGNORECASE))
    return list(dict.fromkeys(values))


OBJECT_TYPES = {
    "Documents": "Document", "Документы": "Document",
    "Catalogs": "Catalog", "Справочники": "Catalog",
    "InformationRegisters": "InformationRegister", "РегистрыСведений": "InformationRegister",
    "AccumulationRegisters": "AccumulationRegister", "РегистрыНакопления": "AccumulationRegister",
    "CommonModules": "CommonModule", "ОбщиеМодули": "CommonModule",
    "Reports": "Report", "Отчеты": "Report",
    "DataProcessors": "DataProcessor", "Обработки": "DataProcessor",
    "Roles": "Role", "Роли": "Role", "Subsystems": "Subsystem", "Подсистемы": "Subsystem",
}


def _object_identity(path: str) -> tuple[str, str]:
    parts = PurePosixPath(path).parts
    for index, part in enumerate(parts):
        if part in OBJECT_TYPES and index + 1 < len(parts):
            name = parts[index + 1]
            if part in {"CommonModules", "ОбщиеМодули", "Roles", "Роли", "Subsystems", "Подсистемы"}:
                name = PurePosixPath(name).stem
            return OBJECT_TYPES[part], name
    return "Unclassified", parts[0] if parts else ""


def _blob_bytes(repository: Path, commit: str, path: str) -> tuple[str | None, bytes]:
    entry = run_git(repository, ["ls-tree", commit, "--", path])
    line = _text(entry).strip()
    if not line or "\t" not in line:
        return None, b""
    metadata, _ = line.split("\t", 1)
    _mode, kind, blob = metadata.split()
    if kind != "blob":
        return None, b""
    shown = run_git(repository, ["show", f"{commit}:{path}"], max_output=8 * 1024 * 1024)
    return blob, shown.stdout if not shown.returncode else b""


def _bsl_routines(content: bytes) -> dict[str, tuple[str, str]]:
    text = content.decode("utf-8", errors="replace")
    declaration = re.compile(r"^\s*(Процедура|Функция|Procedure|Function)\s+([A-Za-zА-Яа-яЁё_][\wА-Яа-яЁё]*)", re.IGNORECASE)
    ending = re.compile(r"^\s*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.IGNORECASE)
    result: dict[str, tuple[str, str]] = {}
    current_name = None
    current_kind = None
    body: list[str] = []
    for line in text.splitlines():
        match = declaration.match(line)
        if match and current_name is None:
            current_kind = "procedure" if match.group(1).casefold() in {"процедура", "procedure"} else "function"
            current_name = match.group(2)
            body = [line]
            continue
        if current_name is not None:
            body.append(line)
            if ending.match(line):
                result[current_name] = (str(current_kind), hashlib.sha256("\n".join(body).encode("utf-8")).hexdigest())
                current_name = current_kind = None
                body = []
    return result


def object_change_set(repository: Path, base: str, commit: str) -> list[dict[str, Any]]:
    changed = run_git(repository, ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--name-status", base, commit])
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for line in _text(changed).splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status = fields[0]
        old_path = fields[1]
        new_path = fields[2] if len(fields) > 2 else old_path
        primary_path = new_path if not status.startswith("D") else old_path
        object_type, object_name = _object_identity(primary_path)
        key = object_type, object_name
        record = grouped.setdefault(key, {"object_type": object_type, "object_name": object_name,
            "change_kind": "MODIFIED", "files": [], "code_changes": {
                "procedures_added": [], "procedures_modified": [], "procedures_deleted": [],
                "functions_added": [], "functions_modified": [], "functions_deleted": []},
            "metadata_changes": [], "range": f"{base}..{commit}", "task_scope": {"state": "UNASSESSED", "evidence": []}})
        kind = "RENAMED" if status.startswith("R") else ({"A": "ADDED", "D": "DELETED"}.get(status[:1], "MODIFIED"))
        if record["change_kind"] == "MODIFIED" or kind == "RENAMED":
            record["change_kind"] = kind
        before_blob, before = _blob_bytes(repository, base, old_path)
        after_blob, after = _blob_bytes(repository, commit, new_path)
        record["files"].append({"path": new_path, "old_path": old_path if old_path != new_path else None,
                                "change_kind": kind, "blob_before": before_blob, "blob_after": after_blob})
        if PurePosixPath(primary_path).suffix.casefold() == ".bsl":
            before_routines, after_routines = _bsl_routines(before), _bsl_routines(after)
            for name in sorted(after_routines.keys() - before_routines.keys()):
                record["code_changes"][f"{after_routines[name][0]}s_added"].append(name)
            for name in sorted(before_routines.keys() - after_routines.keys()):
                record["code_changes"][f"{before_routines[name][0]}s_deleted"].append(name)
            for name in sorted(before_routines.keys() & after_routines.keys()):
                if before_routines[name][1] != after_routines[name][1]:
                    record["code_changes"][f"{after_routines[name][0]}s_modified"].append(name)
        elif PurePosixPath(primary_path).suffix.casefold() == ".xml":
            record["metadata_changes"].append({"path": primary_path, "change_kind": kind,
                                               "state": "STRUCTURAL_DIFF_REQUIRES_RLM_INTERPRETATION"})
    return list(grouped.values())


def branch_changes(repository: Path, source_ref: str, target_ref: str, *, freshness_known: bool | None = None) -> dict[str, Any]:
    integration = merge_search(repository, [source_ref], target_ref, freshness_known=freshness_known)[0]
    source_commit = integration.get("integrated_source_commit") or integration.get("source_commit")
    final = integration.get("final_merge") or {}
    if not source_commit:
        return {"state": integration["integration_status"], "commits": [], "integration": integration}
    if final.get("parents"):
        comparison = final["parents"][0]
        base_result = run_git(repository, ["merge-base", comparison, source_commit])
        baseline = _text(base_result).strip()
        reason = "merge_first_parent_merge_base"
    else:
        target_commit = integration.get("target_commit")
        base_result = run_git(repository, ["merge-base", target_commit, source_commit])
        baseline = _text(base_result).strip()
        reason = "comparison_target_merge_base"
    log = run_git(repository, ["log", "--reverse", "--format=%H%x00%P%x00%aI%x00%cI%x00%s", f"{baseline}..{source_commit}"])
    commits = []
    for line in _text(log).splitlines():
        fields = line.split("\x00", 4)
        if len(fields) < 5:
            continue
        stat = run_git(repository, ["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", fields[0]])
        commits.append({"commit": fields[0], "parents": fields[1].split(), "author_date": fields[2],
                        "committer_date": fields[3], "subject": fields[4],
                        "references": _issue_references(fields[4]), "files": _text(stat).splitlines()})
    object_base = final.get("parents", [baseline])[0] if final.get("parents") else baseline
    object_commit = str(final.get("commit") or source_commit)
    return {"state": "READ", "baseline": baseline, "baseline_reason": reason,
            "integrated_source_commit": source_commit, "commits": commits,
            "object_changes": object_change_set(repository, object_base, object_commit),
            "integration": integration}


def history_search(repository: Path, target_ref: str, *, subject_query: str = "", regex: bool = False,
                   merges_only: bool = False, first_parent: bool = False, min_parents: int | None = None,
                   max_parents: int | None = None, path: str = "", since: str = "", until: str = "",
                   cursor: str = "", max_count: int = 100) -> dict[str, Any]:
    target = resolve_ref(repository, target_ref, role="target")
    selected = target.get("selected")
    if not selected:
        return {"state": "TARGET_NOT_FOUND", "items": []}
    page_size = max(1, min(max_count, 500))
    material_path = safe_git_path(path) if path else ""
    binding = {
        "identity": ensure_repository(repository)["identity"],
        "target": selected["commit"],
        "action": "history-search",
        "subject_query": subject_query,
        "regex": regex,
        "merges_only": merges_only,
        "first_parent": first_parent,
        "min_parents": min_parents,
        "max_parents": max_parents,
        "path": material_path,
        "since": since,
        "until": until,
    }
    offset = _cursor_decode(cursor, binding) if cursor else 0
    arguments = ["log", f"--skip={offset}", f"--max-count={page_size + 1}",
                 "--format=%H%x00%P%x00%aI%x00%cI%x00%s"]
    if merges_only:
        arguments.append("--merges")
    if first_parent:
        arguments.append("--first-parent")
    if min_parents is not None:
        arguments.append(f"--min-parents={max(0, min_parents)}")
    if max_parents is not None:
        arguments.append(f"--max-parents={max(0, max_parents)}")
    if subject_query:
        arguments.append(f"--grep={subject_query if regex else re.escape(subject_query)}")
        arguments.append("--regexp-ignore-case")
    if since:
        arguments.append(f"--since={since}")
    if until:
        arguments.append(f"--until={until}")
    arguments.append(selected["commit"])
    if material_path:
        arguments.extend(["--", material_path])
    result = run_git(repository, arguments)
    if result.returncode:
        raise GitAnalysisError("GIT_COMMAND_FAILED", _text(result, "stderr").strip())
    items = []
    for line in _text(result).splitlines():
        fields = line.split("\x00", 4)
        if len(fields) == 5:
            items.append({"commit": fields[0], "parents": fields[1].split(), "author_date": fields[2],
                          "committer_date": fields[3], "subject": fields[4]})
    has_more = len(items) > page_size
    items = items[:page_size]
    next_cursor = _cursor_encode({**binding, "offset": offset + page_size}) if has_more else None
    return {"state": "READ", "target": target, "items": items,
            "returned_items": len(items), "next_cursor": next_cursor,
            "truncated": has_more, "truncated_reason": "max_count" if has_more else None}


def _cursor_encode(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(b"flow1c-git-cursor-v1\0" + body).hexdigest().encode("ascii")
    return base64.urlsafe_b64encode(body + b"." + digest).decode("ascii").rstrip("=")


def _cursor_payload(cursor: str, expected: Mapping[str, Any]) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        body, digest = raw.rsplit(b".", 1)
        if hashlib.sha256(b"flow1c-git-cursor-v1\0" + body).hexdigest().encode("ascii") != digest:
            raise ValueError("digest")
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise GitAnalysisError("GIT_CURSOR_INVALID", "Pagination cursor is invalid", recoverable=True,
                               next_action="restart-pagination") from exc
    for key, value in expected.items():
        if payload.get(key) != value:
            raise GitAnalysisError("GIT_CURSOR_MISMATCH", "Pagination cursor belongs to different parameters",
                                   recoverable=True, next_action="restart-pagination")
    return payload


def _cursor_decode(cursor: str, expected: Mapping[str, Any]) -> int:
    return int(_cursor_payload(cursor, expected).get("offset", 0))


def diff(repository: Path, commit_ref: str, *, base_ref: str | None = None, detail: str = "patch",
         paths: Iterable[str] = (), cursor: str = "", max_files: int = 100, max_chars: int = 40000) -> dict[str, Any]:
    commit_resolved = resolve_ref(repository, commit_ref)
    selected = commit_resolved.get("selected")
    if not selected:
        return {"state": "NOT_FOUND", "git_ref": commit_ref}
    commit = selected["commit"]
    record = commit_record(repository, commit)
    base = None
    if base_ref:
        base_selected = resolve_ref(repository, base_ref).get("selected")
        base = (base_selected or {}).get("commit")
    elif record["parents"]:
        base = record["parents"][0]
    if not base:
        raise GitAnalysisError("GIT_REF_NOT_FOUND", "A diff base is required")
    material_paths = [safe_git_path(item) for item in paths]
    names_result = run_git(repository, ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--find-copies",
                                          "--name-status", base, commit, *( ["--", *material_paths] if material_paths else [])])
    if names_result.returncode:
        raise GitAnalysisError("GIT_COMMAND_FAILED", _text(names_result, "stderr").strip())
    files = _text(names_result).splitlines()
    identity = ensure_repository(repository)["identity"]
    binding = {"identity": identity, "base": base, "commit": commit, "detail": detail, "paths": material_paths}
    position = _cursor_payload(cursor, binding) if cursor else {}
    offset = int(position.get("offset", 0))
    char_offset = int(position.get("char_offset", 0))
    page_size = max(1, min(max_files, 500))
    if "page_size" in position and position["page_size"] != page_size:
        raise GitAnalysisError("GIT_CURSOR_MISMATCH", "Pagination cursor belongs to a different page size",
                               recoverable=True, next_action="restart-pagination")
    if offset < 0 or offset >= max(1, len(files)) or char_offset < 0:
        raise GitAnalysisError("GIT_CURSOR_INVALID", "Pagination cursor position is invalid",
                               recoverable=True, next_action="restart-pagination")
    page = files[offset:offset + page_size]
    content = ""
    if detail == "names":
        content = "\n".join(page)
    elif detail == "stat":
        stat = run_git(repository, ["diff", "--no-ext-diff", "--no-textconv", "--stat", base, commit,
                                    *( ["--", *[entry.split("\t")[-1] for entry in page]] if page else [])])
        content = _text(stat)
    else:
        selected_paths = []
        for entry in page:
            columns = entry.split("\t")
            selected_paths.append(columns[-1])
        if detail in {"remerge-diff", "combined"}:
            mode = "--remerge-diff" if detail == "remerge-diff" else "--cc"
            patch_args = ["show", "--format=", "--no-ext-diff", "--no-textconv", mode, "--unified=3", commit]
        else:
            patch_args = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--find-copies",
                          "--unified=3", base, commit]
        patch = run_git(repository, [*patch_args, *( ["--", *selected_paths] if selected_paths else [])], timeout=90)
        content = _text(patch)
    if char_offset > len(content):
        raise GitAnalysisError("GIT_CURSOR_INVALID", "Pagination cursor exceeds the selected diff",
                               recoverable=True, next_action="restart-pagination")
    limit = max(1, min(max_chars, 20_000))
    total_chars = len(content)
    content = content[char_offset:char_offset + limit]
    next_char_offset = char_offset + len(content)
    truncated_chars = next_char_offset < total_chars
    next_offset = offset + len(page)
    next_cursor = None
    if truncated_chars:
        next_cursor = _cursor_encode({**binding, "page_size": page_size,
                                      "offset": offset, "char_offset": next_char_offset})
    elif next_offset < len(files):
        next_cursor = _cursor_encode({**binding, "page_size": page_size, "offset": next_offset})
    reason = "max_chars" if truncated_chars else ("max_files" if next_cursor else None)
    return {"state": "READ", "git_ref": commit_ref, "commit": commit, "base_commit": base,
            "detail": detail, "files": files, "total_files": len(files), "returned_files": len(page),
            "page_files": page, "content": content,
            "diff": content if detail not in {"names", "stat"} else None,
            "page_char_offset": char_offset,
            "page_total_chars": total_chars, "effective_max_chars": limit,
            "next_cursor": next_cursor, "truncated": bool(reason), "truncated_reason": reason,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


def safe_git_path(raw_path: str) -> str:
    value = str(raw_path or "").replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", "..", ".git"} for part in path.parts):
        raise GitAnalysisError("GIT_PATH_REJECTED", "Git path must be a contained relative path",
                               details={"path": value})
    return str(path)


def read_at_ref(repository: Path, git_ref: str, path: str, *, start_line: int = 1,
                max_chars: int = 24000) -> dict[str, Any]:
    relative = safe_git_path(path)
    if PurePosixPath(relative).suffix.casefold() not in READABLE_EXTENSIONS:
        raise GitAnalysisError("GIT_PATH_REJECTED", "File extension is not allowed", details={"path": relative})
    selected = resolve_ref(repository, git_ref).get("selected")
    if not selected:
        return {"state": "NOT_FOUND", "git_ref": git_ref, "path": relative}
    commit = selected["commit"]
    entry = run_git(repository, ["ls-tree", commit, "--", relative])
    line = _text(entry).strip()
    if not line:
        return {"state": "NOT_FOUND", "commit": commit, "path": relative}
    metadata, _name = line.split("\t", 1)
    mode, kind, blob = metadata.split()
    if kind != "blob" or mode == "160000":
        raise GitAnalysisError("GIT_PATH_REJECTED", "Submodules and non-blob entries cannot be read")
    shown = run_git(repository, ["show", f"{commit}:{relative}"], max_output=2 * 1024 * 1024)
    if shown.returncode:
        raise GitAnalysisError("GIT_REF_NOT_FOUND", _text(shown, "stderr").strip())
    full = shown.stdout.decode("utf-8", errors="replace")
    lines = full.splitlines(keepends=True)
    returned = "".join(lines[max(0, start_line - 1):])[:max(1, min(max_chars, 1_000_000))]
    return {"state": "READ", "commit": commit, "path": relative, "blob": blob,
            "mode": mode, "start_line": max(1, start_line), "content": returned,
            "truncated": len(returned) < len("".join(lines[max(0, start_line - 1):])),
            "blob_sha256": hashlib.sha256(shown.stdout).hexdigest(),
            "returned_sha256": hashlib.sha256(returned.encode("utf-8")).hexdigest(),
            "repository_identity": ensure_repository(repository)["identity"]}


def create_snapshot(repository: Path, workspace_root: Path, request_id: str, git_ref: str,
                    paths: Iterable[str] = ()) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9-]{8,64}", request_id, re.IGNORECASE):
        raise GitAnalysisError("GIT_PATH_REJECTED", "Invalid request id")
    selected = resolve_ref(repository, git_ref).get("selected")
    if not selected:
        return {"state": "NOT_FOUND", "git_ref": git_ref}
    commit = selected["commit"]
    tree_result = run_git(repository, ["rev-parse", f"{commit}^{{tree}}"])
    tree = _text(tree_result).strip()
    requested = [safe_git_path(item) for item in paths]
    snapshot_base = (workspace_root / "git-analysis" / request_id / commit).resolve()
    allowed_root = (workspace_root / "git-analysis").resolve()
    if allowed_root not in snapshot_base.parents:
        raise GitAnalysisError("GIT_PATH_REJECTED", "Snapshot escaped the managed workspace")
    manifest_path = snapshot_base / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("tree") == tree and manifest.get("requested_paths") == requested:
                return {"state": "REUSED", **manifest, "manifest_path": str(manifest_path)}
        except (OSError, json.JSONDecodeError):
            pass
    snapshot_base.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{commit}.", dir=snapshot_base.parent))
    source = temporary / "source"
    source.mkdir()
    try:
        args = ["ls-tree", "-r", "-z", commit]
        if requested:
            args.extend(["--", *requested])
        listed = run_git(repository, args)
        if listed.returncode:
            raise GitAnalysisError("GIT_COMMAND_FAILED", _text(listed, "stderr").strip())
        content_hash = hashlib.sha256()
        count = 0
        for raw in listed.stdout.split(b"\x00"):
            if not raw:
                continue
            metadata, raw_name = raw.split(b"\t", 1)
            mode, kind, blob = metadata.decode("ascii").split()
            name = raw_name.decode("utf-8", errors="strict")
            relative = safe_git_path(name)
            if kind != "blob" or mode == "160000":
                continue
            output = run_git(repository, ["show", f"{commit}:{relative}"], max_output=8 * 1024 * 1024)
            if output.returncode:
                raise GitAnalysisError("GIT_COMMAND_FAILED", f"Cannot export {relative}")
            target = source.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(output.stdout)
            content_hash.update(relative.encode("utf-8") + b"\0" + blob.encode("ascii") + b"\0")
            count += 1
        manifest = {"schema_version": 1,
                    "snapshot_id": semantic_fingerprint({"repository": ensure_repository(repository)["identity"],
                                                         "tree": tree, "paths": requested})[:32],
                    "repository_identity": ensure_repository(repository)["identity"], "commit": commit,
                    "tree": tree, "requested_paths": requested, "created_at": utc_now(),
                    "content_digest": content_hash.hexdigest(), "file_count": count,
                    "source_path": str(snapshot_base / "source")}
        (temporary / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if snapshot_base.exists():
            shutil.rmtree(snapshot_base)
        os.replace(temporary, snapshot_base)
        return {"state": "CREATED", **manifest, "manifest_path": str(manifest_path)}
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def cleanup_snapshots(workspace_root: Path, *, ttl_hours: int = 168) -> dict[str, Any]:
    root = (workspace_root / "git-analysis").resolve()
    if not root.exists():
        return {"state": "CLEAN", "removed": []}
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - max(1, ttl_hours) * 3600
    removed = []
    for request_dir in root.iterdir():
        if not request_dir.is_dir() or request_dir.is_symlink():
            continue
        for snapshot in request_dir.iterdir():
            resolved = snapshot.resolve()
            if root not in resolved.parents or snapshot.is_symlink() or not snapshot.is_dir():
                continue
            if snapshot.stat().st_mtime < cutoff:
                shutil.rmtree(snapshot)
                removed.append(str(snapshot.relative_to(root)))
    return {"state": "CLEANED", "removed": removed}
