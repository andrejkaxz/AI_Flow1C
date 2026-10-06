"""Deterministic Redmine input and URL policy, independent from network I/O."""

from __future__ import annotations

import re
import datetime as dt
from html.parser import HTMLParser
import urllib.parse
from pathlib import Path
from typing import Any


class RedminePolicyError(ValueError):
    """Input rejected by the Redmine integration policy."""


def normalize_base_url(value: str) -> str:
    """Validate a Redmine URL and return its stable, credential-safe form."""
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise RedminePolicyError("Redmine URL is malformed or has an invalid port.") from exc
    if parsed.scheme.casefold() != "https":
        raise RedminePolicyError("Redmine URL must use HTTPS so the API key is encrypted in transit.")
    if not hostname or "@" in parsed.netloc:
        raise RedminePolicyError("Redmine URL must include a host and must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise RedminePolicyError("Redmine URL must not contain a query string or fragment.")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", parsed.netloc, path, "", ""))


def issue_number(value: str | int) -> int:
    raw = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", raw):
        raise RedminePolicyError("Redmine issue number must be a positive integer.")
    return int(raw)


def relation_label(relation_type: str, direction: str) -> str:
    """Name a Redmine relation from the requested issue's perspective."""
    labels = {
        "relates": ("Связана с", "Связана с"),
        "duplicates": ("Дублирует", "Дублируется задачей"),
        "duplicated": ("Дублируется задачей", "Дублирует"),
        "blocks": ("Блокирует", "Заблокирована задачей"),
        "blocked": ("Заблокирована задачей", "Блокирует"),
        "precedes": ("Предшествует", "Следует за"),
        "follows": ("Следует за", "Предшествует"),
        "copied_to": ("Скопирована в", "Скопирована из"),
        "copied_from": ("Скопирована из", "Скопирована в"),
    }
    pair = labels.get(relation_type)
    if pair is None:
        return relation_type
    return pair[0] if direction == "outgoing" else pair[1]


def dmsf_identifier(value: Any, *, label: str = "DMSF ID") -> int:
    """Validate an API identifier without accepting bools, signs, or coercions."""
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(value or "")):
        raise RedminePolicyError(f"{label} must be a positive integer.")
    return int(value)


class _IssueDmsfParser(HTMLParser):
    """Read file links only from the issue's DMS attachment container."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.issue_depth = 0
        self.section_found = False
        self.files: dict[int, dict[str, Any]] = {}

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if self.issue_depth and tag not in self._VOID_TAGS:
            self.issue_depth += 1
        elif tag == "div" and {"issue", "details"} <= classes:
            self.issue_depth = 1
        if self.depth and tag not in self._VOID_TAGS:
            self.depth += 1
        elif self.issue_depth and tag == "div" and {"attachments", "dmsf-parent-container"} <= classes:
            self.section_found = True
            self.depth = 1
        if not self.depth or tag != "a" or "dmsf-icon-file" not in classes:
            return
        match = re.fullmatch(r"/dmsf/files/([1-9][0-9]{0,9})/view", values.get("href") or "")
        if match:
            file_id = int(match.group(1))
            self.files[file_id] = {"id": file_id, "name": ""}

    def handle_endtag(self, tag: str) -> None:
        if self.depth and tag not in self._VOID_TAGS:
            self.depth -= 1
        if self.issue_depth and tag not in self._VOID_TAGS:
            self.issue_depth -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)


def issue_html_dmsf_file_ids(html: str) -> list[dict[str, Any]]:
    """Find DMSF links rendered in the task's dedicated DMS section."""
    parser = _IssueDmsfParser()
    parser.feed(html)
    parser.close()
    return list(parser.files.values())


def issue_html_has_dmsf_section(html: str) -> bool:
    """Return whether the issue page exposes its dedicated DMSF attachment target."""
    parser = _IssueDmsfParser()
    parser.feed(html)
    parser.close()
    return parser.section_found


def current_dmsf_file_ids(journals: Any) -> list[dict[str, Any]]:
    """Return DMSF files still attached after replaying issue journal details."""
    if journals is None:
        return []
    if not isinstance(journals, list):
        raise RedminePolicyError("Redmine issue has an invalid journal list.")
    def journal_time(journal: Any) -> dt.datetime:
        raw = str(journal.get("created_on", "")) if isinstance(journal, dict) else ""
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    ordered_journals = sorted(
        enumerate(journals),
        key=lambda pair: (
            journal_time(pair[1]),
            int(pair[1].get("id", 0)) if isinstance(pair[1], dict)
            and str(pair[1].get("id", "0")).isdigit() and len(str(pair[1].get("id", "0"))) <= 10 else 0,
            pair[0],
        ),
    )
    state: dict[int, dict[str, Any]] = {}
    for _, journal in ordered_journals:
        if not isinstance(journal, dict):
            continue
        details = journal.get("details", [])
        if not isinstance(details, list):
            continue
        for position, detail in enumerate(details):
            if not isinstance(detail, dict) or detail.get("property") != "dmsf_file":
                continue
            try:
                file_id = dmsf_identifier(detail.get("name"), label="DMSF ID in issue journal")
            except RedminePolicyError as exc:
                raise RedminePolicyError(f"REDMINE_DMSF_INVALID_ID: {exc}") from exc
            old_value = str(detail.get("old_value") or "").strip()
            new_value = str(detail.get("new_value") or "").strip()
            record = state.setdefault(file_id, {"id": file_id, "attached": False, "name": "", "position": position})
            if not old_value and new_value:
                record.update(attached=True, name=new_value)
            elif old_value and not new_value:
                record.update(attached=False, name=old_value)
            elif old_value and new_value:
                record.update(attached=True, name=new_value)
            record["position"] = position
    return [record for record in state.values() if record["attached"]]


def safe_attachment_name(value: str, attachment_id: Any = None) -> str:
    """Reduce a remote filename to a single safe Windows/POSIX filename."""
    name = str(value or "attachment").replace("\\", "/").split("/")[-1]
    name = "".join("_" if ord(char) < 32 or char in '<>:"|?*' else char for char in name)
    name = name.strip().rstrip(".")
    if not name or name in {".", ".."}:
        name = "attachment"
    stem = name.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = f"_{name}"
    if len(name) > 120:
        suffix = Path(name).suffix[:20]
        name = name[: 120 - len(suffix)] + suffix
    if attachment_id is not None:
        tag = re.sub(r"[^A-Za-z0-9_-]", "", str(attachment_id))[:24]
        if tag:
            path = Path(name)
            name = f"{path.stem}-{tag}{path.suffix}"
    return name


def url_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise RedminePolicyError("URL is malformed or has an invalid port.") from exc
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port
