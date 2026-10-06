"""Executable and source path checks shared by setup and workflow."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from flow1c import context as runtime


def command_path(name: str) -> str | None:
    return shutil.which(name)


def normalize_git_url(url: str) -> str:
    """Return a comparable host/path identity for common Git remote syntaxes."""
    value = str(url or "").strip().replace("\\", "/")
    if not value:
        return ""
    if "://" not in value and re.match("^[^/@]+@[^:]+:.+$", value):
        user_host, path = value.split(":", 1)
        value = f"ssh://{user_host}/{path}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname.casefold()
        path = parsed.path
    else:
        host = ""
        path = value
    path = re.sub("^/+|/+$", "", path)
    path = re.sub("\\.git$", "", path, flags=re.IGNORECASE)
    return f"{host}/{path}".casefold().strip("/")


def git_remote_url(path: Path) -> str:
    if not command_path("git") or not (path / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=path,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return result.stdout.strip()


def redact_url_credentials(url: str) -> str:
    """Return a display-safe remote URL without embedded HTTP credentials."""
    value = str(url or "").strip()
    if not value or "://" not in value:
        return value
    parsed = urllib.parse.urlsplit(value)
    if parsed.username is None and parsed.password is None:
        return value
    hostname = parsed.hostname or ""
    if ":" in hostname and (not hostname.startswith("[")):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def containing_workflow_root(path: Path) -> Path | None:
    candidate = path.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for current in (candidate, *candidate.parents):
        if (current / runtime.CONFIG_FILE).is_file() and (
            current / "scripts" / "flow1c.py"
        ).is_file():
            return current
    return None


def contains_1c_sources(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        return any(
            (
                candidate.suffix.casefold() in {".bsl", ".xml", ".mdo"}
                for candidate in path.rglob("*")
            )
        )
    except OSError:
        return False


def resolve_1c_source_root(path: Path) -> Path:
    """Resolve a repository checkout to its single nested 1C XML source root."""
    root = path.resolve()
    if (root / "Configuration.xml").is_file():
        return root
    candidates: list[Path] = []
    excluded = {".git", ".tools", ".venv", ".workspace", "build", "node_modules"}
    try:
        for current, directories, files in os.walk(root):
            directories[:] = [name for name in directories if name.casefold() not in excluded]
            if "Configuration.xml" in files:
                candidates.append(Path(current).resolve())
                if len(candidates) > 1:
                    return root
    except OSError:
        return root
    return candidates[0] if candidates else root


def extension_source_state(local: dict[str, Any]) -> tuple[Path | None, str, str]:
    raw = str(local.get("extension_path", "")).strip()
    extension = Path(raw) if raw else None
    mode = str(local.get("extension_mode", "")).strip().casefold()
    has_sources = bool(extension and contains_1c_sources(extension))
    if mode == "git":
        expected_url = str(local.get("extension_repository_url", "")).strip()
        actual_url = git_remote_url(extension) if extension else ""
        valid = bool(has_sources and expected_url and actual_url) and normalize_git_url(
            expected_url
        ) == normalize_git_url(actual_url)
        return (extension, "OK" if valid else "ERROR", actual_url or "Git origin is not configured")
    if mode == "local-export":
        valid = has_sources and (not str(local.get("extension_repository_url", "")).strip())
        detail = (
            "local XML/BSL export"
            if valid
            else "local export must contain XML/BSL and have no repository URL"
        )
        return (extension, "OK" if valid else "ERROR", detail)
    return (extension, "ERROR", "set extension_mode to git or local-export")


def extension_git_state(
    code: str, manifest: dict[str, Any], *, product_root: Path
) -> tuple[dict[str, Any] | None, str]:
    _, local = runtime.load_config(product_root=product_root)
    raw = str(local.get("extension_path", "")).strip()
    extension = Path(raw).resolve() if raw else None
    if extension is None or not extension.is_dir():
        return (None, "extension path is not configured")
    if not (extension / ".git").exists() or not command_path("git"):
        return (None, "extension source is not a Git checkout")
    branch_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=extension,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    branch = branch_result.stdout.strip()
    expected = str(manifest.get("branches", {}).get("extension", f"feature/{code}"))
    default_branch = (
        runtime.load_config(product_root=product_root)[0]
        .get("project", {})
        .get("default_branch", "main")
    )
    base_candidates = [f"origin/{default_branch}", str(default_branch)]
    base = next(
        (
            candidate
            for candidate in base_candidates
            if subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                cwd=extension,
                capture_output=True,
                check=False,
            ).returncode
            == 0
        ),
        "",
    )
    if not base:
        return (None, f"base branch {default_branch} is unavailable")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=extension,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    ).stdout.strip()
    names = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=extension,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    ).stdout.splitlines()
    return (
        {
            "path": str(extension),
            "branch": branch,
            "expected_branch": expected,
            "base": base,
            "head": head,
            "files": [name for name in names if name.strip()],
        },
        "",
    )
