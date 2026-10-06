"""File service for the interview gate, independent of the main CLI globals."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

try:
    from flow1c_interview_policy import InterviewError
    from flow1c_interview_workbook import audit_book, build_book, load_book, read_register
except ModuleNotFoundError:
    from scripts.flow1c_interview_policy import InterviewError
    from scripts.flow1c_interview_workbook import audit_book, build_book, load_book, read_register


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contained_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ":" in relative:
        raise InterviewError("INTERVIEW_PATH_FORBIDDEN", "Нужен относительный путь внутри текущего запроса.")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve():
        raise InterviewError("INTERVIEW_PATH_FORBIDDEN", "Путь выходит за каталог текущего запроса.")
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise InterviewError("INTERVIEW_PATH_FORBIDDEN", "Ссылки и reparse points не допускаются.")
    return path.resolve()


def verified_source(root: Path, relative: Any, artifacts: list[dict], changed_files: list[dict]) -> Path:
    path = contained_file(root, relative)
    records = [*artifacts, *changed_files]
    normalized = str(path.relative_to(root.resolve())).replace("\\", "/")
    record = next((item for item in reversed(records) if item.get("path") == normalized), None)
    if not record or not path.is_file() or digest(path) != record.get("sha256"):
        raise InterviewError("INTERVIEW_SOURCE_UNVERIFIED", "Книга отсутствует среди принятых или записанных файлов либо изменилась.",
                             "Примите выбранную пользователем книгу через flow1c_intake и используйте возвращённый path; повторно примите изменённый файл.")
    return path


def execute(action: str, request: dict, root: Path, artifacts: list[dict], changed_files: list[dict]) -> dict:
    if not isinstance(request, dict):
        raise InterviewError("INTERVIEW_INVALID_INPUT", "request должен быть JSON-объектом.")
    for field in ("source", "sheet", "output_name"):
        if field in request and (not isinstance(request[field], str) or not request[field].strip()):
            raise InterviewError("INTERVIEW_INVALID_INPUT", f"{field} должен быть непустой строкой.")
    source = verified_source(root, request["source"], artifacts, changed_files) if request.get("source") else None
    if action in {"inspect", "audit"}:
        if not source or set(request) - {"source", "sheet", "offset", "limit"}:
            raise InterviewError("INTERVIEW_INVALID_INPUT", "Просмотр требует source; допустимы sheet, offset и limit.")
        book = load_book(source)
        try:
            audit = audit_book(book, request.get("sheet"))
            result = {"state": "INSPECTED" if action == "inspect" else "AUDITED", "audit": audit}
            if action == "inspect":
                offset, limit = request.get("offset", 0), request.get("limit", 100)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
                    raise InterviewError("INTERVIEW_INVALID_INPUT", "offset >= 0; limit — целое от 1 до 200.")
                _, _, rows, _ = read_register(book, request.get("sheet"))
                result.update(rows=rows[offset:offset + limit], offset=offset, total=len(rows), next_offset=offset + limit if offset + limit < len(rows) else None)
            return result
        finally:
            book.close()
    if action != "write":
        raise InterviewError("INTERVIEW_INVALID_INPUT", "Действие должно быть inspect, audit или write.")
    source_digest = digest(source) if source else None
    request_hash = hashlib.sha256(json.dumps({"request": request, "source_sha256": source_digest},
                                            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    name = request.get("output_name", f"interview-{request_hash[:16]}.xlsx")
    if not isinstance(name, str) or not re.fullmatch(r"[\w][\w .-]{0,120}\.xlsx", name, re.IGNORECASE) or name.endswith(" .xlsx"):
        raise InterviewError("INTERVIEW_PATH_FORBIDDEN", "output_name должен быть простым именем XLSX без каталогов.")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise InterviewError("INTERVIEW_PATH_FORBIDDEN", "Имя зарезервировано Windows; выберите другое output_name.")
    target = contained_file(root, name)
    metadata_path = contained_file(root, name + ".interview.json")
    if target.exists() or metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InterviewError("INTERVIEW_OUTPUT_EXISTS", "Файл уже существует; выберите другое output_name.") from exc
        if (not isinstance(metadata, dict) or metadata.get("path") != name or metadata.get("schema_version") != 1
                or metadata.get("document_status") != "UNVERIFIED_DRAFT"
                or metadata.get("request_sha256") != request_hash or not target.is_file() or digest(target) != metadata.get("sha256")):
            raise InterviewError("INTERVIEW_OUTPUT_EXISTS", "Выбранная книга уже существует или изменена; она сохранена.", "Выберите другое output_name для новой версии.")
        return {"state": "WRITTEN", "absolute_path": str(target), "record": metadata, "reused": True}
    book, sheet, _ = build_book(request, source)
    root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        handle, temporary_name = tempfile.mkstemp(prefix=".interview-", suffix=".xlsx", dir=root)
        os.close(handle)
        temporary = Path(temporary_name)
        book.save(temporary)
        book.close()
        reopened = load_book(temporary)
        try:
            audit = audit_book(reopened, sheet)
        finally:
            reopened.close()
        if not audit["ready"]:
            raise InterviewError("INTERVIEW_AUDIT_FAILED", "Проверка записанной книги не прошла: " + ", ".join(audit["errors"]))
        if source and digest(source) != source_digest:
            raise InterviewError("INTERVIEW_SOURCE_CHANGED", "Исходная книга изменилась во время подготовки; примите актуальную версию заново.")
        output_digest = digest(temporary)
        # Exclusive creation works on Windows and network filesystems without hard links.
        # Evidence is published only after the complete, audited bytes are flushed.
        with target.open("xb") as output, temporary.open("rb") as saved:
            shutil.copyfileobj(saved, output)
            output.flush()
            os.fsync(output.fileno())
        if digest(target) != output_digest:
            raise InterviewError("INTERVIEW_OUTPUT_CHANGED", "Результат изменился во время записи; создайте новую проверенную версию.")
        metadata = {"schema_version": 1, "path": name, "sha256": output_digest, "request_sha256": request_hash,
                    "document_status": "UNVERIFIED_DRAFT", "sheet": sheet, "audit": audit,
                    "source": request.get("source"), "source_sha256": source_digest}
        with metadata_path.open("x", encoding="utf-8") as output:
            json.dump(metadata, output, ensure_ascii=False, indent=2)
            output.write("\n")
        return {"state": "WRITTEN", "absolute_path": str(target), "record": metadata, "reused": False}
    except OSError as exc:
        raise InterviewError("INTERVIEW_WRITE_FAILED", f"Не удалось сохранить копию: {exc}",
                             "Исходник сохранён. Проверьте доступ/свободное место; при оставшемся результате выберите новое output_name.") from exc
    finally:
        book.close()
        if temporary:
            temporary.unlink(missing_ok=True)


def completion(root: Path, record: dict, changed_files: list[dict]) -> Path:
    target = verified_source(root, record.get("path"), [], changed_files)
    if digest(target) != record.get("sha256") or record.get("document_status") != "UNVERIFIED_DRAFT":
        raise InterviewError("INTERVIEW_OUTPUT_CHANGED", "Черновик изменился после проверки; создайте новую проверенную версию.")
    book = load_book(target)
    try:
        audit = audit_book(book, record.get("sheet"))
        if not audit["ready"] or "UNVERIFIED_DRAFT" not in (book.properties.description or ""):
            raise InterviewError("INTERVIEW_AUDIT_FAILED", "Черновик не соответствует сохранённой проверке.")
    finally:
        book.close()
    return target
