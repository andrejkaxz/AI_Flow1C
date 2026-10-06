"""Execution configuration and reference inputs; all reads are explicit."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from flow1c import storage as storage
from flow1c.errors import WorkflowError
from scripts import flow1c_policy as policy

CONFIG_FILE = ".flow1c.json"
LOCAL_CONFIG_FILE = ".flow1c.local.json"
ID_PATTERN = re.compile("^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_.]*-\\d+$")
VALID_ROLES = ("analyst", "functional-architect", "technical-architect", "tester")
PROFILE_NAMES = ("conversation", "project-basic", "documents", "analysis", "implementation", "full")
LOCAL_CONFIG_SCHEMA_VERSION = 2
REFERENCE_KINDS = ("auto", "requirement", "specification")
from dataclasses import dataclass


def documentation_root(product_root: Path, local_config: dict[str, Any]) -> Path:
    """Preserve the configured external root and the legacy checkout fallback."""
    raw = str(local_config.get("documentation_path", "")).strip()
    return Path(raw).expanduser().resolve() if raw else product_root


@dataclass(frozen=True)
class RuntimeContext:
    """Paths and loaded configuration only; never a container of service functions."""

    product_root: Path
    config: dict[str, Any]
    local_config: dict[str, Any]

    @property
    def documentation_root(self) -> Path:
        return documentation_root(self.product_root, self.local_config)


def load_context(product_root: Path) -> RuntimeContext:
    """Load on explicit invocation; keep missing/invalid JSON failure semantics."""
    config = storage.read_json(product_root / CONFIG_FILE)
    if config is None:
        raise WorkflowError(f"{CONFIG_FILE} not found. Run scripts/bootstrap.ps1 first.")
    local = storage.read_json(product_root / LOCAL_CONFIG_FILE, {})
    return RuntimeContext(product_root, config, local)


def load_config(*, product_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    context = load_context(product_root)
    return (context.config, context.local_config)


def project_root(local: dict[str, Any] | None = None, *, product_root: Path) -> Path:
    """Return the external documentation repository, or ROOT for legacy projects."""
    if local is None:
        local = storage.read_json(product_root / LOCAL_CONFIG_FILE, {})
    return documentation_root(product_root, local)


def load_capabilities(*, product_root: Path) -> dict[str, Any]:
    value = storage.read_json(product_root / "config" / "capabilities.json")
    try:
        return policy.load_capability_model(value)
    except ValueError as exc:
        raise WorkflowError(f"Invalid capability configuration: {exc}") from exc


def resolve_reference_args(
    args: argparse.Namespace,
    local: dict[str, Any] | None = None,
    *,
    ignore_legacy_code: bool = False,
    product_root: Path,
) -> dict[str, Any]:
    """Resolve new reference fields and all legacy CLI aliases."""
    if local is None:
        local = storage.read_json(product_root / LOCAL_CONFIG_FILE, {})
    explicit = getattr(args, "task_reference", None)
    legacy_code = None if ignore_legacy_code else getattr(args, "code", None)
    legacy_g_number = getattr(args, "g_number", None)
    task = (
        explicit
        if explicit not in (None, "")
        else legacy_code if legacy_code not in (None, "") else legacy_g_number
    )
    project = getattr(args, "project_reference", None)
    if project in (None, ""):
        project = local.get("project_reference") if isinstance(local, dict) else None
    try:
        return policy.resolve_work_reference(task, project)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


def load_stages(*, product_root: Path) -> dict[str, Any]:
    path = product_root / "config" / "stages.json"
    stages = storage.read_json(path)
    if not isinstance(stages, dict) or not isinstance(stages.get("operations"), dict):
        raise WorkflowError(f"Invalid stage configuration: {path}")
    return stages
