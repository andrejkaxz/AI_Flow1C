"""Optional local renderer boundary. Structural success never implies visual QA."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RendererCapabilities:
    name: str
    version: str
    license: str
    supported_formats: tuple[str, ...]
    qualified_windows: bool


class LocalRenderer(Protocol):
    capabilities: RendererCapabilities

    def render(self, source: Path, output_root: Path, *, timeout_seconds: int) -> list[Path]: ...


def render_for_review(source: Path, output_root: Path, renderer: LocalRenderer | None = None,
                      *, timeout_seconds: int = 60) -> dict:
    """Produce review pages only through an explicitly supplied qualified adapter.

    The adapter must implement a hard process timeout and never fetch links.
    Page production is not a visual pass. Agent/human page review remains separate.
    """
    if renderer is None:
        return {"layout_state": "UNVERIFIED", "pages": [],
                "next_action": "Review pages using a locally approved Windows renderer; none is qualified in this release."}
    capabilities = renderer.capabilities
    if not capabilities.qualified_windows or "docx" not in capabilities.supported_formats:
        return {"layout_state": "UNVERIFIED", "pages": [], "next_action": "Qualify this renderer on supported Windows fixtures first."}
    if not 1 <= timeout_seconds <= 120:
        raise ValueError("Renderer timeout must be 1..120 seconds")
    try:
        pages = renderer.render(source, output_root, timeout_seconds=timeout_seconds)
        output_root = output_root.resolve()
        if not pages or any(not page.is_file() or output_root not in page.resolve().parents for page in pages):
            raise ValueError("Renderer returned missing or uncontained pages")
        return {"layout_state": "UNVERIFIED", "pages": [str(p) for p in pages],
                "renderer": {"name": capabilities.name, "version": capabilities.version, "license": capabilities.license},
                "next_action": "Review every page for clipping, tables, lists, headers, images and readability before recording visual QA."}
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return {"layout_state": "FAILED", "pages": [], "next_action": "Repair the renderer and repeat local page review; preserve the draft."}
