"""Bounded verification of persisted bytes, independent of the write adapter."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.flow1c_templates_policy import TemplateError, fingerprint
from scripts.flow1c_templates_store import read_json, sha256

VERIFICATION_TIMEOUT = 15
VERIFICATION_VERSION = 1


def mismatch(check: str, *, target: str | None = None) -> None:
    raise TemplateError(
        "TEMPLATE_INTEGRITY_FAILED", "Written document does not match the pinned template and plan.",
        component="template-result-verification",
        next_action="Preserve the candidate, correct the writer or plan, and retry; do not approve this result.",
        preserved_state={"check": check, **({"target": target} if target else {})},
    )


def verify_local(source: Path, output: Path, extraction_path: Path, operations: list[dict]) -> dict:
    extraction = read_json(extraction_path)
    before = (sha256(source), sha256(output))
    if before[0] != extraction["source_sha256"]:
        mismatch("source-version")
    targets = {target["id"]: target for target in extraction["targets"]}
    ranges: dict[str, list[tuple[int, int]]] = {}
    seen = set()
    for operation in operations:
        key = operation["target_id"]
        if key in seen or key not in targets:
            mismatch("plan-target")
        seen.add(key)
        target = targets[key]
        kind = "text" if target["kind"] in {"field", "paragraph"} else target["kind"]
        if not target["supported"] or operation["kind"] != kind:
            mismatch("plan-operation", target=key)
        start, end = target["range"]
        previous = ranges.setdefault(target["part"], [])
        if any(start < right and left < end for left, right in previous):
            mismatch("overlapping-targets", target=key)
        previous.append((start, end))
    if extraction["source_format"] == "markdown":
        from scripts.flow1c_markdown_verify import verify_document
    elif extraction["source_format"] == "docx":
        from scripts.flow1c_docx_verify import verify_document
    else:
        mismatch("format")
    verify_document(source, output, targets, operations)
    if before != (sha256(source), sha256(output)):
        mismatch("file-changed-during-verification")
    return {"schema_version": VERIFICATION_VERSION, "status": "PASSED",
            "source_sha256": before[0], "output_sha256": before[1],
            "operations_sha256": fingerprint(operations), "operation_count": len(operations)}


def verify(source: Path, output: Path, extraction_path: Path, operations: list[dict]) -> dict[str, Any]:
    request = {"source": str(source), "output": str(output), "extraction": str(extraction_path),
               "operations": operations}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.flow1c_templates_verify"],
            cwd=Path(__file__).resolve().parents[1], input=json.dumps(request).encode("utf-8"),
            capture_output=True, check=False, timeout=VERIFICATION_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Independent result verification is unavailable or timed out.",
                            component="template-result-verification",
                            next_action="Preserve the candidate and retry verification; do not report PASSED.") from exc
    if len(result.stdout) > 64 * 1024:
        mismatch("verification-output-budget")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Independent verifier returned no valid receipt.",
                            component="template-result-verification") from exc
    if not isinstance(payload, dict):
        mismatch("verification-receipt")
    if result.returncode or payload.get("status") != "PASSED":
        error = payload.get("error", {})
        error = error if isinstance(error, dict) else {}
        raise TemplateError("TEMPLATE_INTEGRITY_FAILED", "Independent verification rejected the written document.",
                            component="template-result-verification",
                            preserved_state=error.get("preserved_state"),
                            next_action="Preserve the candidate, correct the writer or plan, and retry.")
    if (payload.get("schema_version") != VERIFICATION_VERSION
            or payload.get("source_sha256") != sha256(source)
            or payload.get("output_sha256") != sha256(output)
            or payload.get("operations_sha256") != fingerprint(operations)
            or payload.get("operation_count") != len(operations)):
        mismatch("verification-receipt")
    return payload


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.buffer.read(32 * 1024 * 1024 + 1))
        receipt = verify_local(Path(request["source"]), Path(request["output"]),
                               Path(request["extraction"]), request["operations"])
        sys.stdout.buffer.write(json.dumps(receipt).encode("utf-8"))
    except Exception as exc:
        details = exc.as_dict() if isinstance(exc, TemplateError) else {"preserved_state": {"check": "unreadable-result"}}
        sys.stdout.buffer.write(json.dumps({"error": details}).encode("utf-8"))
        raise SystemExit(2)
