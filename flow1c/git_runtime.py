"""Argument-array Git operations for the documentation checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

from flow1c import context as runtime
from flow1c.errors import WorkflowError


def run_git(
    args: list[str], *, check: bool = True, capture: bool = True, product_root: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=runtime.project_root(product_root=product_root),
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )


def in_git_repository(*, product_root: Path) -> bool:
    try:
        return (
            run_git(
                ["rev-parse", "--is-inside-work-tree"], product_root=product_root
            ).stdout.strip()
            == "true"
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def ensure_clean_tree(*, product_root: Path) -> None:
    status = run_git(["status", "--porcelain"], product_root=product_root).stdout.strip()
    if status:
        raise WorkflowError(
            "Git working tree is not clean. Commit or stash existing changes first."
        )


def create_branch(branch: str, *, product_root: Path) -> None:
    if not in_git_repository(product_root=product_root):
        raise WorkflowError("Current directory is not a Git repository.")
    ensure_clean_tree(product_root=product_root)
    default_branch = (
        runtime.load_config(product_root=product_root)[0]
        .get("project", {})
        .get("default_branch", "main")
    )
    try:
        run_git(["fetch", "origin", default_branch], product_root=product_root)
        start_point = f"origin/{default_branch}"
    except subprocess.CalledProcessError:
        start_point = default_branch
    existing = run_git(["branch", "--list", branch], product_root=product_root).stdout.strip()
    if existing:
        run_git(["switch", branch], product_root=product_root)
    else:
        run_git(["switch", "-c", branch, start_point], product_root=product_root)
