"""Bounded RLM transport, index and source readiness operations."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import system as system


def rlm_health_url(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def endpoint_is_healthy(endpoint: str, timeout: float = 2.0) -> bool:
    health_url = rlm_health_url(endpoint)
    if not health_url:
        return False
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def rlm_mcp_call(
    endpoint: str, name: str, arguments: dict[str, Any], *, timeout: float = 5.0
) -> tuple[dict[str, Any] | None, str]:
    """Call a stateless RLM MCP tool and decode its structured JSON result."""
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path.rstrip("/") != "/mcp"
    ):
        return (None, "invalid MCP endpoint")
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except (OSError, UnicodeError, urllib.error.URLError, ValueError) as exc:
        return (None, str(exc))
    data_lines = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    if not data_lines:
        return (None, "MCP response contains no data event")
    try:
        envelope = json.loads(data_lines[-1])
        if envelope.get("error"):
            return (None, str(envelope["error"].get("message") or envelope["error"]))
        result = envelope["result"]
        raw = result.get("structuredContent", {}).get("result")
        if raw is None:
            content = result.get("content", [])
            raw = next((item.get("text") for item in content if item.get("type") == "text"), None)
        decoded = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(decoded, dict):
            return (None, "MCP tool returned a non-object result")
        return (decoded, "")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return (None, f"invalid MCP response: {exc}")


def rlm_endpoint_index_state(endpoint: str, source: Path) -> tuple[str, str]:
    result, error = rlm_mcp_call(endpoint, "rlm_index", {"action": "info", "path": str(source)})
    if error:
        return ("ERROR", error)
    assert result is not None
    if result.get("error"):
        return ("ERROR", str(result["error"]))
    modules = result.get("modules")
    if isinstance(modules, int):
        return ("OK", f"available ({modules} modules)")
    return ("ERROR", "endpoint did not return index information")


def rlm_session_execute(
    endpoint: str,
    source: Path,
    query: str,
    code: str,
    *,
    effort: str,
    max_output_chars: int,
    timeout: float,
    domains: list[str],
) -> tuple[dict[str, Any] | None, str]:
    """Open an RLM session, execute sandbox code, and always close the session."""
    start_args = {
        "query": query,
        "path": str(source),
        "effort": effort,
        "max_output_chars": max_output_chars,
    }
    start_result, start_error = rlm_mcp_call(endpoint, "rlm_start", start_args, timeout=timeout)
    domain_error = start_error or str(start_result.get("error", "") if start_result else "")
    if "domains" in domain_error and ("явный выбор" in domain_error or "required" in domain_error):
        start_result, start_error = rlm_mcp_call(
            endpoint, "rlm_start", {**start_args, "domains": domains}, timeout=timeout
        )
    if start_error:
        return (None, start_error)
    if start_result is None:
        return (None, "RLM session start returned no result")
    if start_result.get("error"):
        return (None, str(start_result["error"]))
    session_id = str(start_result.get("session_id", "")).strip()
    if not session_id:
        return (None, "endpoint did not create a sandbox session")
    execute_result: dict[str, Any] | None = None
    execute_error = ""
    try:
        execute_result, execute_error = rlm_mcp_call(
            endpoint, "rlm_execute", {"session_id": session_id, "code": code}, timeout=timeout
        )
    finally:
        end_result, end_error = rlm_mcp_call(
            endpoint, "rlm_end", {"session_id": session_id}, timeout=10.0
        )
    if execute_error:
        return (None, execute_error)
    if execute_result is None:
        return (None, "RLM execution returned no result")
    if execute_result.get("error"):
        return (None, str(execute_result["error"]))
    if end_error:
        return (None, f"query executed but session could not be closed: {end_error}")
    if end_result is None:
        return (None, "query executed but session close returned no result")
    if end_result.get("error"):
        return (None, f"query executed but session could not be closed: {end_result['error']}")
    return (execute_result, "")


def rlm_endpoint_session_state(endpoint: str, source: Path) -> tuple[str, str]:
    marker = "FLOW1C_WORKFLOW_RLM_OK"
    result, error = rlm_session_execute(
        endpoint,
        source,
        "Flow1C health check",
        f"print({marker!r})",
        effort="low",
        max_output_chars=1000,
        timeout=10.0,
        domains=[],
    )
    if error:
        return ("ERROR", error)
    assert result is not None
    if marker not in str(result.get("stdout", "")):
        return ("ERROR", "sandbox execution did not return the smoke-test marker")
    return ("OK", "sandbox execution succeeded and session closed")


def rlm_index_state(index_command: str, source: Path, *, product_root: Path) -> tuple[str, str]:
    if not index_command or not Path(index_command).is_file():
        return ("ERROR", "rlm-bsl-index is not installed")
    from scripts.rlm_index_runtime import IndexManager

    try:
        manager = IndexManager(product_root, source, command=[index_command])
        if manager.receipt_path.exists() or manager.job_path.exists():
            state = manager.status()
            return ("OK" if state["state"] == "FRESH" else "ERROR", state["detail"])
    except Exception as exc:
        return ("ERROR", f"Index validation failed: {exc}")
    try:
        result = subprocess.run(
            [index_command, "index", "info", str(source)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ("ERROR", str(exc))
    output = "\n".join((part for part in (result.stdout.strip(), result.stderr.strip()) if part))
    match = re.search("^\\s*Status:\\s*([^\\r\\n]+)", output, flags=re.IGNORECASE | re.MULTILINE)
    status = match.group(1).strip() if match else ""
    if result.returncode == 0 and status.casefold() == "fresh":
        return ("OK", status)
    detail = status or (output.splitlines()[0] if output else f"exit code {result.returncode}")
    return ("ERROR", detail)


def rlm_readiness(*, product_root: Path) -> tuple[bool, list[str]]:
    config, local = runtime.load_config(product_root=product_root)
    quality = config.get("quality", {})
    endpoint = str(quality.get("rlm_endpoint", "")).strip()
    errors: list[str] = []
    if not endpoint_is_healthy(endpoint):
        errors.append("RLM MCP endpoint is not healthy")
        return (False, errors)
    index_command = (
        str(local.get("rlm", {}).get("index_command", ""))
        if isinstance(local.get("rlm"), dict)
        else ""
    )
    for label, raw in (
        ("configuration", local.get("configuration_path")),
        ("extension", local.get("extension_path")),
    ):
        path = system.resolve_1c_source_root(Path(str(raw))) if raw else None
        if path is None:
            errors.append(f"RLM {label} source is not configured")
            continue
        state, detail = rlm_index_state(index_command, path, product_root=product_root)
        if state != "OK":
            errors.append(f"RLM {label} index: {detail}")
        endpoint_state, endpoint_detail = rlm_endpoint_index_state(endpoint, path)
        if endpoint_state != "OK":
            errors.append(f"RLM {label} MCP index: {endpoint_detail}")
    return (not errors, errors)
