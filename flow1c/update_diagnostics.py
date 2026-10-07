"""Bounded, read-only recovery diagnostics for an installation update."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flow1c import storage, system
from flow1c.errors import WorkflowError


def safe_remote_url(value: str) -> str:
    """Display a Git location without authentication, query or fragment data."""
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
        return ""
    if "://" not in value and re.match(r"^[^/@]+@[^:]+:.+$", value):
        user_host, path = value.split(":", 1)
        value = f"ssh://{user_host}/{path}"
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            if parsed.port:
                host += f":{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        if parsed.scheme in {"http", "https", "ssh", "git"} or "@" in value:
            return ""
        if parsed.scheme == "file":
            return urlunsplit(("file", "", parsed.path, "", ""))
        return value.split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        return ""


def _plain_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and storage.is_reparse_or_symlink(part)):
            raise WorkflowError("Update diagnostics reject symlinks and junctions.")


def _git_read(repository: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repository), *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Git errors can contain credentials; they never cross this boundary.
    if result.returncode or len(result.stdout) > 4096:
        return None
    return result.stdout.strip()


def diagnose_update(gate: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    """Read only selected local settings and tracking-remote metadata, never sources."""
    config_path = product_root / ".flow1c.local.json"
    _plain_path(config_path)
    if not config_path.is_file() or config_path.stat().st_size > 1024 * 1024:
        raise WorkflowError("Local configuration is missing or too large for update diagnostics.")
    local = storage.read_json(config_path)
    if not isinstance(local, dict):
        raise WorkflowError("Local configuration must be a JSON object.")
    updater = gate.get("update_result")
    if not isinstance(updater, dict):
        updater = {}
    result = {
        "schema_version": 1, "state": "DIAGNOSTIC", "ready": False,
        "gate_state": gate.get("state"), "update_id": gate.get("update_id"),
        "updater_state": updater.get("state"),
        "phase": updater.get("phase"),
        "extension_repository": {"state": "SKIPPED"},
        "next_action": "Review the diagnosis before retrying the same update gate.",
    }
    if local.get("extension_mode") != "git":
        return result
    repository = result["extension_repository"] = {"state": "UNAVAILABLE"}
    configured_url = local.get("extension_repository_url", "")
    repository["configured_url"] = safe_remote_url(configured_url) if isinstance(configured_url, str) else ""
    raw_path = local.get("extension_path", "")
    if not isinstance(raw_path, str):
        raise WorkflowError("Extension path must be a string.")
    path = Path(raw_path)
    if not raw_path or not path.is_absolute() or path == Path(path.anchor):
        raise WorkflowError("Extension path must be an absolute checkout directory.")
    _plain_path(path)
    if storage.path_is_within(path, product_root) or storage.path_is_within(product_root, path):
        raise WorkflowError("Extension checkout must not overlap the product checkout.")
    git_path = path / ".git"
    _plain_path(git_path)
    if not path.is_dir() or not git_path.exists():
        return result
    if not git_path.is_dir():
        repository["state"] = "LINKED_WORKTREE"
        return result
    for metadata in ("config", "HEAD"):
        _plain_path(git_path / metadata)
    branch = _git_read(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        repository["state"] = "DETACHED_OR_UNAVAILABLE"
        return result
    remote = _git_read(path, "config", "--get", f"branch.{branch}.remote")
    if not remote or remote == "." or not re.fullmatch(r"[A-Za-z0-9_./-]+", remote):
        repository["state"] = "NO_TRACKING_REMOTE"
        return result
    actual = _git_read(path, "remote", "get-url", "--", remote)
    if actual is None:
        return result
    actual_url = safe_remote_url(actual)
    expected_identity = system.normalize_git_url(repository["configured_url"])
    actual_identity = system.normalize_git_url(actual_url)
    repository.update(
        branch=branch, remote=remote, actual_url=actual_url,
        configured_identity=expected_identity, actual_identity=actual_identity,
        matches=bool(expected_identity and expected_identity == actual_identity),
    )
    repository["state"] = "MATCH" if repository["matches"] else "MISMATCH"
    if repository["matches"]:
        if "remote URL does not match extension_repository_url" in str(updater.get("error", "")):
            result["next_action"] = "Repository identities match. Resume update on the same gate without reconfiguring paths or remotes."
        else:
            result["next_action"] = "Repository identities match. Review the original updater blocker; this diagnosis does not resolve other failures."
    else:
        result["next_action"] = "Ask which repository identity is intended; preserve configuration and remote until that decision."
    return result
