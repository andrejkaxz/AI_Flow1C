"""Read existing project results and refresh their non-destructive Markdown catalog."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flow1c import context, documentation, storage
from flow1c.errors import WorkflowError
from flow1c.knowledge_policy import MAX_FILE_BYTES, MAX_FILES, MAX_SCAN_BYTES, markdown_cell, markdown_info, managed_links

RESULT_EXTENSIONS = {".md", ".docx", ".xlsx", ".pdf"}
EXCLUDED = {"input", "sources", "evidence", "context", "handoffs", ".workspace", ".git", "__pycache__"}


def project_paths(product_root: Path) -> tuple[Path, Path]:
    local = storage.read_json(product_root / context.LOCAL_CONFIG_FILE, {})
    raw = local.get("documentation_path")
    if not isinstance(raw, str) or not raw.strip() or not Path(raw).is_absolute():
        raise WorkflowError("A configured absolute documentation_path is required")
    root = documentation.regular_path(Path(raw))
    if not root.is_dir():
        raise WorkflowError("Configured documentation directory is unavailable")
    return root, documentation.data_root(root)


def _text(path: Path) -> str:
    documentation.regular_path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise WorkflowError("Navigation metadata exceeds its file limit")
    return path.read_text(encoding="utf-8-sig")


def _record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_text(path))
    except (ValueError, UnicodeError) as exc:
        raise WorkflowError("Navigation metadata is invalid") from exc
    if not isinstance(value, dict):
        raise WorkflowError("Navigation metadata must be an object")
    return value


def _template_text(text: str, originals: set[str]) -> bool:
    if text.strip() in originals:
        return True
    # Headings, watermarks and empty tables alone are not a result.
    table_seen = False
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith(("#", ">", "<!--")):
            continue
        if value.startswith("|"):
            cells = [cell.strip() for cell in value.strip("|").split("|")]
            if cells and all(not cell or set(cell) <= {"-", ":"} for cell in cells):
                table_seen = True
                continue
            if table_seen and any(cells):
                return False
            continue
        if "{{" not in value:
            return False
    return True


def inventory(*, product_root: Path) -> dict[str, Any]:
    root, data = project_paths(product_root)
    originals = {p.read_text(encoding="utf-8-sig").strip()
                 for p in (product_root / "templates/work-item").rglob("*.md")}
    tasks, items, warnings = [], [], []
    scanned, scanned_bytes, directories, truncated = 0, 0, 0, False
    metadata_cache: dict[Path, dict[str, Any]] = {}
    def walk_error(error: OSError) -> None:
        raise WorkflowError("Navigation source directory is unavailable") from error
    for area in ("work-items", "drafts"):
        base = documentation.regular_path(data / area)
        if not base.exists():
            continue
        for directory, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
            directories += 1
            if directories > MAX_FILES or scanned >= MAX_FILES or scanned_bytes >= MAX_SCAN_BYTES:
                truncated = True
                break
            # Validate before pruning: links never become navigation destinations.
            for name in [*dirs, *names]:
                documentation.regular_path(Path(directory) / name)
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
            folder = Path(directory)
            relative = folder.relative_to(base)
            if not relative.parts:
                continue
            owner = base / relative.parts[0]
            metadata_file = owner / ("manifest.yaml" if area == "work-items" else "request.json")
            metadata: dict[str, Any] = metadata_cache.get(metadata_file, {})
            if metadata_file not in metadata_cache and metadata_file.is_file():
                documentation.regular_path(metadata_file)
                try:
                    metadata = _record(metadata_file)
                except WorkflowError:
                    warnings.append({"path": metadata_file.relative_to(root).as_posix(), "reason": "invalid-metadata"})
                metadata_cache[metadata_file] = metadata
            title = str(metadata.get("title") or metadata.get("summary") or relative.parts[0])[:240]
            reference = str(metadata.get("work_reference") or metadata.get("code") or "")[:240]
            kind = "work-item" if area == "work-items" and metadata else "draft" if area == "drafts" else "fragment"
            if folder == owner and kind == "work-item":
                tasks.append({"title": title, "reference": reference, "status": str(metadata.get("status", "unknown"))[:240],
                              "path": metadata_file.relative_to(root).as_posix()})
            if folder == owner and area == "drafts" and metadata.get("result_summary"):
                items.append({"title": title, "reference": reference, "kind": "consultation",
                              "status": str(metadata.get("state", "unknown"))[:240], "document_state": "SUMMARY_ONLY",
                              "path": metadata_file.relative_to(root).as_posix()})
            for name in sorted(names):
                scanned += 1
                if scanned > MAX_FILES:
                    truncated = True
                    break
                path = folder / name
                if path.suffix.lower() not in RESULT_EXTENSIONS or name == "README.md":
                    continue
                size = path.stat().st_size
                if size > MAX_FILE_BYTES and path.suffix.lower() == ".md":
                    warnings.append({"path": path.relative_to(root).as_posix(), "reason": "oversized-document"})
                    continue
                content = _text(path) if path.suffix.lower() == ".md" else ""
                scanned_bytes += len(content.encode("utf-8"))
                if scanned_bytes > MAX_SCAN_BYTES:
                    truncated = True
                    break
                info = markdown_info(content)
                state = "TEMPLATE" if size == 0 or content and _template_text(content, originals) else (
                    "UNVERIFIED_DRAFT" if kind == "draft" else info["document_state"]
                )
                items.append({"title": info["title"] or path.stem[:240], "task_title": title, "reference": reference,
                              "kind": kind, "status": str(metadata.get("status") or metadata.get("state") or "unknown")[:240],
                              "document_state": state, "path": path.relative_to(root).as_posix()})
            if truncated:
                break
        if truncated:
            break
    return {"schema_version": 1, "state": "CATALOG", "source": "local", "tasks": tasks, "items": items,
            "truncated": truncated, "scan_complete": not truncated and not warnings,
            "warnings": warnings, "scanned_files": scanned}


def render(value: dict[str, Any], wiki_relative: str) -> str:
    def link(path: str) -> str:
        relative = os.path.relpath(path, wiki_relative).replace("\\", "/")
        return quote(relative, safe="/.-_")

    lines = ["<!-- Generated by Flow1C: results catalog v1. -->", "# Документы и результаты", "",
             "Каталог локальных материалов. Наличие документа или завершённой операции не подтверждает согласование, внедрение или сохранение в Git.", "",
             "## Задачи", "", "| Название | Reference | Состояние | Источник |", "|---|---|---|---|"]
    for row in value["tasks"]:
        lines.append(f"| {markdown_cell(row['title'])} | {markdown_cell(row['reference'])} | {markdown_cell(row['status'])} | [Manifest]({link(row['path'])}) |")
    if not value["tasks"]:
        lines.append("| Зарегистрированных задач в просмотренной области нет | — | — | — |")
    lines += ["", "## Документы, черновики и фрагменты", "",
              "| Название | Задача | Reference | Вид | Состояние документа | Результат |", "|---|---|---|---|---|---|"]
    for row in value["items"]:
        lines.append("| " + " | ".join(markdown_cell(row.get(field, "")) for field in (
            "title", "task_title", "reference", "kind", "document_state")) + f" | [Открыть]({link(row['path'])}) |")
    if not value["items"]:
        lines.append("| Результатов в просмотренной области нет | — | — | — | — | — |")
    if not value["scan_complete"]:
        lines += ["", "> Каталог неполный: достигнут предел просмотра или найдены недоступные метаданные. Отсутствие строки не доказывает отсутствие результата."]
    lines += ["", "Обновление: попросите агента «Обнови каталог результатов проекта». Исходные документы и служебные записи не изменяются.", ""]
    return "\n".join(lines)


def refresh(*, product_root: Path) -> dict[str, Any]:
    root, data = project_paths(product_root)
    value = inventory(product_root=product_root)
    target = documentation.regular_path(data / "wiki/results.md")
    entry = documentation.regular_path(root / "README.md")
    if target.exists():
        if not target.is_file():
            raise WorkflowError("Navigation output is occupied by a directory")
        with target.open("rb") as stream:
            if not stream.read(64).startswith(b"<!-- Generated by Flow1C: results catalog v1. -->"):
                raise WorkflowError("User-authored wiki/results.md is preserved; choose another name before refresh")
    try:
        entry_content = managed_links(_text(entry) if entry.exists() else "# Документация проекта\n", ".flow1c/" if data != root else "")
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    wiki = documentation.regular_path(data / "wiki/README.md")
    writes = [(target, render(value, (data / "wiki").relative_to(root).as_posix())), (entry, entry_content)]
    if not wiki.exists():
        writes.append((wiki, "# База знаний проекта\n\n[Документы и результаты](results.md) · [Задачи и согласования](status.md)\n\n"
                       "## Функции\n\n| Функция | Альтернативные термины | Назначение | Карточка |\n|---|---|---|---|\n\n"
                       "Карточки хранят назначение, текущее поведение, ограничения, историю и источники. Новые предложения отмечаются UNVERIFIED_DRAFT.\n"))
    # Validate every output before performing the first mutation.
    for path, _ in writes:
        documentation.regular_path(path)
        if path.exists() and not path.is_file():
            raise WorkflowError("Navigation output is occupied by a directory")
    changed = []
    for path, content in writes:
        payload = content.encode("utf-8")
        if not path.exists() or path.read_bytes() != payload:
            storage.atomic_write_bytes(path, payload)
        changed.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(payload).hexdigest()})
    return {"schema_version": 1, "state": "REFRESHED", "changed_files": changed,
            "task_count": len(value["tasks"]), "result_count": len(value["items"]),
            "truncated": value["truncated"], "scan_complete": value["scan_complete"]}
