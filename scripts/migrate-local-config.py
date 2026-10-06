#!/usr/bin/env python3
"""Migrate the ignored Flow1C machine configuration in place."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


CURRENT_SCHEMA_VERSION = 2


class MigrationError(RuntimeError):
    pass


def read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MigrationError(f"Local configuration not found: {path}. Run /setup first.")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"Cannot read local configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError("Local configuration must be a JSON object.")
    return value


def schema_version(config: dict[str, Any]) -> int:
    value = config.get("schema_version", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationError("Local configuration schema_version must be a non-negative integer.")
    if value > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"Local configuration schema {value} is newer than supported schema "
            f"{CURRENT_SCHEMA_VERSION}. Install a compatible Flow1C version."
        )
    return value


def migrate_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy while preserving unknown user-defined fields."""
    migrated = dict(config)
    version = schema_version(migrated)
    changes: list[str] = []

    if version == 0:
        migrated["schema_version"] = 1
        changes.append("0->1: added schema_version")
        version = 1

    if version == 1:
        migrated.setdefault("template_library", {"schema_version": 1, "storage_kind": "documentation",
                                                "root": "document-templates", "library_id": None})
        migrated["schema_version"] = 2
        changes.append("1->2: added versioned template_library; legacy template and unknown fields preserved")
        version = 2

    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"No migration path from schema {version} to {CURRENT_SCHEMA_VERSION}."
        )
    return migrated, changes


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def migrate_file(path: Path, backup_dir: Path | None = None) -> dict[str, Any]:
    original = read_config(path)
    previous_version = schema_version(original)
    migrated, changes = migrate_config(original)
    backup_path: Path | None = None

    if changes:
        backup_dir = backup_dir or path.parent / ".workspace" / "backups" / "local-config-migration"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / "local-config.before-migration.json"
        if backup_path.exists():
            import uuid
            backup_path = backup_dir / ("local-config.before-migration." + str(uuid.uuid4()) + ".json")
        shutil.copy2(path, backup_path)
        write_json_atomic(path, migrated)

    return {
        "schema_version": 1,
        "state": "MIGRATED" if changes else "CURRENT",
        "previous_schema_version": previous_version,
        "current_schema_version": CURRENT_SCHEMA_VERSION,
        "changes": changes,
        "backup": str(backup_path) if backup_path else "",
        "path": str(path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="Path to .flow1c.local.json")
    parser.add_argument("--backup-dir", help="Directory for a pre-migration backup")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = migrate_file(
            Path(args.path).expanduser().resolve(),
            Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None,
        )
    except MigrationError as exc:
        if args.json:
            print(json.dumps({"schema_version": 1, "state": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Local configuration: {result['state']} (schema {result['current_schema_version']})")
        if result["backup"]:
            print(f"Backup: {result['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
