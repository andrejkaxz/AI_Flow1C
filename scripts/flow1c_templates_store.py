"""Durable project library storage; explicit paths, integrity and exclusive commits."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator

try:
    from flow1c_templates_policy import TemplateError
except ModuleNotFoundError:
    from scripts.flow1c_templates_policy import TemplateError

MAX_JSON_BYTES = 16 * 1024 * 1024


def safe_path(path: Path, root: Path | None = None) -> Path:
    """Reject symbolic links and Windows reparse points in every existing ancestor."""
    absolute = Path(os.path.abspath(path))
    for node in (absolute, *absolute.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "Links/reparse points are forbidden in template paths.",
                                component="template-store", next_action="Use a regular directory without links/junctions.")
    resolved = absolute.resolve()
    if root is not None:
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "Path escapes the allowed root.", component="template-store") from exc
    return resolved


def documentation_root(value: str, workflow_root: Path) -> Path:
    if not value or not Path(value).is_absolute():
        raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "An explicit absolute documentation_path is required.",
                            next_action="Specify an existing user documentation folder outside all Workflow checkouts.")
    path = safe_path(Path(value))
    if not path.is_dir() or path == workflow_root.resolve() or workflow_root.resolve() in path.parents:
        raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "Documentation must be an accessible external folder.")
    for parent in (path, *path.parents):
        if (parent / "scripts/flow1c.py").is_file() and (parent / ".flow1c.json").is_file():
            raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "Documentation belongs to a Workflow checkout.")
    return path


def identifier(value: str) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9-]{8,64}", value):
        raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "Invalid generated identifier.")
    return value


def new_id() -> str:
    return str(uuid.uuid4())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with safe_path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    path = safe_path(path)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Stored JSON exceeds parsing budget.")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Unsupported stored schema; preserved without rewriting.",
                            next_action="Use a compatible Workflow version or an explicit migration.")
    return value


def atomic_json(path: Path, value: dict) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "JSON exceeds the storage/read budget; existing data is preserved.",
                            next_action="Reduce the profile or context size and resume the same operation.")
    atomic_bytes(path, data)


def atomic_bytes(path: Path, data: bytes) -> None:
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".flow1c-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def exclusive(root: Path) -> Iterator[None]:
    root = safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = safe_path(root / ".template-library.lock", root)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Another library operation holds the lock.",
                            next_action="Retry after it finishes; after a crash verify no writer runs before removing the lock.") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


class LibraryStore:
    def __init__(self, documentation: Path):
        self.documentation = safe_path(documentation)
        self.root = safe_path(documentation / "document-templates", documentation)

    def index(self, *, create: bool = False, project_reference: str | None = None) -> dict:
        path = safe_path(self.root / "library.json", self.root)
        if path.exists():
            return read_json(path)
        if not create:
            return {"schema_version": 1, "library_id": None, "templates": []}
        value = {"schema_version": 1, "library_id": new_id(), "project_reference": project_reference,
                 "storage_kind": "documentation", "templates": []}
        atomic_json(path, value)
        atomic_bytes(self.root / ".gitignore", b".state/\n.template-library.lock\n.flow1c-*.tmp\n")
        return value

    def operation_path(self, operation_id: str) -> Path:
        return safe_path(self.root / ".state" / "operations" / identifier(operation_id), self.root)

    def operation(self, operation_id: str) -> dict:
        return read_json(self.operation_path(operation_id) / "checkpoint.json")

    def checkpoint(self, value: dict) -> None:
        atomic_json(self.operation_path(value["operation_id"]) / "checkpoint.json", value)

    def revision_path(self, template_id: str, revision_id: str) -> Path:
        return safe_path(self.root / "templates" / identifier(template_id) / "revisions" / identifier(revision_id), self.root)

    def revision(self, pin: dict) -> tuple[Path, dict]:
        if self.index()["library_id"] != pin["library_id"]:
            raise TemplateError("TEMPLATE_REVISION_STALE", "Library identity changed.")
        root = self.revision_path(pin["template_id"], pin["revision_id"])
        manifest = read_json(root / "manifest.json")
        for key, actual in (("source_sha256", manifest["source_sha256"]), ("profile_sha256", manifest["hashes"]["profile.json"])):
            if pin.get(key) and pin[key] != actual:
                raise TemplateError("TEMPLATE_REVISION_STALE", "Revision hash changed after the request pin was saved.")
        if manifest["state"] != "VALIDATED" or manifest["document_type"] != pin["document_type"]:
            raise TemplateError("TEMPLATE_PROFILE_INCOMPLETE", "Revision is not validated for this document type.")
        for relative, expected in manifest["hashes"].items():
            path = safe_path(root / relative, root)
            if not path.is_file() or sha256(path) != expected:
                raise TemplateError("TEMPLATE_INTEGRITY_FAILED", f"Revision integrity check failed: {relative}.")
        return root, manifest

    def stable_copy(self, source: Path, destination: Path, maximum: int) -> str:
        source = safe_path(source)
        if not source.is_file() or source.stat().st_size > maximum:
            raise TemplateError("TEMPLATE_FORMAT_UNSUPPORTED", "Source is missing or exceeds the format size limit.")
        before = sha256(source)
        destination = safe_path(destination, self.root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, destination.open("xb") as writer:
            total = 0
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                total += len(block)
                if total > maximum:
                    raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Source grew during intake.")
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        if sha256(source) != before or sha256(destination) != before:
            raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Source changed during intake; no active revision was changed.")
        return before

    def relocate(self, destination: Path, operation_id: str) -> dict:
        """Keep the old library; resume partial transfer using a deterministic staging path."""
        target = safe_path(destination / "document-templates", destination)
        index = self.index()
        if self.documentation == destination.resolve():
            return {"library_id": index["library_id"], "root": str(self.root), "old_root": str(self.root), "old_copy_preserved": True}
        if self.root == destination.resolve() or self.root in destination.resolve().parents:
            raise TemplateError("TEMPLATE_STORAGE_UNAVAILABLE", "Relocation destination cannot be inside the source library.")
        if not index["library_id"]:
            raise TemplateError("TEMPLATE_SOURCE_REQUIRED", "There is no library to relocate.")
        staging = safe_path(destination / (".template-transfer-" + identifier(operation_id)), destination)
        with exclusive(self.root), exclusive(destination):
            if target.exists():
                if read_json(target / "library.json").get("library_id") != index["library_id"]:
                    raise TemplateError("TEMPLATE_REVISION_CONFLICT", "Destination already contains a different library.")
                compare = target
            else:
                staging.mkdir(exist_ok=True)
                for source in self.root.rglob("*"):
                    safe_path(source, self.root)
                    if source.name == ".template-library.lock":
                        continue
                    relative = source.relative_to(self.root)
                    copied = safe_path(staging / relative, staging)
                    if source.is_dir():
                        copied.mkdir(exist_ok=True, parents=True)
                    elif not copied.is_file() or sha256(copied) != sha256(source):
                        atomic_bytes(copied, source.read_bytes())
                compare = staging
            for source in self.root.rglob("*"):
                if source.is_file() and source.name != ".template-library.lock":
                    copied = safe_path(compare / source.relative_to(self.root), compare)
                    if not copied.is_file() or sha256(source) != sha256(copied):
                        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Relocation verification failed; old library preserved.")
            if compare == staging:
                os.rename(staging, target)
        return {"library_id": index["library_id"], "root": str(target), "old_root": str(self.root), "old_copy_preserved": True}
