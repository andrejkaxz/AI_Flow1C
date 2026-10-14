"""Project-local memory persistence with checked paths and one writer lock."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from flow1c import documentation, navigation, storage
from flow1c.errors import WorkflowError
from flow1c.memory_policy import MAX_RECORDS, MAX_SCAN_BYTES, ProjectMemoryError, memory_id

Project = tuple[Path, Path, str]


def project(*, product_root: Path) -> Project:
    root, data = navigation.project_paths(product_root)
    local = storage.read_json(product_root / ".flow1c.local.json", {})
    reference = str(local.get("project_reference") or "")
    identity = hashlib.sha256(json.dumps({"root": str(root).casefold(), "reference": reference},
                                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    directory = documentation.regular_path(data / ".workspace" / "memory")
    if not storage.path_is_within(directory, root):
        raise ProjectMemoryError("MEMORY_SCOPE: memory root escapes documentation")
    return root, directory, identity


def record_path(directory: Path, identifier: str) -> Path:
    target = documentation.regular_path(directory / (memory_id(identifier) + ".json"))
    if not storage.path_is_within(target, directory):
        raise ProjectMemoryError("MEMORY_SCOPE: record path escapes memory root")
    return target


def _ignore(root: Path, directory: Path) -> None:
    git_marker = documentation.regular_path(root / ".git")
    if not git_marker.exists():
        return
    path = documentation.regular_path(root / ".gitignore")
    if path.exists() and not path.is_file():
        raise ProjectMemoryError("MEMORY_INVALID: .gitignore is not a file")
    relative = directory.relative_to(root).as_posix()
    entry = "/" + relative + "/"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if entry not in current.splitlines():
        storage.atomic_write_bytes(path, (current.rstrip("\r\n") + "\n" + entry + "\n").encode("utf-8"))


@contextmanager
def writer(root: Path, directory: Path) -> Iterator[None]:
    """Use a process lock; keep the lock file so another writer cannot race its unlink."""
    documentation.regular_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = documentation.regular_path(directory / ".writer.lock")
    with lock.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            _ignore(root, directory)
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read(directory: Path, identifier: str, project_id: str) -> dict[str, Any]:
    path = record_path(directory, identifier)
    if not path.is_file() or path.stat().st_size > 16_384:
        raise ProjectMemoryError("MEMORY_MISSING: record is absent or exceeds the file limit")
    try:
        value = storage.read_json(path)
    except WorkflowError as exc:
        raise ProjectMemoryError("MEMORY_CORRUPT: invalid record JSON") from exc
    validate(value, identifier, project_id)
    return value


def validate(value: Any, identifier: str, project_id: str) -> None:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ProjectMemoryError("MEMORY_VERSION: unsupported record schema")
    if value.get("memory_id") != identifier or value.get("project_id") != project_id:
        raise ProjectMemoryError("MEMORY_SCOPE: record belongs to another project or ID")
    fields = {"schema_version", "memory_id", "revision", "project_id", "kind", "subject", "aliases",
              "text", "basis", "source", "origin_gate_id", "created_at", "updated_at", "state"}
    source = value.get("source")
    source_fields = {"type", "path", "locator", "sha256", "gate_id", "entry_id", "answer_index", "note_index"}
    from flow1c import memory_policy as policy
    if (set(value) != fields or type(value.get("revision")) is not int or value["revision"] < 1
            or not isinstance(value.get("kind"), str) or value["kind"] not in policy.KINDS
            or not isinstance(value.get("basis"), str) or value["basis"] not in policy.BASES
            or value.get("state") != "WORKING" or not isinstance(source, dict)
            or not {"type", "path", "locator", "sha256", "gate_id"} <= set(source)
            or set(source) - source_fields or not isinstance(source.get("type"), str)
            or policy.SOURCE_BASIS.get(source["type"]) != value["basis"]
            or not isinstance(source.get("path"), str) or len(source["path"]) > 500
            or source["path"].startswith("/") or "\\" in source["path"] or ":" in source["path"]
            or (source["path"] != "" and any(part in {"", ".", ".."} for part in source["path"].split("/")))
            or source["type"] == "context_entry" and (not source["path"] or not isinstance(source.get("entry_id"), str)
                or len(source["entry_id"]) > 100)
            or source["type"] != "context_entry" and source["path"] != ""
            or source["type"] == "dialogue_answer" and (type(source.get("answer_index")) is not int or source["answer_index"] < 0)
            or source["type"] == "dialogue_note" and (type(source.get("note_index")) is not int or source["note_index"] < 0)
            or not isinstance(source.get("locator"), str) or not 0 < len(source["locator"]) <= 200
            or not isinstance(source.get("sha256"), str) or len(source["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in source["sha256"])
            or not isinstance(source.get("gate_id"), str) or not policy.ID_PATTERN.fullmatch(source["gate_id"])
            or not isinstance(value.get("origin_gate_id"), str) or not policy.ID_PATTERN.fullmatch(value["origin_gate_id"])
            or not isinstance(value.get("created_at"), str) or not isinstance(value.get("updated_at"), str)
            or not isinstance(value.get("aliases"), list) or len(value["aliases"]) > 8
            or any(not isinstance(alias, str) or not 0 < len(alias) <= 80 for alias in value["aliases"])
            or not isinstance(value.get("subject"), str) or not 0 < len(value["subject"]) <= 160
            or not isinstance(value.get("text"), str) or not 0 < len(value["text"]) <= 800):
        raise ProjectMemoryError("MEMORY_CORRUPT: record does not match schema")


def scan(directory: Path, project_id: str) -> tuple[list[dict[str, Any]], bool, list[str]]:
    if not directory.exists():
        return [], True, []
    documentation.regular_path(directory)
    records, warnings, total = [], [], 0
    paths = sorted(directory.iterdir(), key=lambda p: p.name)
    for path in paths:
        try:
            documentation.regular_path(path)
            if path.suffix != ".json":
                continue
            size = path.stat().st_size
            if len(records) + len(warnings) >= MAX_RECORDS or total + size > MAX_SCAN_BYTES:
                warnings.append("MEMORY_SCAN_LIMIT")
                break
            total += size
            records.append(read(directory, path.stem, project_id))
        except (OSError, WorkflowError) as exc:
            warnings.append(f"{path.name}: {exc}")
    return records, not warnings, warnings[:5]


def put(directory: Path, value: dict[str, Any]) -> None:
    path = record_path(directory, value["memory_id"])
    storage.write_json(path, value)


def new_id() -> str:
    return str(uuid.uuid4())
