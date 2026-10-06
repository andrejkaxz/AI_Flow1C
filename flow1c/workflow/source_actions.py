"""Bounded source reads, query checks and BSL integration evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import sources as sources
from flow1c import storage as storage
from flow1c import system as system
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import git_actions as git_actions
from flow1c.workflow import state as gate_state
from scripts import cc_query_inspector as cc_inspector
from scripts import flow1c_query_policy as query_policy
from scripts import flow1c_query_schema as metadata_schema


def agent_source_read(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_source_read", product_root=product_root)
    if args.source != "extension":
        raise WorkflowError("Direct configuration reads are forbidden; use source-query.")
    try:
        local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    except WorkflowError as exc:
        _value = {
            "state": "NEEDS_INPUT",
            "available_actions": ["flow1c_dialogue"],
            "user_message": "Локальная настройка источника расширения недоступна. Можно продолжить общую часть черновика или уточнить подключение.",
            "errors": [{"code": "EXTENSION_CONFIG_INVALID", "message": str(exc)}],
        }
        return OperationResult(_value, 1)
    root_raw = str(local.get("extension_path", "")).strip()
    if not root_raw:
        _value = {
            "state": "NEEDS_INPUT",
            "available_actions": ["flow1c_dialogue"],
            "user_message": "Путь к checkout расширения не настроен. Можно продолжить общую часть черновика или уточнить подключение.",
            "errors": [
                {
                    "code": "EXTENSION_PATH_NOT_CONFIGURED",
                    "message": "extension_path is not configured",
                }
            ],
        }
        return OperationResult(_value, 1)
    root = Path(root_raw).resolve()
    if not root.is_dir():
        _value = {
            "state": "NEEDS_INPUT",
            "available_actions": ["flow1c_dialogue"],
            "user_message": "Настроенный checkout расширения недоступен. Можно продолжить общую часть черновика или уточнить подключение.",
            "errors": [
                {
                    "code": "EXTENSION_PATH_INVALID",
                    "message": "extension_path is not an existing directory",
                }
            ],
        }
        return OperationResult(_value, 1)
    target = (root / args.path).resolve()
    if not storage.path_is_within(target, root) or not target.is_file():
        raise WorkflowError(
            "Requested extension file is outside the configured checkout or does not exist."
        )
    if target.suffix.casefold() not in {".bsl", ".xml", ".json", ".md"}:
        raise WorkflowError("Only BSL, XML, JSON and Markdown extension sources may be read.")
    content = target.read_text(encoding="utf-8-sig", errors="replace")
    if len(content) > int(args.max_chars):
        raise WorkflowError(
            f"Source exceeds {args.max_chars} characters; request a narrower file or use RLM."
        )
    digest = storage.sha256(target)
    evidence_id = f"SRC-{digest[:12]}"
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    relative = str(target.relative_to(root)).replace("\\", "/")
    source_reads = evidence.setdefault("source_reads", [])
    existing = next(
        (
            item
            for item in source_reads
            if isinstance(item, dict)
            and item.get("source") == "extension"
            and (item.get("path") == relative)
            and (item.get("sha256") == digest)
        ),
        None,
    )
    if existing:
        evidence_id = str(existing.get("evidence_id") or evidence_id)
    else:
        source_reads.append(
            {
                "evidence_id": evidence_id,
                "source": "extension",
                "path": relative,
                "sha256": digest,
                "created_at": storage.utc_now(),
            }
        )
        storage.write_json(evidence_path, evidence)
    _value = {
        "state": "READY",
        "evidence_id": evidence_id,
        "path": str(target),
        "sha256": digest,
        "content": content,
    }
    return OperationResult(_value, 0)


def source_query(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_source_query", product_root=product_root)
    config, local = runtime.load_config(product_root=product_root)
    endpoint = str(config.get("quality", {}).get("rlm_endpoint", "")).strip()
    source_selector = args.source
    source_kind = (
        str(source_selector.get("kind", ""))
        if isinstance(source_selector, dict)
        else str(source_selector or "")
    )
    source_provenance: dict[str, Any] = {"kind": source_kind}
    if source_kind != "git_snapshot":
        ready, errors = sources.rlm_readiness(product_root=product_root)
        if not ready:
            query_work = gate.get("operation") == "query-analysis"
            _value = {
                "state": "NEEDS_INPUT",
                "code": "RLM_SOURCE_UNAVAILABLE",
                "available_actions": (
                    gate["available_actions"]
                    if query_work
                    else ["flow1c_dialogue", "flow1c_complete"]
                ),
                "next_actions": (
                    ["continue-static-query-check"]
                    if query_work
                    else [
                        "bootstrap-rlm-if-missing",
                        "start-rlm",
                        "ensure-source-indexes",
                        "retry-source-query",
                    ]
                ),
                "user_message": (
                    "RLM недоступен. Продолжите независимый анализ текста через flow1c_query_check; метаданные останутся неподтверждёнными."
                    if query_work
                    else "RLM недоступен. Восстановите установленный сервис и индексы, затем повторите запрос к источнику."
                ),
                "errors": errors,
            }
            return OperationResult(_value, 1)
    if source_kind == "git_snapshot":
        snapshot_id = str(source_selector.get("snapshot_id", ""))
        snapshot = git_actions._git_analysis_state(gate).get("snapshots", {}).get(snapshot_id)
        if not isinstance(snapshot, dict):
            raise WorkflowError("The requested Git snapshot does not belong to this gate")
        requested_commit = str(source_selector.get("commit", "") or snapshot.get("commit", ""))
        if requested_commit != snapshot.get("commit"):
            raise WorkflowError("Snapshot commit does not match the saved manifest")
        source_root = Path(str(snapshot.get("path", ""))).resolve()
        snapshot_root = (product_root / ".workspace" / "git-analysis").resolve()
        if not storage.path_is_within(source_root, snapshot_root) or not source_root.is_dir():
            raise WorkflowError("Snapshot source is unavailable or outside the managed cache")
        source_provenance.update(
            snapshot_id=snapshot_id,
            commit=requested_commit,
            repository=source_selector.get("repository", "extension"),
            path=str(source_root),
        )
    else:
        if source_kind not in {"configuration", "extension"}:
            raise WorkflowError(
                "source must be configuration, extension, or a git_snapshot selector"
            )
        raw_path = str(
            local.get(
                "configuration_path" if source_kind == "configuration" else "extension_path", ""
            )
        ).strip()
        if not raw_path:
            raise WorkflowError(f"{source_kind} source path is not configured.")
        source_root = system.resolve_1c_source_root(Path(raw_path))
        source_provenance["path"] = str(source_root)
    if source_kind == "git_snapshot":
        errors = (
            [] if sources.endpoint_is_healthy(endpoint) else ["RLM MCP endpoint is not healthy"]
        )
    else:
        errors = []
    if errors:
        query_work = gate.get("operation") == "query-analysis"
        _value = {
            "state": "RECOVERABLE_ERROR",
            "code": (
                "RLM_SNAPSHOT_INDEX_FAILED"
                if source_kind == "git_snapshot"
                else "RLM_SOURCE_UNAVAILABLE"
            ),
            "source": source_provenance,
            "git_evidence_preserved": True,
            "available_actions": (
                gate["available_actions"] if query_work else ["flow1c_dialogue", "flow1c_complete"]
            ),
            "next_actions": (
                ["continue-static-query-check"]
                if query_work
                else ["bootstrap-rlm-if-missing", "start-rlm", "retry-source-query"]
            ),
            "user_message": (
                "RLM недоступен. Продолжите независимый анализ текста через flow1c_query_check; метаданные останутся неподтверждёнными."
                if query_work
                else "RLM недоступен. Восстановите сервис и повторите запрос; статический Git-анализ сохранён."
            ),
            "errors": errors,
        }
        return OperationResult(_value, 1)
    code = str(args.code or "").strip()
    if not code:
        raise WorkflowError(
            "RLM execution code is required. The query describes the evidence goal; code must be Python for rlm_execute and must print the result."
        )
    result, error = sources.rlm_session_execute(
        endpoint,
        source_root,
        args.query,
        code,
        effort=args.effort,
        max_output_chars=args.max_chars,
        timeout=120.0,
        domains=["весь каталог"],
    )
    if error or result is None or result.get("error"):
        message = error or str(result.get("error") if result else "RLM query failed")
        evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
        evidence.setdefault("rlm_queries", []).append(
            {
                "source": source_provenance,
                "query": args.query,
                "status": "recoverable_error",
                "code": (
                    "RLM_SNAPSHOT_INDEX_FAILED"
                    if source_kind == "git_snapshot"
                    else "RLM_QUERY_FAILED"
                ),
                "message": message,
                "created_at": storage.utc_now(),
            }
        )
        storage.write_json(evidence_path, evidence)
        query_work = gate.get("operation") == "query-analysis"
        _value = {
            "state": "RECOVERABLE_ERROR",
            "code": (
                "RLM_SNAPSHOT_INDEX_FAILED" if source_kind == "git_snapshot" else "RLM_QUERY_FAILED"
            ),
            "source": source_provenance,
            "message": message,
            "git_evidence_preserved": True,
            **({"available_actions": gate["available_actions"]} if query_work else {}),
            "next_actions": (
                ["continue-static-query-check"]
                if query_work
                else [
                    "check-rlm-health-and-index",
                    "retry-source-query",
                    "complete-static-analysis-if-recovery-fails",
                ]
            ),
        }
        return OperationResult(_value, 1)
    if not str(result.get("stdout", "")).strip():
        raise WorkflowError("RLM query execution produced no output; code must print a result.")
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    query_id = f"RLM-{len(evidence.get('rlm_queries', [])) + 1:03d}"
    record = {
        "id": query_id,
        "source": source_provenance,
        "query": args.query,
        "code": code,
        "reason": args.reason,
        "status": (
            "retrieved_unvalidated" if gate.get("operation") == "query-analysis" else "confirmed"
        ),
        "result_sha256": hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "result": result,
        "created_at": storage.utc_now(),
    }
    evidence.setdefault("rlm_queries", []).append(record)
    storage.write_json(evidence_path, evidence)
    _value = {
        "state": (
            "RETRIEVED_UNVALIDATED" if gate.get("operation") == "query-analysis" else "CONFIRMED"
        ),
        "evidence_id": query_id,
        "source": source_provenance,
        "result": result,
    }
    return OperationResult(_value, 0)


def cc_inspect(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    """Run one read-only CC inspector against accepted or configured XML."""
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_cc_inspect", product_root=product_root)
    if gate.get("operation") != "query-analysis":
        raise WorkflowError("CC query inspection is available only in the query-analysis scenario")
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    configuration_raw = str(local.get("configuration_path", "") or "").strip()
    extension_raw = str(local.get("extension_path", "") or "").strip()
    try:
        target, source = cc_inspector.resolve_input_path(
            source=args.source,
            relative_path=args.path,
            request_root=gate_state.request_root(gate, product_root=product_root),
            request_artifacts=[
                item for item in gate.get("artifacts", []) if isinstance(item, dict)
            ],
            configuration_root=(
                system.resolve_1c_source_root(Path(configuration_raw))
                if configuration_raw
                else None
            ),
            extension_root=(
                system.resolve_1c_source_root(Path(extension_raw)) if extension_raw else None
            ),
        )
        result = cc_inspector.run_inspection(
            checkout=product_root / ".tools" / "cc-1c-skills",
            operation=args.operation,
            target=target,
            name=args.name,
            max_chars=args.max_chars,
        )
    except cc_inspector.CcInspectionUnavailable as exc:
        _value = {
            "state": "NEEDS_INPUT",
            "limitation": "CC_SKILLS_UNAVAILABLE",
            "user_message": f"{exc}. Продолжите независимый анализ текста запроса; эти метаданные не подтверждены.",
            "available_actions": gate["available_actions"],
        }
        return OperationResult(_value, 1)
    except cc_inspector.CcInspectionError as exc:
        raise WorkflowError(str(exc)) from exc
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    inspection_id = f"CC-{len(evidence.get('cc_inspections', [])) + 1:03d}"
    record = {
        "id": inspection_id,
        "source": source,
        "source_kind": "local_xml_export" if source != "request" else "accepted_xml_artifact",
        "path": str(target),
        "skill": result["skill"],
        "operation": result["operation"],
        "result_sha256": hashlib.sha256(result["output"].encode("utf-8")).hexdigest(),
        "created_at": storage.utc_now(),
    }
    evidence.setdefault("cc_inspections", []).append(record)
    storage.write_json(evidence_path, evidence)
    _value = {
        "state": "CONFIRMED_LOCAL_XML",
        "evidence_id": inspection_id,
        "verification_level": "metadata",
        "source_kind": record["source_kind"],
        "database_verified": False,
        "execution_verified": False,
        "result": result,
    }
    return OperationResult(_value, 0)


def query_schema(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    """Record exact fields from one XML export, with source provenance."""
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_query_schema", product_root=product_root)
    if gate.get("operation") != "query-analysis":
        raise WorkflowError("query-schema is available only for query-analysis")
    source = str(args.source or "")
    local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    configuration_raw = str(local.get("configuration_path", "") or "").strip()
    extension_raw = str(local.get("extension_path", "") or "").strip()
    configuration_root = (
        system.resolve_1c_source_root(Path(configuration_raw))
        if source == "configuration" and configuration_raw
        else None
    )
    extension_root = (
        system.resolve_1c_source_root(Path(extension_raw))
        if source == "extension" and extension_raw
        else None
    )
    try:
        target, _ = cc_inspector.resolve_input_path(
            source=source,
            relative_path=str(args.path or ""),
            request_root=gate_state.request_root(gate, product_root=product_root),
            request_artifacts=[
                item for item in gate.get("artifacts", []) if isinstance(item, dict)
            ],
            configuration_root=configuration_root,
            extension_root=extension_root,
        )
    except cc_inspector.CcInspectionError as exc:
        raise WorkflowError(str(exc)) from exc
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    rlm_id = str(getattr(args, "rlm_evidence_id", "") or "").strip()
    if source != "request":
        selected = next(
            (item for item in evidence.get("rlm_queries", []) if item.get("id") == rlm_id), None
        )
        if (
            not selected
            or selected.get("source", {}).get("kind") != source
            or selected.get("status") not in {"retrieved_unvalidated", "confirmed"}
        ):
            raise WorkflowError(
                "First discover this XML path with flow1c_source_query for the same source"
            )
        selected_root = Path(str(selected.get("source", {}).get("path", ""))).resolve()
        expected_root = configuration_root if source == "configuration" else extension_root
        if expected_root is None or selected_root != expected_root.resolve():
            raise WorkflowError("The RLM result belongs to a different source checkout")
        stdout = str(selected.get("result", {}).get("stdout", "")).replace("\\", "/").casefold()
        if str(args.path or "").replace("\\", "/").casefold() not in stdout:
            raise WorkflowError("The selected RLM evidence does not identify this exact XML path")
    raw = target.read_bytes()
    try:
        schema = metadata_schema.parse_metadata_xml(raw)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    digest = hashlib.sha256(raw).hexdigest()
    for saved in evidence.get("query_schemas", []):
        if (
            saved.get("path") == str(target)
            and saved.get("sha256") == digest
            and (saved.get("source") == source)
        ):
            _value = {"state": "CONFIRMED_IN_XML", "schema": saved}
            return OperationResult(_value, 0)
    schema = {
        **schema,
        "id": f"QM-{len(evidence.get('query_schemas', [])) + 1:03d}",
        "source": source,
        "path": str(target),
        "relative_path": str(args.path or ""),
        "sha256": digest,
        "rlm_evidence_id": rlm_id or None,
        "created_at": storage.utc_now(),
    }
    evidence.setdefault("query_schemas", []).append(schema)
    storage.write_json(evidence_path, evidence)
    _value = {"state": "CONFIRMED_IN_XML", "schema": schema}
    return OperationResult(_value, 0)


def query_check(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    """Save and inspect the exact candidate that the agent will present."""
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"},
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_query_check", product_root=product_root)
    if gate.get("operation") != "query-analysis":
        raise WorkflowError("query-check is available only for query-analysis")
    candidate = str(args.text or "")
    baseline = str(getattr(args, "baseline_text", "") or "")
    if (
        gate.get("query_intent") == "optimize"
        and gate.get("query_contract_version") == 1
        and (not baseline.strip())
    ):
        raise WorkflowError(
            "Optimization requires baseline_text so the original query remains reviewable"
        )
    schema_ids = getattr(args, "schema_ids", []) or []
    if not isinstance(schema_ids, list) or any((not isinstance(item, str) for item in schema_ids)):
        raise WorkflowError("schema_ids must be an array of evidence IDs")
    assumptions = getattr(args, "assumptions", []) or []
    if not isinstance(assumptions, list) or any(
        (not isinstance(item, str) for item in assumptions)
    ):
        raise WorkflowError("assumptions must be an array of strings")
    changes = getattr(args, "changes", []) or []
    if not isinstance(changes, list) or any((not isinstance(item, str) for item in changes)):
        raise WorkflowError("changes must be an array of strings")
    expected_result = str(getattr(args, "expected_result", "") or "").strip()
    if (
        gate.get("query_contract_version") == 1
        and gate.get("query_intent") in {"create", "optimize"}
        and (not expected_result)
    ):
        raise WorkflowError("Describe the expected row grain and result in expected_result")
    if (
        gate.get("query_contract_version") == 1
        and gate.get("query_intent") == "optimize"
        and (not changes)
    ):
        raise WorkflowError("Explain proposed structural changes in changes")
    evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    available = {str(item.get("id")): item for item in evidence.get("query_schemas", [])}
    if len(set(schema_ids)) != len(schema_ids) or any(
        (item not in available for item in schema_ids)
    ):
        raise WorkflowError("schema_ids must uniquely identify schemas recorded in this gate")
    schemas = [available[item] for item in schema_ids]
    schema_sources = []
    for schema in schemas:
        source_path = Path(str(schema.get("path", "")))
        if not source_path.is_file() or storage.sha256(source_path) != schema.get("sha256"):
            raise WorkflowError(
                f"Metadata evidence {schema.get('id')} is stale; refresh it before query-check"
            )
        schema_sources.append(
            {"id": schema["id"], "path": str(source_path), "sha256": schema["sha256"]}
        )
    try:
        report = query_policy.analyze_query(candidate, schemas)
        baseline_report = query_policy.analyze_query(baseline, schemas) if baseline else None
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    if baseline_report:
        report["diagnostics"].extend(query_policy.compare_optimization(baseline_report, report))
    if not expected_result:
        report["limitations"].append(
            "Ожидаемый состав строк и колонок не описан; соответствие бизнес-требованию не проверено."
        )
    report.update(
        {
            "id": f"QC-{len(evidence.get('query_checks', [])) + 1:03d}",
            "intent": gate.get("query_intent", "review"),
            "schema_ids": schema_ids,
            "schema_sources": schema_sources,
            "baseline_sha256": (
                hashlib.sha256(baseline.encode("utf-8")).hexdigest() if baseline else None
            ),
            "expected_result": expected_result,
            "assumptions": assumptions,
            "changes": changes,
            "created_at": storage.utc_now(),
        }
    )
    candidate_path = (
        gate_state.request_root(gate, product_root=product_root) / "query-candidate.txt"
    )
    previous = (evidence.get("query_checks") or [None])[-1]
    baseline_path = gate_state.request_root(gate, product_root=product_root) / "query-baseline.txt"
    if (
        isinstance(previous, dict)
        and candidate_path.is_file()
        and (storage.sha256(candidate_path) == report["text_sha256"])
        and (
            not baseline
            or (
                baseline_path.is_file()
                and storage.sha256(baseline_path) == report["baseline_sha256"]
            )
        )
        and all(
            (
                previous.get(key) == report.get(key)
                for key in (
                    "text_sha256",
                    "intent",
                    "schema_ids",
                    "schema_sources",
                    "baseline_sha256",
                    "expected_result",
                    "assumptions",
                    "changes",
                )
            )
        )
    ):
        _value = {
            "state": "CHECKED",
            "candidate_path": str(candidate_path),
            "query_text": candidate,
            "report": previous,
        }
        return OperationResult(_value, 0)
    storage.atomic_write_bytes(candidate_path, candidate.encode("utf-8"))
    if baseline:
        storage.atomic_write_bytes(baseline_path, baseline.encode("utf-8"))
    evidence.setdefault("query_checks", []).append(report)
    storage.write_json(evidence_path, evidence)
    _value = {
        "state": "CHECKED",
        "candidate_path": str(candidate_path),
        "query_text": candidate,
        "report": report,
    }
    return OperationResult(_value, 0)


def agent_analyze_bsl(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={
            "READY",
            "READY_WITH_DEVIATIONS",
            "UNVERIFIED_DRAFT",
            "BLOCKED",
            "NEEDS_INPUT",
            "NEEDS_CONFIRMATION",
            "NEEDS_CODE",
        },
        product_root=product_root,
    )
    gate_state.require_gate_tool(gate, "flow1c_analyze_bsl", product_root=product_root)
    if gate.get("operation") not in {"development", "technical-implementation", "code-review"}:
        raise WorkflowError("BSL analysis is not enabled for this operation.")
    selector = getattr(args, "source", None) or {"kind": "extension"}
    if not isinstance(selector, dict):
        raise WorkflowError("BSL source must be a typed source selector.")
    kind = str(selector.get("kind", ""))
    if kind == "git_snapshot":
        snapshot_id = str(selector.get("snapshot_id", ""))
        snapshot = git_actions._git_analysis_state(gate).get("snapshots", {}).get(snapshot_id)
        if not isinstance(snapshot, dict):
            raise WorkflowError("The requested Git snapshot does not belong to this gate")
        commit = str(snapshot.get("commit", ""))
        if selector.get("commit") and selector["commit"] != commit:
            raise WorkflowError("Snapshot commit does not match the saved manifest")
        source_path = Path(str(snapshot.get("path", ""))).resolve()
        if (
            not storage.path_is_within(
                source_path, (product_root / ".workspace" / "git-analysis").resolve()
            )
            or not source_path.is_dir()
        ):
            raise WorkflowError("Snapshot source is unavailable or outside the managed cache")
        provenance = {
            "kind": kind,
            "snapshot_id": snapshot_id,
            "commit": commit,
            "path": str(source_path),
        }
    elif kind in {"extension", "configuration"}:
        local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
        raw = str(local.get(f"{kind}_path", "")).strip()
        if not raw:
            raise WorkflowError(f"{kind} source path is not configured.")
        source_path = system.resolve_1c_source_root(Path(raw))
        if not source_path.is_dir():
            raise WorkflowError(f"{kind} source directory is unavailable: {source_path}")
        provenance = {"kind": kind, "path": str(source_path)}
    else:
        raise WorkflowError(
            "BSL source must be extension, configuration, or a git_snapshot selector"
        )
    powershell = system.command_path("powershell") or system.command_path("pwsh")
    if not powershell:
        raise WorkflowError("PowerShell is not available.")
    evidence_id = f"BSL-{uuid.uuid4().hex[:12]}"
    report_path = (product_root / ".workspace" / "diagnostics" / "bsl-ls" / evidence_id).resolve()
    result = subprocess.run(
        [
            powershell,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(product_root / "scripts" / "analyze-bsl.ps1"),
            "-SourcePath",
            str(source_path),
            "-OutputPath",
            str(report_path),
        ],
        cwd=product_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=600,
    )
    output = "\n".join((value.strip() for value in (result.stdout, result.stderr) if value.strip()))
    if gate.get("evidence_path"):
        evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
    else:
        evidence_path = (
            product_root
            / ".workspace"
            / "diagnostics"
            / "bsl-ls"
            / str(gate["gate_id"])
            / "evidence.json"
        ).resolve()
        evidence = {
            "schema_version": 2,
            "gate_id": gate["gate_id"],
            "operation": gate["operation"],
            "code": None,
            "artifacts": [],
            "rlm_queries": [],
            "source_reads": [],
            "changed_files": [],
        }
    record = {
        "evidence_id": evidence_id,
        "state": "passed" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "output": output[-8000:],
        "source": provenance,
        "report_path": str(report_path),
        "created_at": storage.utc_now(),
    }
    evidence["bsl_ls"] = record
    storage.write_json(evidence_path, evidence)
    _value = {"state": "PASSED" if result.returncode == 0 else "FAILED", **record}
    return OperationResult(_value, 0 if result.returncode == 0 else 2)
