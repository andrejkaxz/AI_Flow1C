"""Read-only Git analysis and snapshot evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import storage as storage
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state
from scripts import flow1c_git as git_analysis
from scripts import flow1c_git_policy as git_policy
from scripts import gitea_client as ext_scripts_gitea_client


def git_inspection_repository(source: str, *, product_root: Path) -> Path:
    if source == "workflow":
        repository = product_root.resolve()
    else:
        local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
        raw = str(local.get("extension_path", "") or "").strip()
        if not raw:
            raise WorkflowError("extension_path is not configured")
        repository = Path(raw).resolve()
    if not repository.is_dir():
        raise WorkflowError(f"Git repository is unavailable: {repository}")
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repository,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=15,
    )
    if probe.returncode or probe.stdout.strip() != "true":
        raise WorkflowError(f"Path is not a Git repository: {repository}")
    return repository


def _git_payload_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _git_analysis_state(gate: dict[str, Any]) -> dict[str, Any]:
    state = gate.get("git_analysis_state")
    if not isinstance(state, dict):
        state = {
            "schema_version": 1,
            "cache": {},
            "terminal": {},
            "history_search_without_evidence": 0,
            "snapshots": {},
        }
        gate["git_analysis_state"] = state
    state.setdefault("cache", {})
    state.setdefault("terminal", {})
    state.setdefault("snapshots", {})
    return state


def _record_git_analysis(
    gate: dict[str, Any],
    *,
    action: str,
    fingerprint: str,
    parameters: dict[str, Any],
    payload: dict[str, Any],
    product_root: Path,
) -> dict[str, Any]:
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    digest = _git_payload_digest(payload)
    evidence_id = f"GIT-{len(evidence.get('git_analysis', [])) + 1:03d}"
    record = {
        "schema_version": 2,
        "evidence_id": evidence_id,
        "action": action,
        "fingerprint": fingerprint,
        "parameters": parameters,
        "output_sha256": digest,
        "result": payload,
        "created_at": storage.utc_now(),
    }
    evidence.setdefault("git_analysis", []).append(record)
    legacy_ref = parameters.get("git_ref") or (parameters.get("git_refs") or [None])[0]
    evidence.setdefault("git_reads", []).append(
        {
            "action": parameters.get("requested_action", action),
            "git_ref": legacy_ref,
            "git_refs": parameters.get("git_refs", []),
            "target_ref": parameters.get("target_ref"),
            "commit": payload.get("commit"),
            "base_commit": payload.get("base_commit"),
            "sha256": digest,
            "evidence_id": evidence_id,
            "created_at": storage.utc_now(),
        }
    )
    storage.write_json(evidence_path, evidence)
    state = _git_analysis_state(gate)
    state["cache"][fingerprint] = {"evidence_id": evidence_id, "result": payload}
    if action == "merge-search":
        state["terminal"]["merge-search"] = {
            "fingerprint": fingerprint,
            "evidence_id": evidence_id,
            "refs": parameters.get("git_refs", []),
        }
    if action == "history-search":
        state["history_search_without_evidence"] = (
            int(state.get("history_search_without_evidence", 0)) + 1
        )
    gate_state.save_request(gate, product_root=product_root)
    return {**payload, "evidence_id": evidence_id, "sha256": digest, "cache_hit": False}


def agent_git_refresh(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_git_refresh", product_root=product_root)
    if gate.get("mode") not in {"explore", "draft"}:
        raise WorkflowError("Git refresh is available only in explore or draft mode")
    context = gate.get("git_context", {}) if isinstance(gate.get("git_context"), dict) else {}
    if not context.get("refresh_requested"):
        raise WorkflowError("Git refresh requires the user's saved refresh_git_refs intent")
    repository = git_inspection_repository(str(args.repository), product_root=product_root)
    refs = list(getattr(args, "refs", None) or context.get("source_refs", []))
    target = str(context.get("target_ref", "") or "").strip()
    prefix = (
        str(
            storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {}).get(
                "extension_branch_prefix", ""
            )
            or ""
        ).strip()
        if args.repository == "extension"
        else ""
    )
    try:
        discovery = git_analysis.discover_issue_refs(repository, refs, prefix=prefix)
        if discovery["state"] != "READ":
            payload = {
                **discovery,
                "recoverable": True,
                "repository_changed": False,
                "next_action": "check-origin-or-select-full-branch",
            }
        else:
            resolved = discovery["resolved"]
            refresh_material = list(
                dict.fromkeys([*resolved.values(), *([target] if target else [])])
            )
            payload = git_analysis.refresh_refs(
                repository, remote=str(args.remote), refs=refresh_material
            )
            payload["branch_discovery"] = discovery
            if payload.get("state") in {"UPDATED", "UNCHANGED"}:
                context["resolved_refs"] = resolved
    except git_analysis.GitAnalysisError as exc:
        _value = {"schema_version": 2, "state": "BLOCKED", "errors": [exc.payload]}
        return OperationResult(_value, 2)
    context["freshness"] = (
        "FRESH" if payload.get("state") in {"UPDATED", "UNCHANGED"} else payload.get("state")
    )
    context["refresh_result_sha256"] = _git_payload_digest(payload)
    gate["git_context"] = context
    fingerprint = git_policy.semantic_fingerprint(
        {
            "repository": args.repository,
            "action": "git-refresh",
            "remote": args.remote,
            "refs": refs,
        }
    )
    result = _record_git_analysis(
        gate,
        action="git-refresh",
        fingerprint=fingerprint,
        parameters={"repository": args.repository, "remote": args.remote, "refs": refs},
        payload=payload,
        product_root=product_root,
    )
    _value = result
    return OperationResult(_value, 0 if payload.get("state") in {"UPDATED", "UNCHANGED"} else 1)


def _gitea_pr_evidence(
    repository: Path,
    source_refs: list[str],
    target_ref: str,
    *,
    allow_targeted_fetch: bool,
    product_root: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    config, _local = runtime.load_config(product_root=product_root)
    settings = config.get("gitea", {}) if isinstance(config.get("gitea"), dict) else {}
    token_env = str(settings.get("token_env", "") or "")
    if not token_env or not os.environ.get(token_env):
        return ({}, [])
    required = [
        str(settings.get(key, "") or "").strip() for key in ("base_url", "owner", "repository")
    ]
    if not all(required):
        return (
            {},
            [
                {
                    "code": "GITEA_CONFIG_INCOMPLETE",
                    "recoverable": True,
                    "next_action": "continue-with-git-evidence",
                }
            ],
        )
    client = ext_scripts_gitea_client.GiteaClient(
        required[0], required[1], required[2], token_env=token_env
    )
    records: dict[str, list[dict[str, Any]]] = {}
    limitations = []
    target_short = target_ref.rsplit("/", 1)[-1]
    for source in source_refs:
        try:
            response = client.merged_pulls(source, target_short)
        except ext_scripts_gitea_client.GiteaError as exc:
            limitations.append(exc.as_dict())
            continue
        records[source] = response["records"]
        for item in records[source]:
            merge_sha = str(item.get("merge_commit_sha", ""))
            if (
                allow_targeted_fetch
                and merge_sha
                and (not git_analysis.resolve_ref(repository, merge_sha).get("selected"))
            ):
                fetched = git_analysis.refresh_refs(repository, remote="origin", refs=[merge_sha])
                if fetched.get("state") not in {"UPDATED", "UNCHANGED"}:
                    limitations.append(
                        {
                            "code": fetched.get("code", "GIT_FETCH_FAILED"),
                            "recoverable": True,
                            "next_action": fetched.get("next_action"),
                        }
                    )
    return (records, limitations)


def agent_git_inspect_v2(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_git_inspect", product_root=product_root)
    if gate.get("mode") not in {"explore", "draft"}:
        raise WorkflowError("Git inspection is available only in explore or draft mode")
    repository = git_inspection_repository(str(args.repository), product_root=product_root)
    requested_action = str(args.action)
    action = git_policy.canonical_action(requested_action)
    raw_refs = list(
        dict.fromkeys(
            (
                str(item).strip()
                for item in getattr(args, "git_refs", None) or []
                if str(item).strip()
            )
        )
    )
    raw_ref = str(getattr(args, "git_ref", "") or gate.get("git_ref") or "").strip()
    if not raw_refs and raw_ref:
        raw_refs = [raw_ref]
    context = gate.get("git_context", {}) if isinstance(gate.get("git_context"), dict) else {}
    resolved_refs = (
        context.get("resolved_refs", {}) if isinstance(context.get("resolved_refs"), dict) else {}
    )
    raw_refs = [str(resolved_refs.get(item, item)) for item in raw_refs]
    raw_ref = str(resolved_refs.get(raw_ref, raw_ref))
    target_ref = str(
        getattr(args, "target_ref", "")
        or context.get("target_ref")
        or runtime.load_config(product_root=product_root)[0]
        .get("project", {})
        .get("default_branch", "main")
    ).strip()
    parameters = {
        "repository": str(args.repository),
        "action": action,
        "git_ref": raw_ref,
        "git_refs": raw_refs,
        "target_ref": target_ref,
        "base_ref": str(getattr(args, "base_ref", "") or ""),
        "detail": str(getattr(args, "detail", "patch") or "patch"),
        "paths": list(getattr(args, "paths", None) or []),
        "path": str(getattr(args, "path", "") or ""),
        "start_line": int(getattr(args, "start_line", 1)),
        "max_files": int(getattr(args, "max_files", 100)),
        "output_max_chars": int(args.max_chars),
        "subject_query": str(getattr(args, "subject_query", "") or ""),
        "regex": bool(getattr(args, "regex", False)),
        "merges_only": bool(getattr(args, "merges_only", False)),
        "first_parent": bool(getattr(args, "first_parent", False)),
        "min_parents": getattr(args, "min_parents", None),
        "max_parents": getattr(args, "max_parents", None),
        "since": str(getattr(args, "since", "") or ""),
        "until": str(getattr(args, "until", "") or ""),
        "max_count": int(args.max_count),
        "include_pr_evidence": bool(getattr(args, "include_pr_evidence", True)),
        "include_patch_evidence": bool(getattr(args, "include_patch_evidence", True)),
        "cursor": str(getattr(args, "cursor", "") or ""),
    }
    fingerprint = git_policy.semantic_fingerprint(parameters)
    parameters["requested_action"] = requested_action
    state = _git_analysis_state(gate)
    try:
        transition = git_policy.validate_transition(state, action, fingerprint)
    except ValueError as exc:
        code, _, message = str(exc).partition(":")
        _value = {
            "schema_version": 2,
            "state": code,
            "code": code,
            "message": message.strip(),
            "next_actions": ["use-saved-evidence", "complete"],
        }
        return OperationResult(_value, 0 if code == "ALREADY_RESOLVED" else 2)
    if transition == "CACHE_HIT":
        cached = state["cache"][fingerprint]
        _value = {**cached["result"], "evidence_id": cached["evidence_id"], "cache_hit": True}
        return OperationResult(_value, 0)
    freshness_known = context.get("freshness") == "FRESH"
    try:
        if action == "resolve":
            if not raw_ref:
                raise WorkflowError("git_ref is required")
            resolution = git_analysis.resolve_ref(
                repository, raw_ref, role="target" if raw_ref == target_ref else "source"
            )
            selected = resolution.get("selected")
            payload = {"state": "READ" if selected else "NOT_FOUND", "action": action, **resolution}
            if selected:
                record = git_analysis.commit_record(repository, selected["commit"])
                payload.update(
                    commit=selected["commit"],
                    parents=record["parents"],
                    subject=record["subject"],
                    is_merge=len(record["parents"]) > 1,
                    git_ref=raw_ref,
                    repository=str(repository),
                )
        elif action == "merge-search":
            if not raw_refs:
                raise WorkflowError("git_ref or git_refs is required")
            if len(raw_refs) > 20:
                raise WorkflowError("At most 20 git_refs may be inspected at once")
            known = (
                freshness_known
                or git_analysis.run_git(repository, ["remote", "get-url", "origin"]).returncode != 0
            )
            integrations = git_analysis.merge_search(
                repository,
                raw_refs,
                target_ref,
                freshness_known=known,
                include_patch_evidence=False,
            )
            unresolved = [
                item["git_ref"]
                for item in integrations
                if item["integration_status"] != "FAST_FORWARD"
                and (
                    not any(
                        (
                            candidate["evidence_level"] == "EXACT_TOPOLOGY"
                            for candidate in item["merge_candidates"]
                        )
                    )
                )
            ]
            pr_evidence: dict[str, list[dict[str, Any]]] = {}
            pr_limitations: list[dict[str, Any]] = []
            if unresolved and bool(getattr(args, "include_pr_evidence", True)):
                pr_evidence, pr_limitations = _gitea_pr_evidence(
                    repository,
                    unresolved,
                    target_ref,
                    allow_targeted_fetch=bool(context.get("refresh_requested")),
                    product_root=product_root,
                )
            if unresolved:
                fallback = git_analysis.merge_search(
                    repository,
                    unresolved,
                    target_ref,
                    freshness_known=known,
                    include_patch_evidence=bool(getattr(args, "include_patch_evidence", True)),
                    pr_evidence=pr_evidence,
                )
                replacements = {item["git_ref"]: item for item in fallback}
                integrations = [replacements.get(item["git_ref"], item) for item in integrations]
            if requested_action == "integration":
                legacy_statuses = {
                    "FAST_FORWARD": "NO_MERGE_COMMIT",
                    "NO_INTEGRATION_EVIDENCE": "NOT_MERGED",
                }
                for integration in integrations:
                    integration["legacy_status"] = legacy_statuses.get(
                        integration["integration_status"], integration["integration_status"]
                    )
                    integration["status"] = integration["legacy_status"]
            if pr_limitations:
                for integration in integrations:
                    integration.setdefault("limitations", []).extend(pr_limitations)
            payload = {
                "state": "READ",
                "action": requested_action,
                "canonical_action": action,
                "target_ref": target_ref,
                "repository": str(repository),
                "integrations": integrations,
                "next_actions": list(
                    dict.fromkeys(
                        (item for result in integrations for item in result["next_actions"])
                    )
                ),
            }
        elif action == "branch-changes":
            if not raw_ref:
                raise WorkflowError("git_ref is required")
            payload = git_analysis.branch_changes(
                repository,
                raw_ref,
                target_ref,
                freshness_known=freshness_known
                or git_analysis.run_git(repository, ["remote", "get-url", "origin"]).returncode
                != 0,
            )
            payload["action"] = action
        elif action == "history-search":
            payload = git_analysis.history_search(
                repository,
                target_ref,
                subject_query=str(getattr(args, "subject_query", "") or ""),
                regex=bool(getattr(args, "regex", False)),
                merges_only=bool(getattr(args, "merges_only", False)),
                first_parent=bool(getattr(args, "first_parent", False)),
                min_parents=getattr(args, "min_parents", None),
                max_parents=getattr(args, "max_parents", None),
                path=str(getattr(args, "path", "") or ""),
                since=str(getattr(args, "since", "") or ""),
                until=str(getattr(args, "until", "") or ""),
                cursor=str(getattr(args, "cursor", "") or ""),
                max_count=int(args.max_count),
            )
            payload["action"] = action
        elif action == "read-at-ref":
            if not raw_ref or not getattr(args, "path", None):
                raise WorkflowError("git_ref and path are required")
            payload = git_analysis.read_at_ref(
                repository,
                raw_ref,
                str(args.path),
                start_line=int(getattr(args, "start_line", 1)),
                max_chars=int(args.max_chars),
            )
            payload["action"] = action
        elif action == "diff":
            if not raw_ref:
                raise WorkflowError("git_ref is required")
            payload = git_analysis.diff(
                repository,
                raw_ref,
                base_ref=parameters["base_ref"] or None,
                detail=parameters["detail"],
                paths=parameters["paths"],
                cursor=parameters["cursor"],
                max_files=int(getattr(args, "max_files", 100)),
                max_chars=int(args.max_chars),
            )
            payload["action"] = action
        elif action in {"log", "latest-merge"}:
            resolved = git_analysis.resolve_ref(repository, raw_ref)
            selected = resolved.get("selected")
            if not selected:
                payload = {"state": "NOT_FOUND", "git_ref": raw_ref, "action": action}
            else:
                git_args = [
                    "log",
                    f"--max-count={(1 if action == 'latest-merge' else min(int(args.max_count), 100))}",
                    "--format=%H%x00%P%x00%s",
                ]
                if action == "latest-merge":
                    git_args.append("--merges")
                git_args.append(selected["commit"])
                result = git_analysis.run_git(repository, git_args)
                content = result.stdout.decode("utf-8", errors="replace")[: int(args.max_chars)]
                payload = {
                    "state": "READ",
                    "action": action,
                    "git_ref": raw_ref,
                    "commit": selected["commit"],
                    "merge" if action == "latest-merge" else "log": content or None,
                }
        else:
            raise WorkflowError(f"Unsupported Git action: {action}")
    except git_analysis.GitAnalysisError as exc:
        _value = {"schema_version": 2, "state": "BLOCKED", "errors": [exc.payload]}
        return OperationResult(_value, 2)
    result = _record_git_analysis(
        gate,
        action=action,
        fingerprint=fingerprint,
        parameters=parameters,
        payload=payload,
        product_root=product_root,
    )
    _value = result
    return OperationResult(_value, 0)


def agent_git_snapshot(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_git_snapshot", product_root=product_root)
    if gate.get("mode") not in {"explore", "draft"}:
        raise WorkflowError("Git snapshots are available only in explore or draft mode")
    snapshot_action = str(getattr(args, "action", "create") or "create")
    if snapshot_action == "cleanup":
        payload = git_analysis.cleanup_snapshots(
            product_root / ".workspace", ttl_hours=int(getattr(args, "ttl_hours", 168))
        )
        fingerprint = git_policy.semantic_fingerprint(
            {"action": "snapshot-cleanup", "ttl_hours": int(getattr(args, "ttl_hours", 168))}
        )
        result = _record_git_analysis(
            gate,
            action="snapshot-cleanup",
            fingerprint=fingerprint,
            parameters={"action": "cleanup", "ttl_hours": int(getattr(args, "ttl_hours", 168))},
            payload=payload,
            product_root=product_root,
        )
        _value = result
        return OperationResult(_value, 0)
    repository = git_inspection_repository(str(args.repository), product_root=product_root)
    raw_ref = str(args.git_ref or "").strip()
    paths = list(getattr(args, "paths", None) or [])
    fingerprint = git_policy.semantic_fingerprint(
        {"repository": args.repository, "action": "snapshot", "git_ref": raw_ref, "paths": paths}
    )
    state = _git_analysis_state(gate)
    if fingerprint in state["cache"]:
        cached = state["cache"][fingerprint]
        _value = {**cached["result"], "evidence_id": cached["evidence_id"], "cache_hit": True}
        return OperationResult(_value, 0)
    try:
        payload = git_analysis.create_snapshot(
            repository, product_root / ".workspace", str(gate["request_id"]), raw_ref, paths
        )
    except git_analysis.GitAnalysisError as exc:
        _value = {"schema_version": 2, "state": "BLOCKED", "errors": [exc.payload]}
        return OperationResult(_value, 2)
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    evidence.setdefault("snapshots", []).append({**payload, "created_at": storage.utc_now()})
    storage.write_json(evidence_path, evidence)
    result = _record_git_analysis(
        gate,
        action="snapshot",
        fingerprint=fingerprint,
        parameters={"repository": args.repository, "git_ref": raw_ref, "paths": paths},
        payload=payload,
        product_root=product_root,
    )
    state = _git_analysis_state(gate)
    state["snapshots"][payload["snapshot_id"]] = {
        "commit": payload["commit"],
        "path": payload["source_path"],
    }
    gate_state.save_request(gate, product_root=product_root)
    _value = result
    return OperationResult(_value, 0)
