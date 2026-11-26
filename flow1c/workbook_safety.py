"""Identify macro-free workbook containers before opening them in Excel."""

from __future__ import annotations

from pathlib import Path
from struct import error as StructError, iter_unpack, unpack_from
from xml.etree.ElementTree import ParseError, fromstring
from zipfile import BadZipFile, ZipFile

from flow1c.workbook_format_policy import WorkbookFormatError

END_OF_CHAIN = 0xFFFFFFFE


def _uint(data: bytes, offset: int) -> int:
    return unpack_from("<I", data, offset)[0]


def _chain(allocation: list[int], first: int, capacity: int) -> list[int]:
    indices: list[int] = []
    seen: set[int] = set()
    while first != END_OF_CHAIN:
        if first >= min(len(allocation), capacity) or first in seen:
            raise ValueError("Invalid compound-file sector chain.")
        seen.add(first)
        indices.append(first)
        first = allocation[first]
    return indices


def _biff_workbook(data: bytes) -> bytes:
    # Excel 97–2003 uses a version 3 compound file with 512-byte sectors.
    if (len(data) < 1024 or data[:8] != bytes.fromhex("d0cf11e0a1b11ae1")
            or unpack_from("<HHH", data, 26) != (3, 0xFFFE, 9)
            or unpack_from("<H", data, 32)[0] != 6):
        raise ValueError("Expected a legacy Excel compound file.")
    sector_size = 512
    capacity = len(data) // sector_size - 1

    def sector(index: int) -> bytes:
        if index >= capacity:
            raise ValueError("Invalid compound-file sector.")
        return data[(index + 1) * sector_size:(index + 2) * sector_size]

    fat_count = _uint(data, 44)
    difat_sector_count = _uint(data, 72)
    if not 1 <= fat_count <= capacity or difat_sector_count > capacity:
        raise ValueError("Invalid compound-file allocation table.")
    difat = [_uint(data, 76 + 4 * index) for index in range(min(fat_count, 109))]
    extra_sector = _uint(data, 68)
    seen_difat: set[int] = set()
    for _ in range(difat_sector_count):
        if len(difat) >= fat_count:
            break
        if extra_sector in seen_difat:
            raise ValueError("Invalid compound-file allocation chain.")
        seen_difat.add(extra_sector)
        block = sector(extra_sector)
        difat.extend(index for (index,) in iter_unpack("<I", block[:-4]) if index != 0xFFFFFFFF)
        extra_sector = _uint(block, sector_size - 4)
    if len(difat) != fat_count or len(set(difat)) != fat_count:
        raise ValueError("Invalid compound-file allocation table.")
    fat = [index for block in difat for (index,) in iter_unpack("<I", sector(block))]
    entries = b"".join(sector(index) for index in _chain(fat, _uint(data, 48), capacity))
    streams = {}
    for offset in range(0, len(entries), 128):
        entry = entries[offset:offset + 128]
        if not entry[66]:
            continue
        name_length = unpack_from("<H", entry, 64)[0]
        if not 2 <= name_length <= 64 or name_length % 2:
            raise ValueError("Invalid compound-file directory entry.")
        name = entry[:name_length - 2].decode("utf-16le").casefold()
        if name in {"_vba_project_cur", "vba", "macros", "encryptedpackage", "encryptioninfo"}:
            raise ValueError("Macro-enabled or encrypted legacy workbook.")
        if name in {"root entry", "workbook", "book"}:
            streams[name] = (_uint(entry, 116), _uint(entry, 120))
    start, length = streams.get("workbook") or streams["book"]
    if length >= _uint(data, 56):
        return b"".join(sector(index) for index in _chain(fat, start, capacity))[:length]
    root_start, root_length = streams["root entry"]
    mini_stream = b"".join(sector(index) for index in _chain(fat, root_start, capacity))[:root_length]
    mini_fat = [index for block in _chain(fat, _uint(data, 60), capacity)
                for (index,) in iter_unpack("<I", sector(block))]
    return b"".join(mini_stream[index * 64:(index + 1) * 64]
                    for index in _chain(mini_fat, start, len(mini_stream) // 64))[:length]


def _check_biff(data: bytes) -> None:
    book = _biff_workbook(data)
    if unpack_from("<HHHH", book) != (0x0809, 16, 0x0600, 5):
        raise ValueError("Expected BIFF8 workbook globals.")
    offset = 0
    while offset + 4 <= len(book):
        record, size = unpack_from("<HH", book, offset)
        end = offset + 4 + size
        if end > len(book):
            break
        if record == 0x002F or (record == 0x0085 and (size < 8 or book[offset + 9] in (1, 6))):
            raise ValueError("Encrypted workbook or an Excel 4.0/VBA macro sheet.")
        if record == 0x000A:
            return
        offset = end
    raise ValueError("Incomplete workbook globals.")


def check_workbook(path: Path) -> None:
    try:
        if path.suffix.lower() == ".xls":
            _check_biff(path.read_bytes())
            return
        with ZipFile(path) as book:
            manifest = book.getinfo("[Content_Types].xml")
            if manifest.file_size > 1024 * 1024:
                raise ValueError("Workbook content types exceed the parsing limit.")
            types = [node.get("ContentType", "").casefold() for node in fromstring(book.read(manifest))]
            parts = {name.casefold() for name in book.namelist()}
            if "xl/workbook.xml" not in parts or not any(
                value == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
                for value in types
            ):
                raise ValueError("Expected an XLSX workbook.")
            if any(marker in value for value in types for marker in ("macroenabled", "macrosheet", "vbaproject")) or any(
                part.startswith(("xl/vbaproject", "xl/macrosheets/")) for part in parts
            ):
                raise ValueError("Macro-enabled workbook.")
    except (BadZipFile, KeyError, ValueError, StructError, ParseError) as exc:
        raise WorkbookFormatError("WORKBOOK_FILE", str(exc)) from exc
