"""Bounded, read-only access to the supported cc-1c-skills query inspectors."""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_OUTPUT_CHARS = 24_000
TIMEOUT_SECONDS = 30

OPERATIONS: dict[str, tuple[str, str, bool]] = {
    "meta-overview": ("meta-info", "overview", False),
    "meta-full": ("meta-info", "full", False),
    "meta-item": ("meta-info", "overview", True),
    "skd-overview": ("skd-info", "overview", False),
    "skd-query": ("skd-info", "query", True),
    "skd-fields": ("skd-info", "fields", False),
    "skd-params": ("skd-info", "params", False),
    "skd-links": ("skd-info", "links", False),
    "skd-full": ("skd-info", "full", False),
}


class CcInspectionError(RuntimeError):
    """An unsafe or invalid inspection request."""


class CcInspectionUnavailable(CcInspectionError):
    """The optional inspector cannot run; independent text analysis may continue."""


def _is_reparse_or_symlink(candidate: Path) -> bool:
    if candidate.is_symlink():
        return True
    try:
        attributes = getattr(candidate.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return True


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_input_path(*, source: str, relative_path: str, request_root: Path,
                       request_artifacts: list[dict[str, Any]], configuration_root: Path | None,
                       extension_root: Path | None) -> tuple[Path, str]:
    """Resolve only accepted request XML or XML below an explicitly configured 1C root."""
    raw = str(relative_path or "").strip()
    supplied = Path(raw)
    if not raw or supplied.is_absolute() or ".." in supplied.parts:
        raise CcInspectionError("path must be relative and must not contain '..'")
    roots = {"request": request_root, "configuration": configuration_root, "extension": extension_root}
    if source not in roots:
        raise CcInspectionError("source must be request, configuration, or extension")
    root = roots[source]
    if root is None:
        raise CcInspectionUnavailable(f"{source}_path is not configured")
    root = root.resolve()
    current = root
    for part in supplied.parts:
        current = current / part
        if current.exists() and _is_reparse_or_symlink(current):
            raise CcInspectionError("symlinks and reparse points are not allowed")
    target = (root / supplied).resolve()
    if not _within(target, root):
        raise CcInspectionError("path escapes the allowed source root")
    if source == "request":
        accepted = {str(item.get("path", "")).replace("\\", "/") for item in request_artifacts}
        normalized = str(supplied).replace("\\", "/")
        if normalized not in accepted:
            raise CcInspectionError("request path is not an accepted artifact for this gate")
    if not target.is_file() or target.suffix.casefold() != ".xml":
        raise CcInspectionError("the selected input must be an existing XML file")
    if _is_reparse_or_symlink(target):
        raise CcInspectionError("symlinks and reparse points are not allowed")
    if target.stat().st_size > MAX_INPUT_BYTES:
        raise CcInspectionError(f"input exceeds the {MAX_INPUT_BYTES}-byte limit")
    return target, source


def validate_operation(operation: str, target: Path, name: str = "") -> tuple[str, str]:
    if operation not in OPERATIONS:
        raise CcInspectionError("unsupported cc-1c-skills operation")
    skill, mode, name_required = OPERATIONS[operation]
    clean_name = str(name or "").strip()
    if name_required and not clean_name:
        raise CcInspectionError(f"operation {operation} requires name")
    if len(clean_name) > 128 or re.search(r"[\x00-\x1f\x7f]", clean_name):
        raise CcInspectionError("name contains control characters or exceeds 128 characters")
    if skill == "skd-info" and target.name.casefold() != "template.xml":
        raise CcInspectionError("skd-info is allowed only for a Template.xml file")
    if skill == "meta-info" and target.name.casefold() == "template.xml":
        raise CcInspectionError("Template.xml must be inspected with skd-info, not meta-info")
    try:
        _, root = next(ET.iterparse(target, events=("start",)))
        root_name = root.tag.rsplit("}", 1)[-1]
    except (ET.ParseError, OSError, StopIteration) as exc:
        raise CcInspectionError(f"input is not readable XML: {exc}") from exc
    expected_root = "DataCompositionSchema" if skill == "skd-info" else "MetaDataObject"
    if root_name != expected_root:
        raise CcInspectionError(f"{skill} requires XML root {expected_root}, found {root_name}")
    return skill, mode


def run_inspection(*, checkout: Path, operation: str, target: Path, name: str = "",
                   max_chars: int = 12_000) -> dict[str, Any]:
    """Run a fixed upstream info script without exposing PowerShell or arbitrary arguments."""
    skill, mode = validate_operation(operation, target, name)
    script = (checkout / ".claude" / "skills" / skill / "scripts" / f"{skill}.ps1").resolve()
    expected_root = checkout.resolve()
    if not _within(script, expected_root) or not script.is_file() or _is_reparse_or_symlink(script):
        raise CcInspectionUnavailable(f"cc-1c-skills {skill} is not installed or is incomplete")
    limit = max(1, min(int(max_chars), MAX_OUTPUT_CHARS))
    path_argument = "-ObjectPath" if skill == "meta-info" else "-TemplatePath"
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(script), path_argument, str(target), "-Mode", mode, "-Limit", "200",
    ]
    if name:
        command.extend(["-Name", str(name).strip()])
    try:
        result = subprocess.run(
            command, cwd=target.parent, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8-sig", errors="replace", timeout=TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CcInspectionUnavailable(f"{skill} exceeded the {TIMEOUT_SECONDS}-second timeout") from exc
    except OSError as exc:
        raise CcInspectionUnavailable(f"PowerShell could not start {skill}: {exc}") from exc
    stdout, stderr = result.stdout or "", result.stderr or ""
    combined = stdout if stdout.strip() else stderr
    truncated = len(combined) > limit
    output = combined[:limit]
    if result.returncode:
        raise CcInspectionUnavailable(f"{skill} failed with exit code {result.returncode}: {output.strip()}")
    if not output.strip():
        raise CcInspectionUnavailable(f"{skill} produced no output")
    return {
        "skill": skill,
        "operation": operation,
        "mode": mode,
        "output": output,
        "truncated": truncated,
        "limits": {"timeout_seconds": TIMEOUT_SECONDS, "max_output_chars": limit, "max_input_bytes": MAX_INPUT_BYTES},
    }
