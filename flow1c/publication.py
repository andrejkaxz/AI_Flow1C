"""Status, publication validation, commits and pull requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import git_runtime as git_runtime
from flow1c import intake as intake_service
from flow1c import storage as storage
from flow1c import system as system
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult


def publication_snapshot(
    code: str, *, include_extension: bool = False, product_root: Path
) -> dict[str, Any]:
    root = work_items.work_item_root(code, product_root=product_root)
    files = {
        str(p.relative_to(root)).replace("\\", "/"): storage.sha256(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in {"evidence", "context", ".git"}
    }
    snapshot: dict[str, Any] = {"files": files}
    if include_extension:
        _, manifest = work_items.load_manifest(code, product_root=product_root)
        diff, error = system.extension_git_state(code, manifest, product_root=product_root)
        if error or diff is None:
            raise WorkflowError(error or "Extension Git state is unavailable")
        path = Path(diff["path"])

        def git_output(arguments: list[str]) -> str:
            result = subprocess.run(
                ["git", *arguments],
                cwd=path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode:
                raise WorkflowError("Cannot capture extension Git state")
            return result.stdout

        snapshot["extension"] = {
            "head": diff["head"],
            "branch": diff["branch"],
            "base_head": git_output(["rev-parse", diff["base"]]).strip(),
            "dirty": git_output(["status", "--porcelain"]),
            "working_diff_sha256": hashlib.sha256(
                git_output(["diff", "--no-ext-diff", "--binary", "HEAD"]).encode("utf-8")
            ).hexdigest(),
        }
    return snapshot


def validate_publication(code: str, phase: str, *, product_root: Path) -> None:
    _, manifest = work_items.load_manifest(code, product_root=product_root)
    if (
        manifest.get("traceability_mode", "registry") != "registry"
        or manifest.get("registry", {}).get("status", "verified") != "verified"
    ):
        raise WorkflowError("Provisional or unverified-registry work items cannot be published")
    stage = runtime.load_stages(product_root=product_root)["operations"]["publish"]
    if manifest.get("status") not in stage["allowed_statuses"]:
        raise WorkflowError("Current work-item status does not allow publication")
    phases = {
        "specification": {"functional-spec", "functional-review"},
        "technical": {"technical-design", "technical-implementation", "code-review", "development"},
        "acceptance": {"testing"},
    }
    if phase not in phases:
        raise WorkflowError("Unknown publication phase")
    config, local = runtime.load_config(product_root=product_root)
    gitea = {**config.get("gitea", {}), **local.get("gitea", {})}
    reviewer_role = "technical" if phase == "technical" else "functional"
    if not gitea.get("reviewers", {}).get(reviewer_role):
        raise WorkflowError(f"No {reviewer_role} reviewers configured")
    index = intake_service.load_artifact_index(code, product_root=product_root)
    present = {a.get("category") for a in index["artifacts"]} | set(index["confirmed_absent"])
    if any((category not in present for category in stage.get("confirm_absence", []))):
        raise WorkflowError(
            "Organizational approvals must be supplied or their absence recorded before publication"
        )
    for path in (work_items.work_item_root(code, product_root=product_root) / "evidence").glob(
        "*.json"
    ):
        evidence = storage.read_json(path, {})
        if (
            evidence.get("completion_state") != "COMPLETE"
            or evidence.get("operation") not in phases[phase]
        ):
            continue
        snapshot = evidence.get("validation_snapshot")
        if not isinstance(snapshot, dict) or not evidence.get("gate_id"):
            continue
        completed_stage = runtime.load_stages(product_root=product_root)["operations"][
            evidence["operation"]
        ]
        if any(
            (
                manifest.get("approvals", {}).get(role) != status
                for role, status in completed_stage.get("required_approvals", {}).items()
            )
        ):
            continue
        if snapshot == publication_snapshot(
            code, include_extension="extension" in snapshot, product_root=product_root
        ):
            if snapshot.get("extension", {}).get("dirty"):
                continue
            return
    raise WorkflowError(
        "No current validated evidence matches this phase, documents and Git state. Run the relevant review again."
    )


def render_status(*, product_root: Path) -> str:
    rows = []
    for path in sorted(
        (runtime.project_data_root(product_root=product_root) / "work-items").glob("*/manifest.yaml")
    ):
        manifest = storage.read_json(path, {})
        rows.append(
            (
                manifest.get("code", path.parent.name),
                manifest.get("title", ""),
                manifest.get("status", "unknown"),
                ", ".join(manifest.get("requirements", [])),
                manifest.get("approvals", {}).get("functional_architect", "pending"),
                manifest.get("approvals", {}).get("technical_architect", "pending"),
            )
        )
    lines = [
        "# Project status",
        "",
        "| FS | Title | Status | Requirements | Functional approval | Technical approval |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        (
            "| " + " | ".join((str(value).replace("|", "\\|") for value in row)) + " |"
            for row in rows
        )
    )
    if not rows:
        lines.append("| — | No work items | — | — | — | — |")
    return "\n".join(lines) + "\n"


def status(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    value = render_status(product_root=product_root)
    if args.write:
        target = runtime.project_data_root(product_root=product_root) / "wiki" / "status.md"
        storage.write_text(target, value)
        _value = target
    else:
        _value = value
    return OperationResult(_value, 0)


def git_commit(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    code = args.code
    work_items.load_manifest(code, product_root=product_root)
    if not git_runtime.in_git_repository(product_root=product_root):
        raise WorkflowError("Current directory is not a Git repository.")
    item_relative = (
        work_items.work_item_root(code, product_root=product_root)
        .relative_to(runtime.project_root(product_root=product_root))
        .as_posix()
    )
    repository = runtime.project_root(product_root=product_root)
    data = runtime.project_data_root(product_root=product_root)
    allowed = [item_relative, (data / "wiki/status.md").relative_to(repository).as_posix()]
    if args.include_registry:
        allowed.append((data / "registry").relative_to(repository).as_posix())
    git_runtime.run_git(["add", "--", *allowed], product_root=product_root)
    staged = git_runtime.run_git(
        ["diff", "--cached", "--name-only"], product_root=product_root
    ).stdout.strip()
    if not staged:
        raise WorkflowError("No allowed changes are staged for commit.")
    unexpected = [
        line
        for line in staged.splitlines()
        if not any((line == root or line.startswith(root + "/") for root in allowed))
    ]
    if unexpected:
        raise WorkflowError("Unexpected staged paths: " + ", ".join(unexpected))
    result = git_runtime.run_git(["commit", "-m", args.message], product_root=product_root)
    _value = result.stdout.strip()
    return OperationResult(_value, 0)


def api_request(url: str, token: str, *, method: str = "GET", payload: Any = None) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"token {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise WorkflowError(f"Gitea API returned HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise WorkflowError(f"Cannot reach Gitea API: {exc}") from exc


def pr_create(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    code = args.code
    validate_publication(code, args.phase, product_root=product_root)
    _, manifest = work_items.load_manifest(code, product_root=product_root)
    config, local = runtime.load_config(product_root=product_root)
    gitea = {**config.get("gitea", {}), **local.get("gitea", {})}
    required = ["base_url", "owner", "repository"]
    missing = [key for key in required if not str(gitea.get(key, "")).strip()]
    if missing:
        raise WorkflowError("Missing Gitea settings: " + ", ".join(missing))
    token_env = gitea.get("token_env", "FLOW1C_GITEA_TOKEN")
    token = os.environ.get(token_env, "")
    if not token:
        raise WorkflowError(f"Environment variable {token_env} is not set.")
    if not git_runtime.in_git_repository(product_root=product_root):
        raise WorkflowError("Current directory is not a Git repository.")
    branch = git_runtime.run_git(
        ["branch", "--show-current"], product_root=product_root
    ).stdout.strip()
    if not branch or branch in {"main", "master"}:
        raise WorkflowError("Refusing to create a PR from the default branch.")
    if git_runtime.run_git(["status", "--porcelain"], product_root=product_root).stdout.strip():
        raise WorkflowError("Commit all changes before creating a PR.")
    git_runtime.run_git(
        ["push", "--set-upstream", "origin", branch], capture=True, product_root=product_root
    )
    phase_reviewers = {
        "specification": gitea.get("reviewers", {}).get("functional", []),
        "technical": gitea.get("reviewers", {}).get("technical", []),
        "acceptance": gitea.get("reviewers", {}).get("functional", []),
    }
    reviewers = phase_reviewers.get(args.phase, [])
    title = args.title or f"{code}: {manifest.get('title')} [{args.phase}]"
    body = f"FS: {code}\n\nRequirements: {', '.join(manifest.get('requirements', []))}\n\nStatus: {manifest.get('status')}\n\nPrepared by Flow1C. Human review and approval are required."
    base_url = str(gitea["base_url"]).rstrip("/")
    owner = urllib.parse.quote(str(gitea["owner"]), safe="")
    repository = urllib.parse.quote(str(gitea["repository"]), safe="")
    url = f"{base_url}/api/v1/repos/{owner}/{repository}/pulls"
    payload = {
        "base": config.get("project", {}).get("default_branch", "main"),
        "head": branch,
        "title": title,
        "body": body,
        "reviewers": reviewers,
    }
    response = api_request(url, token, method="POST", payload=payload)
    _value = response
    return OperationResult(_value, 0)
