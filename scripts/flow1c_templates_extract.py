"""Format registry and process-isolated structural parsing (10 second hard budget)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

try:
    from flow1c_templates_policy import TemplateError
    from flow1c_templates_store import sha256
except ModuleNotFoundError:
    from scripts.flow1c_templates_policy import TemplateError
    from scripts.flow1c_templates_store import sha256

FORMATS = {".md": ("markdown", 10 * 1024 * 1024), ".docx": ("docx", 50 * 1024 * 1024)}
PARSING_TIMEOUT = 10


def source_format(path: Path) -> tuple[str, int]:
    if path.name.startswith("~$") or path.suffix.lower() not in FORMATS:
        raise TemplateError("TEMPLATE_FORMAT_UNSUPPORTED", "Only UTF-8 Markdown and macro-free DOCX templates are supported.")
    return FORMATS[path.suffix.lower()]


def extract_local(path: Path, format_name: str) -> dict:
    if format_name == "markdown":
        from flow1c_markdown import extract
    elif format_name == "docx":
        from flow1c_docx import extract_template as extract
    else:
        raise TemplateError("TEMPLATE_FORMAT_UNSUPPORTED", "Unknown format adapter.")
    result = extract(path)
    result.update(schema_version=1, source_sha256=sha256(path), source_format=format_name, extractor_version=1)
    return result


def extract(path: Path, format_name: str) -> dict:
    try:
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(path), format_name],
                             capture_output=True, timeout=PARSING_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Extraction exceeded the 10 second parsing budget.",
                            next_action="Reduce template complexity and resume; the accepted source is preserved.") from exc
    if len(run.stdout) > 16 * 1024 * 1024:
        raise TemplateError("TEMPLATE_LAYOUT_UNSUPPORTED", "Extraction exceeds the 16 MiB context budget.")
    if run.returncode:
        try:
            error = json.loads(run.stdout.decode("utf-8"))["error"]
        except (ValueError, KeyError):
            error = {"code": "TEMPLATE_LAYOUT_UNSUPPORTED", "message": "Format parsing failed; use a supported clean template."}
        raise TemplateError(error["code"], error["message"], component="template-extract",
                            next_action=error.get("next_action", "Correct the format and resume."))
    return json.loads(run.stdout.decode("utf-8"))


if __name__ == "__main__":
    try:
        result = extract_local(Path(sys.argv[1]), sys.argv[2])
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        error = exc.as_dict() if hasattr(exc, "as_dict") else {"code": "TEMPLATE_LAYOUT_UNSUPPORTED", "message": "Unable to parse template safely."}
        sys.stdout.buffer.write(json.dumps({"error": error}, ensure_ascii=False).encode("utf-8"))
        raise SystemExit(2)
