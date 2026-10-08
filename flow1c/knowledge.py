"""Project wiki source snapshots, bounded search/read and checked Markdown edits."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

from flow1c import context, documentation, navigation, storage
from flow1c.errors import WorkflowError
from flow1c import knowledge_policy as policy
from scripts import flow1c_git as git


def _git(root: Path, args: list[str], *, max_output: int = 2 * 1024 * 1024) -> bytes:
    try:
        result = git.run_git(root, args, max_output=max_output)
    except (git.GitAnalysisError, OSError) as exc:
        raise WorkflowError("Knowledge Git source is unavailable or exceeds its limit") from exc
    if result.returncode:
        raise WorkflowError("Knowledge Git source is unavailable; choose an existing ref or explicit source=local")
    return result.stdout


class WikiSource:
    def __init__(self, product_root: Path, request: dict[str, Any]) -> None:
        self.root, data = navigation.project_paths(product_root)
        self.source = request.get("source", "git")
        self.commit = ""
        if not isinstance(self.source, str) or self.source not in {"git", "local"}:
            raise WorkflowError("Knowledge source must be git or local")
        self.prefix = (data / "wiki").relative_to(self.root).as_posix()
        if self.source == "git":
            config, _ = context.load_config(product_root=product_root)
            self.ref = request.get("ref") or config.get("project", {}).get("default_branch", "main")
            if not isinstance(self.ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._-]{0,199}", self.ref) or ".." in self.ref:
                raise WorkflowError("Invalid knowledge Git ref")
            top = Path(_git(self.root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()).resolve()
            if top != self.root:
                raise WorkflowError("Documentation must have its own Git repository")
            self.commit = _git(self.root, ["rev-parse", "--verify", f"{self.ref}^{{commit}}"]).decode().strip()
            marker = _git(self.root, ["ls-tree", self.commit, "--", ".flow1c/layout.json"]).decode()
            if marker:
                if not marker.startswith(("100644 blob ", "100755 blob ")):
                    raise WorkflowError("Knowledge layout marker is not a regular Git blob")
                try:
                    version = documentation.validate_layout(json.loads(_git(self.root, ["show", f"{self.commit}:.flow1c/layout.json"], max_output=4096)))
                except (ValueError, UnicodeError) as exc:
                    raise WorkflowError("Knowledge snapshot has an invalid layout") from exc
                self.prefix = ".flow1c/wiki" if version == 2 else "wiki"
            else:
                self.prefix = "wiki"
        else:
            self.ref = ""
        self.wiki = documentation.regular_path(self.root / self.prefix)
        self.snapshot = {"source": self.source, "project": hashlib.sha256(str(self.root).casefold().encode()).hexdigest(),
                         "ref": self.ref, "commit": self.commit or None, "prefix": self.prefix}
        if request.get("snapshot") is not None and request["snapshot"] != self.snapshot:
            raise WorkflowError("Knowledge snapshot changed; repeat search/read for the current source")
        self.scan_complete = True
        self.skipped: list[dict[str, str]] = []

    def entries(self) -> Iterator[tuple[str, bytes]]:
        candidates: list[tuple[str, str, int]] = []
        if self.source == "git":
            tree = _git(self.root, ["ls-tree", "-r", "-l", "-z", self.commit, "--", self.prefix])
            for row in tree.split(b"\x00"):
                if not row:
                    continue
                metadata, raw_path = row.split(b"\t", 1)
                full_path = raw_path.decode("utf-8")
                relative = full_path[len(self.prefix) + 1:]
                if not full_path.startswith(self.prefix + "/") or not relative.endswith(".md"):
                    continue
                mode, kind, blob, size = metadata.decode().split()
                if mode not in {"100644", "100755"} or kind != "blob":
                    raise WorkflowError("Wiki snapshot contains a symlink or nonregular file")
                candidates.append((relative, blob, int(size)))
        elif self.wiki.exists():
            dirs_seen = 0
            def walk_error(error: OSError) -> None:
                raise WorkflowError("Knowledge source directory is unavailable") from error
            for folder, dirs, names in os.walk(self.wiki, followlinks=False, onerror=walk_error):
                dirs_seen += 1
                for name in [*dirs, *names]:
                    documentation.regular_path(Path(folder) / name)
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                candidates.extend(((Path(folder) / name).relative_to(self.wiki).as_posix(), "", (Path(folder) / name).stat().st_size)
                                  for name in sorted(names) if name.endswith(".md"))
                if len(candidates) > policy.MAX_FILES or dirs_seen > policy.MAX_FILES:
                    self.scan_complete = False
                    break
        if len(candidates) > policy.MAX_FILES:
            self.scan_complete = False
        scanned_bytes = 0
        for relative, blob, size in sorted(candidates)[:policy.MAX_FILES]:
            try:
                policy.wiki_path(relative)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
            path = documentation.regular_path(self.wiki / relative)
            if size > policy.MAX_FILE_BYTES:
                self.scan_complete = False
                if len(self.skipped) < 5:
                    self.skipped.append({"path": relative, "reason": "file-limit"})
                continue
            if scanned_bytes + size > policy.MAX_SCAN_BYTES:
                self.scan_complete = False
                break
            payload = _git(self.root, ["show", blob], max_output=policy.MAX_FILE_BYTES) if blob else path.read_bytes()
            scanned_bytes += len(payload)
            yield relative, payload

    def read(self, relative: str) -> bytes:
        try:
            policy.wiki_path(relative)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        path = documentation.regular_path(self.wiki / relative)
        if self.source == "git":
            entry = _git(self.root, ["ls-tree", "-l", "-z", self.commit, "--", f"{self.prefix}/{relative}"])
            if not entry:
                raise WorkflowError("Knowledge document not found at the selected ref")
            records = entry.rstrip(b"\x00").split(b"\x00")
            if len(records) != 1:
                raise WorkflowError("Knowledge path is not a regular document")
            metadata, raw_path = records[0].split(b"\t", 1)
            mode, kind, blob, size = metadata.decode().split()
            if raw_path.decode() != f"{self.prefix}/{relative}" or kind != "blob" or mode not in {"100644", "100755"}:
                raise WorkflowError("Knowledge path is not a regular document")
            if int(size) > policy.MAX_FILE_BYTES:
                raise WorkflowError("Knowledge document exceeds its file limit")
            return _git(self.root, ["show", blob], max_output=policy.MAX_FILE_BYTES)
        if not path.is_file() or path.stat().st_size > policy.MAX_FILE_BYTES:
            raise WorkflowError("Knowledge document is missing or exceeds its file limit")
        return path.read_bytes()


def _decode(payload: bytes) -> str:
    try:
        return payload.decode("utf-8-sig")
    except UnicodeError as exc:
        raise WorkflowError("Knowledge documents must be UTF-8 Markdown") from exc


def search(request: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    limit = policy.response_limit(request.get("max_chars", 8000))
    query = request.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        raise WorkflowError("Knowledge query must be a string of at most 200 characters")
    source = WikiSource(product_root, request)
    matches = []
    for path, payload in source.entries():
        text = _decode(payload)
        match = policy.rank_match(query, text, path)
        if match:
            rank, excerpt = match
            matches.append({"path": path, **policy.markdown_info(text), "excerpt": excerpt,
                            "version": hashlib.sha256(payload).hexdigest(), "rank": rank,
                            "state": "LOCAL_DRAFT" if source.source == "local" else "COMMITTED"})
    matches.sort(key=lambda item: (item["rank"], item["path"]))
    result = {"schema_version": 1, "state": "SEARCH", "snapshot": source.snapshot,
              "matches": matches[:5], "match_count": len(matches), "scan_complete": source.scan_complete,
              "truncated": len(matches) > 5 or not source.scan_complete, "skipped": source.skipped,
              "next_action": "Narrow the query for additional matches; incomplete search does not prove absence."}
    while result["matches"] and policy.response_size(result) > limit:
        result["matches"].pop()
        result["truncated"] = True
    return result


def read(request: dict[str, Any], *, product_root: Path) -> dict[str, Any]:
    limit = policy.response_limit(request.get("max_chars", 8000))
    source = WikiSource(product_root, request)
    path, section = request.get("path", ""), request.get("section", "")
    if not isinstance(section, str) or len(section) > 200:
        raise WorkflowError("Knowledge section must be bounded text")
    payload = source.read(path)
    version = hashlib.sha256(payload).hexdigest()
    if request.get("version") is not None and request["version"] != version:
        raise WorkflowError("Knowledge document changed; repeat search/read")
    text = _decode(payload)
    start, end = policy.section_range(text, section)
    offset = start
    start_line = request.get("start_line", 1)
    if type(start_line) is not int or start_line < 1:
        raise WorkflowError("start_line must be a positive integer")
    if start_line > 1:
        offset = max(start, sum(len(line) for line in text.splitlines(keepends=True)[:start_line - 1]))
    identity = {"snapshot": source.snapshot, "path": path, "version": version, "section": section}
    if request.get("cursor"):
        cursor = policy.decode_cursor(request["cursor"])
        if any(cursor.get(key) != value for key, value in identity.items()) or type(cursor.get("offset")) is not int:
            raise WorkflowError("Knowledge continuation belongs to another source or version")
        offset = cursor["offset"]
    if not start <= offset <= end:
        raise WorkflowError("Knowledge continuation is outside the selected section")
    result = {"schema_version": 1, "state": "READ", **identity, **policy.markdown_info(text),
              "source_state": "LOCAL_DRAFT" if source.source == "local" else "COMMITTED",
              "start_line": text[:offset].count("\n") + 1, "start_offset": offset,
              "content": "", "next_cursor": None, "truncated": False}
    def fill(take: int) -> None:
        result.update(content=text[offset:offset + take], end_offset=offset + take,
                      end_line=text[:offset + take].count("\n") + 1, truncated=offset + take < end,
                      next_cursor=policy.encode_cursor({**identity, "offset": offset + take}) if offset + take < end else None)
    fill(0)
    if policy.response_size(result) > limit:
        raise WorkflowError("Knowledge response metadata exceeds max_chars; use a larger limit")
    low, high = 0, min(end - offset, limit)
    while low < high:
        middle = (low + high + 1) // 2
        fill(middle)
        if policy.response_size(result) <= limit:
            low = middle
        else:
            high = middle - 1
    fill(low)
    if low == 0 and offset < end:
        raise WorkflowError("Knowledge response requires a larger max_chars")
    return result


def edit(request: dict[str, Any], *, product_root: Path, apply: bool = False) -> dict[str, Any]:
    root, data = navigation.project_paths(product_root)
    path = request.get("path", "")
    try:
        content = policy.card_content(path, request.get("content", ""))
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    target = documentation.regular_path(data / "wiki" / path)
    if target.exists() and (not target.is_file() or target.stat().st_size > policy.MAX_FILE_BYTES):
        raise WorkflowError("Wiki edit target is not a bounded regular file")
    before = target.read_bytes() if target.is_file() else b""
    if len(before) > policy.MAX_FILE_BYTES or target.exists() and not target.is_file():
        raise WorkflowError("Wiki edit target is not a bounded regular file")
    expected = hashlib.sha256(before).hexdigest() if target.exists() else "absent"
    if apply and request.get("expected_version") != expected:
        raise WorkflowError("Wiki edit source changed or no preview version was supplied")
    diff = "".join(difflib.unified_diff(_decode(before).splitlines(keepends=True), content.splitlines(keepends=True),
                                      fromfile=path, tofile=path))
    payload = content.encode("utf-8")
    if apply:
        if request.get("confirmed") is not True:
            raise WorkflowError("Writing wiki requires the user's explicit instruction")
        storage.atomic_write_bytes(target, payload)
    result = {"schema_version": 1, "state": "WRITTEN" if apply else "PREVIEW", "path": target.relative_to(root).as_posix(),
              "expected_version": expected, "version": hashlib.sha256(payload).hexdigest(),
              "diff": diff[:5000], "truncated": len(diff) > 5000,
              "document_state": policy.markdown_info(content)["document_state"]}
    while policy.response_size(result) > 8000:
        result["diff"] = result["diff"][:len(result["diff"]) // 2]
        result["truncated"] = True
    return result
