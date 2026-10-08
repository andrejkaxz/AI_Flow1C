"""Gated project navigation, wiki authoring and exact documentation publication."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from flow1c import context, documentation, git_runtime, knowledge, navigation, publication, storage
from flow1c import knowledge_policy as policy
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state
from scripts import flow1c_git as git_analysis

READ_ACTIONS = {"navigation", "search", "read", "preview"}
WRITE_ACTIONS = {"refresh", "write", "commit", "pr"}
FIELDS = {
    "navigation": {"query", "cursor", "max_chars"},
    "search": {"source", "ref", "snapshot", "query", "max_chars"},
    "read": {"source", "ref", "snapshot", "path", "version", "section", "cursor", "start_line", "max_chars"},
    "preview": {"path", "content"},
    "write": {"path", "content", "expected_version", "confirmed"},
    "refresh": {"confirmed"},
    "commit": {"paths", "message", "branch", "confirmed"},
    "pr": {"title", "body", "confirmed"},
}


def _navigation(request: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    limit = policy.response_limit(request.get("max_chars", 8000))
    value = navigation.inventory(product_root=product_root)
    version = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    query = request.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        raise WorkflowError("Navigation query must be at most 200 characters")
    items = [item for item in [*value["tasks"], *value["items"]]
             if query.casefold() in " ".join(str(v) for v in item.values()).casefold()]
    root, _ = navigation.project_paths(product_root)
    identity = {"project": hashlib.sha256(str(root).casefold().encode()).hexdigest(), "version": version, "query": query}
    offset = 0
    if request.get("cursor"):
        cursor = policy.decode_cursor(request["cursor"])
        if any(cursor.get(key) != item for key, item in identity.items()) or type(cursor.get("offset")) is not int:
            raise WorkflowError("Navigation changed; repeat the catalog query")
        offset = cursor["offset"]
        if not 0 <= offset <= len(items):
            raise WorkflowError("Invalid navigation offset")
    result = {"schema_version": 1, "state": "CATALOG", "source": "local", **identity,
              "items": items[offset:offset + 5], "total": len(items), "scan_complete": value["scan_complete"],
              "truncated": value["truncated"], "next_cursor": None}
    while True:
        if not result["items"] and offset < len(items):
            raise WorkflowError("Navigation entry requires a larger max_chars")
        following = offset + len(result["items"])
        result["next_cursor"] = policy.encode_cursor({**identity, "offset": following}) if following < len(items) else None
        result["truncated"] = not value["scan_complete"] or following < len(items)
        if policy.response_size(result) <= limit:
            return result
        if not result["items"]:
            raise WorkflowError("Navigation metadata exceeds max_chars")
        result["items"].pop()


def _commit(request: dict[str, Any], gate: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    root, _ = navigation.project_paths(product_root)
    if gate.get("knowledge_project") != str(root):
        raise WorkflowError("Knowledge project changed since the recorded writes")
    if not git_runtime.in_git_repository(product_root=product_root):
        raise WorkflowError("Documentation Git repository is unavailable")
    if Path(git_runtime.run_git(["rev-parse", "--show-toplevel"], product_root=product_root).stdout.strip()).resolve() != root:
        raise WorkflowError("Documentation must have its own Git repository")
    paths = request.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > 20 or not all(isinstance(p, str) for p in paths):
        raise WorkflowError("Select 1 to 20 exact paths from this gate's written files")
    known = gate.get("knowledge_files", {})
    for path in paths:
        if path not in known:
            raise WorkflowError("Commit path was not written by this knowledge gate")
        target = documentation.regular_path(root / path)
        if not target.is_file() or storage.sha256(target) != known[path]:
            raise WorkflowError("Wiki changed since its recorded write; preview and write again")
    message = request.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 500:
        raise WorkflowError("A bounded commit message is required")
    staged = git_runtime.run_git(["diff", "--cached", "--name-only", "-z"], product_root=product_root).stdout.split("\x00")
    if any(path and path not in paths for path in staged):
        raise WorkflowError("Unexpected staged paths; existing index is preserved")
    config, _ = context.load_config(product_root=product_root)
    default = config.get("project", {}).get("default_branch", "main")
    branch = git_runtime.run_git(["branch", "--show-current"], product_root=product_root).stdout.strip()
    requested_branch = request.get("branch", "")
    if requested_branch:
        if not isinstance(requested_branch, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9/_-]{0,99}", requested_branch) or requested_branch in {default, "main", "master"}:
            raise WorkflowError("Choose a new documentation review branch")
        if git_runtime.run_git(["check-ref-format", "--branch", requested_branch], check=False, product_root=product_root).returncode:
            raise WorkflowError("Invalid documentation branch")
        if branch != requested_branch:
            # Creating from HEAD preserves all local files; never switch to an existing branch.
            git_runtime.run_git(["switch", "-c", requested_branch], product_root=product_root)
            branch = requested_branch
    if not branch or branch in {default, "main", "master"}:
        raise WorkflowError("Knowledge commits require a review branch; supply a new branch name")
    git_runtime.run_git(["add", "--", *paths], product_root=product_root)
    staged = git_runtime.run_git(["diff", "--cached", "--name-only", "-z"], product_root=product_root).stdout.split("\x00")
    if not any(staged):
        raise WorkflowError("No selected changes to commit")
    if any(path and path not in paths for path in staged):
        raise WorkflowError("Unexpected staged paths")
    for path in paths:
        try:
            blob = git_analysis.run_git(root, ["show", f":{path}"], max_output=2 * 1024 * 1024)
        except git_analysis.GitAnalysisError as exc:
            raise WorkflowError("Cannot verify the selected index bytes") from exc
        if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != known[path]:
            raise WorkflowError("Git index bytes differ from the checked wiki write; inspect Git filters before committing")
    git_runtime.run_git(["commit", "-m", message], product_root=product_root)
    head = git_runtime.run_git(["rev-parse", "HEAD"], product_root=product_root).stdout.strip()
    gate["knowledge_commit"] = {"commit": head, "branch": branch, "paths": paths}
    return {"schema_version": 1, "state": "COMMITTED", **gate["knowledge_commit"]}


def _pr(request: dict[str, Any], gate: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    root, _ = navigation.project_paths(product_root)
    if gate.get("knowledge_project") != str(root):
        raise WorkflowError("Knowledge project changed since the recorded commit")
    record = gate.get("knowledge_commit", {})
    if not record:
        raise WorkflowError("Commit selected knowledge files through this gate first")
    head = git_runtime.run_git(["rev-parse", "HEAD"], product_root=product_root).stdout.strip()
    branch = git_runtime.run_git(["branch", "--show-current"], product_root=product_root).stdout.strip()
    if head != record["commit"] or branch != record["branch"]:
        raise WorkflowError("Documentation branch changed after the checked knowledge commit")
    config, _ = context.load_config(product_root=product_root)
    base = config.get("project", {}).get("default_branch", "main")
    base_commit = git_runtime.run_git(["rev-parse", "--verify", f"{base}^{{commit}}"], product_root=product_root).stdout.strip()
    changed = git_runtime.run_git(["diff", "--name-only", "-z", f"{base_commit}...{head}"], product_root=product_root).stdout.split("\x00")
    if not any(changed) or any(path and path not in record["paths"] for path in changed):
        raise WorkflowError("PR contains changes outside the selected knowledge files")
    title, body = request.get("title"), request.get("body")
    if not isinstance(title, str) or not title.strip() or len(title) > 240 or not isinstance(body, str) or not body.strip() or len(body) > 4000:
        raise WorkflowError("Supply a bounded PR title and body with sources and verification limits")
    response = publication.push_documentation_pr(title, body, [], product_root=product_root)
    return {"schema_version": 1, "state": "PR_CREATED", "url": response.get("html_url"), "number": response.get("number")}


def command(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    action, request = args.action, args.request
    if not isinstance(action, str) or action not in FIELDS or not isinstance(request, dict) or set(request) - FIELDS[action]:
        raise WorkflowError("Invalid knowledge action or request fields")
    gate = state.load_gate(args.gate_id, states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"}, product_root=product_root)
    state.require_gate_tool(gate, "flow1c_knowledge", product_root=product_root)
    if action in WRITE_ACTIONS:
        if gate.get("operation") != "status" or gate.get("mode", "formal") != "formal" or gate["state"] != "READY":
            raise WorkflowError("Wiki mutations require a ready formal status gate")
        if request.get("confirmed") is not True:
            raise WorkflowError("This wiki mutation requires the user's explicit instruction")
        current_root, _ = navigation.project_paths(product_root)
        if gate.get("knowledge_project") not in {None, str(current_root)}:
            raise WorkflowError("Knowledge project changed; use a new status gate")
    try:
        if action == "navigation":
            result = _navigation(request, product_root=product_root)
        elif action == "search":
            result = knowledge.search(request, product_root=product_root)
        elif action == "read":
            result = knowledge.read(request, product_root=product_root)
        elif action in {"preview", "write"}:
            root, _ = navigation.project_paths(product_root)
            if action == "write":
                candidate = policy.card_content(request.get("path", ""), request.get("content", ""))
                preview = gate.get("knowledge_previews", {}).get(request.get("path"), {})
                if preview != {"project": str(root), "expected_version": request.get("expected_version"),
                               "version": hashlib.sha256(candidate.encode()).hexdigest()}:
                    raise WorkflowError("Preview this exact wiki content on the same gate before writing")
            result = knowledge.edit(request, product_root=product_root, apply=action == "write")
            if action == "preview":
                gate.setdefault("knowledge_previews", {})[request["path"]] = {
                    "project": str(root), "expected_version": result["expected_version"], "version": result["version"]}
            else:
                gate.setdefault("knowledge_files", {})[result["path"]] = result["version"]
                gate["knowledge_project"] = str(root)
        elif action == "refresh":
            result = navigation.refresh(product_root=product_root)
            gate.setdefault("knowledge_files", {}).update({item["path"]: item["sha256"] for item in result["changed_files"]})
            gate["knowledge_project"] = str(navigation.project_paths(product_root)[0])
        elif action == "commit":
            result = _commit(request, gate, product_root=product_root)
        else:
            result = _pr(request, gate, product_root=product_root)
    except (ValueError, OSError) as exc:
        raise WorkflowError(str(exc)) from exc
    if gate.get("operation") == "status":
        gate["action_completed"] = "status"
    state.save_gate(gate, product_root=product_root)
    return OperationResult(result, 0)
