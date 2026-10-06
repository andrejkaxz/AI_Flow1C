"""Shared filesystem primitives; no workflow decisions or CLI output."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from flow1c.errors import WorkflowError


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return sanitize_json_value(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read JSON {path}: {exc}") from exc


def sanitize_text(text: str) -> str:
    """Replace lone UTF-16 surrogates while preserving valid surrogate pairs."""
    result: list[str] = []
    index = 0
    while index < len(text):
        codepoint = ord(text[index])
        if 55296 <= codepoint <= 56319:
            if index + 1 < len(text):
                low = ord(text[index + 1])
                if 56320 <= low <= 57343:
                    result.append(chr(65536 + (codepoint - 55296 << 10) + (low - 56320)))
                    index += 2
                    continue
            result.append("�")
        elif 56320 <= codepoint <= 57343:
            result.append("�")
        else:
            result.append(text[index])
        index += 1
    return "".join(result)


def sanitize_json_value(value: Any) -> Any:
    """Return JSON-compatible data with no unpaired UTF-16 surrogates."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {
            sanitize_text(key) if isinstance(key, str) else key: sanitize_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple((sanitize_json_value(item) for item in value))
    return value


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write beside the target, flush completely, then atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def write_json(path: Path, value: Any) -> None:
    """Atomically replace a JSON file so a failed write cannot destroy valid state."""
    payload = json.dumps(sanitize_json_value(value), ensure_ascii=False, indent=2) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return True


def utc_now() -> str:
    import datetime as dt

    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
