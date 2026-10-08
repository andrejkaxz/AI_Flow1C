"""Markdown selection, ranking and bounded knowledge responses without I/O."""
from __future__ import annotations

import base64
import json
import re
from pathlib import PurePosixPath
from typing import Any

MAX_RESPONSE_CHARS = 8000
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 1000
MAX_SCAN_BYTES = 8 * 1024 * 1024
CARD_SECTIONS = ("Назначение", "Текущее поведение", "Ограничения", "История изменений", "Источники и проверки")


def wiki_path(value: str, *, writable: bool = False) -> str:
    if not isinstance(value, str) or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Wiki path must be a relative POSIX path")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Wiki path must be contained")
    if path.suffix != ".md" or len(value) > 240:
        raise ValueError("Wiki accepts bounded Markdown paths only")
    if writable and value != "README.md" and not re.fullmatch(r"features/[a-z0-9][a-z0-9-]{0,79}\.md", value):
        raise ValueError("Write only README.md or features/<stable-slug>.md")
    return value


def markdown_info(text: str) -> dict[str, str]:
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
    aliases = next((line.split(":", 1)[1].strip() for line in text.splitlines()
                    if line.casefold().startswith(("альтернативные термины:", "aliases:"))), "")
    state = "UNVERIFIED_DRAFT" if "UNVERIFIED_DRAFT" in text else "UNSPECIFIED"
    if re.search(r"(?im)^(?:>\s*)?(?:статус|status):\s*(?:отменено|отменён|cancelled)\s*$", text):
        state = "CANCELLED"
    return {"title": title[:240], "aliases": aliases[:500], "document_state": state}


def rank_match(query: str, text: str, path: str) -> tuple[int, str] | None:
    info = markdown_info(text)
    needle = query.strip().casefold()
    if not needle:
        return (0 if path == "README.md" else 1, text[:300])
    for rank, value in enumerate((info["title"], info["aliases"], text)):
        index = value.casefold().find(needle)
        if index >= 0:
            return rank, value[max(0, index - 100):index + len(needle) + 200]
    return None


def section_range(text: str, section: str) -> tuple[int, int]:
    if not section:
        return 0, len(text)
    lines = text.splitlines(keepends=True)
    offset, start, level = 0, None, 0
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            if start is not None and len(match[1]) <= level:
                return start, offset
            if start is None and match[2].casefold() == section.casefold():
                start, level = offset, len(match[1])
        offset += len(line)
    if start is None:
        raise ValueError("Knowledge section not found")
    return start, len(text)


def encode_cursor(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).decode()


def decode_cursor(value: str) -> dict[str, Any]:
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError()
        result = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid knowledge continuation") from exc


def response_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def response_limit(value: Any) -> int:
    if type(value) is not int or not 2000 <= value <= MAX_RESPONSE_CHARS:
        raise ValueError("max_chars must be an integer between 2000 and 8000")
    return value


def card_content(path: str, content: str) -> str:
    wiki_path(path, writable=True)
    if not isinstance(content, str) or not content.strip() or "\x00" in content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Wiki content must be nonempty bounded Markdown")
    if not markdown_info(content)["title"]:
        raise ValueError("Wiki content requires a title")
    if path.startswith("features/"):
        for section in CARD_SECTIONS:
            section_range(content, section)
        if "UNVERIFIED_DRAFT" not in content:
            content = "> **UNVERIFIED_DRAFT** — предложение для review; даты внедрения не подтверждаются Git.\n\n" + content
    return content.rstrip() + "\n"


def markdown_cell(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value))[:240]
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def managed_links(text: str, prefix: str) -> str:
    start, end = "<!-- flow1c:navigation:start -->", "<!-- flow1c:navigation:end -->"
    block = (f"{start}\n\n## Результаты и знания проекта\n\n"
             f"- [Документы и результаты]({prefix}wiki/results.md)\n"
             f"- [База знаний: функции и история]({prefix}wiki/README.md)\n"
             f"- [Задачи и согласования]({prefix}wiki/status.md)\n\n{end}")
    if start in text or end in text:
        if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
            raise ValueError("Navigation markers are invalid; preserve README")
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        return before + block + after
    return text.rstrip() + "\n\n" + block + "\n"
