"""Resolve template readiness for the Windows updater without writing project data."""

from __future__ import annotations

import json
from importlib.util import find_spec
from pathlib import Path

from scripts import flow1c_templates_store as templates


def resolve_profile(product_root: Path) -> str:
    config = json.loads((product_root / ".flow1c.local.json").read_text(encoding="utf-8-sig"))
    documentation = templates.documentation_root(config["documentation_path"], product_root)
    library = templates.LibraryStore(documentation)
    index = library.index()
    configured = config["template_library"]
    if configured.get("library_id") and configured["library_id"] != index["library_id"]:
        raise ValueError("Configured library identity is missing or changed.")

    needs_docx = bool(configured.get("require_word_for")) or find_spec("docx") is not None
    for variant in index["templates"]:
        if revision := variant["active_revision"]:
            manifest_path = library.revision_path(variant["template_id"], revision) / "manifest.json"
            manifest = templates.read_json(manifest_path)
            needs_docx |= manifest["source_format"] == "docx"
    return "template-docx" if needs_docx else "template-markdown"


def main() -> int:
    try:
        print(resolve_profile(Path(__file__).resolve().parents[1]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
