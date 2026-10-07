"""Documentation layout versions and scaffold projections without I/O."""
from __future__ import annotations

from pathlib import Path
from typing import Any

LAYOUT_FILE = ".flow1c/layout.json"
SERVICE_FOLDERS = frozenset({"inbox", "registry", "work-items", "drafts", "requests",
                             "document-templates", "wiki", ".workspace"})


def validate_layout(value: Any) -> int:
    if (not isinstance(value, dict) or set(value) != {"schema_version", "layout_version"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["layout_version"]) is not int or value["layout_version"] not in {1, 2}):
        raise ValueError("Unsupported documentation layout; preserved without rewriting")
    return value["layout_version"]


def scaffold_path(relative: Path, version: int) -> Path:
    return Path(".flow1c") / relative if version == 2 and relative.parts[0] in SERVICE_FOLDERS else relative


def render_scaffold(content: str, version: int) -> str:
    description = (
        "Каталог `.flow1c/` и его подпапки обслуживает Flow1C"
        if version == 2 else
        "Папки `inbox`, `registry`, `work-items`, `drafts`, `requests`, "
        "`document-templates`, `wiki` и `.workspace` обслуживает Flow1C"
    )
    return (content.replace("{{service-prefix}}", ".flow1c/" if version == 2 else "")
            .replace("{{documentation-prefix}}", "../../" if version == 2 else "../")
            .replace("{{service-description}}", description))
