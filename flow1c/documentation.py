"""Versioned documentation storage and non-destructive project scaffold."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from flow1c import storage
from flow1c.errors import WorkflowError
from flow1c.documentation_policy import LAYOUT_FILE, SERVICE_FOLDERS, render_scaffold, scaffold_path, validate_layout


def regular_path(path: Path) -> Path:
    """Validate ancestors before resolving, including dangling links and junctions."""
    absolute = Path(os.path.abspath(path))
    for node in (absolute, *absolute.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise WorkflowError(f"Documentation path contains a symlink or junction: {node}")
        if node != absolute and not stat.S_ISDIR(info.st_mode):
            raise WorkflowError(f"Documentation parent path is occupied by a file: {node}")
    return absolute.resolve()


def layout_version(documentation: Path) -> int:
    marker = regular_path(documentation / LAYOUT_FILE)
    if not marker.exists():
        return 1
    value = storage.read_json(marker)
    try:
        return validate_layout(value)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


def data_root(documentation: Path) -> Path:
    root = regular_path(documentation)
    return regular_path(root / ".flow1c") if layout_version(root) == 2 else root


def initialize(documentation: Path, templates: Path, *, preserve_legacy: bool = False) -> list[str]:
    """Select v2 only for a new directory; validate the entire plan before writes."""
    if not documentation.is_absolute() or not templates.is_absolute():
        raise WorkflowError("Documentation and scaffold paths must be absolute")
    destination, source = regular_path(documentation), regular_path(templates)
    if destination == Path(destination.anchor) or source == Path(source.anchor):
        raise WorkflowError("Documentation and scaffold paths must not be filesystem roots")
    if destination == source or destination in source.parents or source in destination.parents:
        raise WorkflowError("Documentation and scaffold directories must not overlap")
    if not source.is_dir():
        raise WorkflowError(f"Documentation scaffold is missing: {source}")
    if destination.exists() and not destination.is_dir():
        raise WorkflowError(f"Documentation path is occupied by a file: {destination}")
    version = layout_version(destination)
    marker = destination / LAYOUT_FILE
    new_marker = not marker.exists() and not preserve_legacy and (
        not destination.exists() or not any(p.name != ".git" for p in destination.iterdir())
    )
    if new_marker:
        version = 2
    plan: list[tuple[Path, bytes]] = []
    # Walk explicitly: rglob can silently omit links or traversal errors.
    pending = [source]
    while pending:
        for item in pending.pop().iterdir():
            regular_path(item)
            if item.is_dir():
                pending.append(item)
                continue
            if item.suffix != ".md":
                continue
            relative = scaffold_path(item.relative_to(source), version)
            target = regular_path(destination / relative)
            if target.is_dir():
                raise WorkflowError(f"Documentation scaffold file path is occupied by a directory: {target}")
            content = render_scaffold(item.read_text(encoding="utf-8-sig"), version).encode("utf-8")
            plan.append((target, content))
    if not plan:
        raise WorkflowError("Documentation scaffold contains no Markdown files")
    if new_marker:
        regular_path(marker)
        if marker.is_dir():
            raise WorkflowError("Documentation layout path is occupied by a directory")
    created: list[str] = []
    if new_marker:
        # Commit the version first so interrupted creation resumes the same layout.
        storage.write_json(marker, {"schema_version": 1, "layout_version": 2})
        created.append(marker.relative_to(destination).as_posix())
    for target, content in sorted(plan):
        if target.exists():
            continue
        regular_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        created.append(target.relative_to(destination).as_posix())
    return created
