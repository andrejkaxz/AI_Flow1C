"""Integrity checks and completion of saved requests."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import handoff
from flow1c import context_manifest
from flow1c.handoff_policy import COMPLETED_STATES, HandoffError
from flow1c import publication as publication
from flow1c import storage as storage
from flow1c import system as system
from flow1c import templates as templates_service
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state
from scripts import flow1c_interview_cli as interview_cli
from scripts import flow1c_interview_policy as interview_policy
from scripts import flow1c_policy as policy
from scripts import flow1c_sections_policy as sections_policy
from scripts import flow1c_templates as template_library
from scripts import flow1c_templates_policy as template_policy


def complete_free_request(
    gate: dict[str, Any], args: argparse.Namespace, *, product_root: Path
) -> OperationResult:
    if gate.get("operation") in {"template-management", "template-document"}:
        return templates_service.complete(gate, args, product_root=product_root)
    output = None
    if (
        gate["mode"] == "draft"
        and gate.get("interview_output")
        and (not args.output or str(args.output).lower().endswith(".xlsx"))
    ):
        record = gate["interview_output"]
        if args.output and args.output != record["path"]:
            raise interview_policy.InterviewError(
                "INTERVIEW_OUTPUT_CHANGED",
                "Завершение требует последней проверенной книги этого запроса.",
            )
        _, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
        output = interview_cli.completion(
            gate_state.request_root(gate, product_root=product_root),
            record,
            evidence.get("changed_files", []),
        )
        gate.update(
            state="DRAFT_COMPLETE", document_status="UNVERIFIED_DRAFT", output=record["path"]
        )
    elif gate["mode"] == "draft":
        output = gate_state.safe_request_file(
            gate_state.request_root(gate, product_root=product_root),
            args.output or "result.md",
            markdown=True,
        )
        if not gate_state.meaningful_file(output) or "UNVERIFIED_DRAFT" not in output.read_text(
            encoding="utf-8"
        ):
            raise WorkflowError("A nonempty UNVERIFIED_DRAFT document is required")
        evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
        relative = str(
            output.relative_to(gate_state.request_root(gate, product_root=product_root))
        ).replace("\\", "/")
        if not any(
            (
                item.get("path") == relative and item.get("sha256") == storage.sha256(output)
                for item in evidence["changed_files"]
            )
        ):
            raise WorkflowError("The draft must match its recorded output")
        gate.update(state="DRAFT_COMPLETE", document_status="UNVERIFIED_DRAFT", output=relative)
    else:
        summary = str(getattr(args, "summary", "") or "").strip()
        if not summary:
            raise WorkflowError("Supply a consultation summary to record its result")
        if gate.get("operation") == "query-analysis" and gate.get("query_contract_version") == 1:
            _, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
            checks = evidence.get("query_checks", [])
            if not checks:
                raise WorkflowError(
                    "Run query-check on the final query text before completing query-analysis"
                )
            latest = checks[-1]
            candidate_path = (
                gate_state.request_root(gate, product_root=product_root) / "query-candidate.txt"
            )
            if not candidate_path.is_file() or storage.sha256(candidate_path) != latest.get(
                "text_sha256"
            ):
                raise WorkflowError(
                    "The final query changed after query-check; check the exact text again"
                )
            if gate.get("query_intent") == "optimize":
                baseline_path = (
                    gate_state.request_root(gate, product_root=product_root) / "query-baseline.txt"
                )
                if not baseline_path.is_file() or storage.sha256(baseline_path) != latest.get(
                    "baseline_sha256"
                ):
                    raise WorkflowError(
                        "The original query changed after query-check; check both versions again"
                    )
            if (
                gate.get("query_intent") in {"create", "optimize"}
                and latest.get("check_result") == "FAIL"
            ):
                raise WorkflowError(
                    "The candidate has a static error; correct it and run query-check again"
                )
            for item in latest.get("schema_sources", []):
                source_path = Path(str(item.get("path", "")))
                if not source_path.is_file() or storage.sha256(source_path) != item.get("sha256"):
                    raise WorkflowError(
                        "Metadata changed since query-check; refresh schema and check the candidate again"
                    )
            gate["query_verification"] = {
                "schema_version": 1,
                "verification_level": latest["verification_level"],
                "check_result": latest["check_result"],
                "text_sha256": latest["text_sha256"],
                "platform_executed": False,
                "performance_measured": False,
                "limitations": latest["limitations"],
            }
        gate.update(state="CONSULTATION_COMPLETE", result_summary=summary)
    gate["completed_at"] = storage.utc_now()
    result = {
        "state": gate["state"],
        "document_status": gate.get("document_status"),
        "compliance": gate.get("compliance", "COMPLIANT"),
        "deviations": gate.get("deviations", []),
        "output": str(output) if output else None,
    }
    if gate.get("query_verification"):
        result["query_verification"] = gate["query_verification"]
        result["query_text"] = (
            (gate_state.request_root(gate, product_root=product_root) / "query-candidate.txt")
            .read_bytes()
            .decode("utf-8")
        )
    _value = result
    result = OperationResult(_value, 0)
    handoff.save_completion(gate, result, product_root=product_root)
    return result


def output_has_unverified_claims(text: str, evidence: dict[str, Any]) -> bool:
    claim = re.search(
        "\\b(проверен\\w*|подтвержден\\w*|verified|confirmed)\\b", text, flags=re.IGNORECASE
    )
    if not claim:
        return False
    evidence_ids = [str(evidence.get("context_evidence_id", ""))]
    evidence_ids.extend(
        (
            str(item.get("id", ""))
            for item in evidence.get("rlm_queries", [])
            if isinstance(item, dict)
        )
    )
    if isinstance(evidence.get("diff"), dict):
        evidence_ids.append(str(evidence["diff"].get("evidence_id", "")))
    if isinstance(evidence.get("bsl_ls"), dict):
        evidence_ids.append(str(evidence["bsl_ls"].get("evidence_id", "")))
    evidence_ids.extend(
        (
            str(item.get("evidence_id", ""))
            for item in evidence.get("source_reads", [])
            if isinstance(item, dict)
        )
    )
    return not any((evidence_id and evidence_id in text for evidence_id in evidence_ids))


def _agent_complete(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    gate = gate_state.load_gate(
        args.gate_id,
        states={"READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT", "NON_COMPLIANT"},
        product_root=product_root,
    )
    legacy_update_retry = (
        gate["state"] == "NON_COMPLIANT"
        and gate.get("mode", "formal") == "formal"
        and (gate.get("operation") == "update")
        and (gate.get("action_completed") == "update")
        and (len(gate.get("completion_errors", [])) == 1)
        and str(gate["completion_errors"][0]).startswith("output override is not allowed;")
    )
    if gate["state"] == "NON_COMPLIANT" and (not legacy_update_retry):
        raise WorkflowError("Agent gate state NON_COMPLIANT is not allowed for this action.")
    if not legacy_update_retry:
        gate_state.require_gate_tool(gate, "flow1c_complete", product_root=product_root)
    if gate.get("evidence_path"):
        context_errors = context_manifest.completion_errors(gate, product_root=product_root)
        if context_errors:
            return OperationResult({"state": gate["state"], "ready": False,
                                    "code": "CONTEXT_SCOPE_REQUIRED", "errors": context_errors,
                                    "gate_id": gate["gate_id"], "preserved_state": True,
                                    "next_action": "Rebuild changed compact context or read remaining mandatory parts, then retry complete on this gate."}, 2)
    if gate.get("mode", "formal") != "formal":
        return complete_free_request(gate, args, product_root=product_root)
    stages = runtime.load_stages(product_root=product_root)
    stage = stages["operations"][gate["operation"]]
    configured_output = str(stage.get("output") or "").strip()
    requested_output = str(args.output or "").strip()
    if requested_output and requested_output != configured_output:
        raise WorkflowError(
            f"flow1c_complete output override is not allowed; expected {configured_output or 'no output file'}. Omit output and retry flow1c_complete on the same gate."
        )
    if gate["operation"] == "update" and (not gate.get("action_completed")):
        raise WorkflowError(
            "The update result was not recorded in this gate. Retry flow1c_action action=update on the same gate, then retry flow1c_complete without output."
        )
    errors: list[str] = []
    if gate["operation"] in {"setup", "update", "registry", "status", "publish"} and (
        not gate.get("action_completed")
    ):
        errors.append("the requested workflow action has not completed")
    evidence: dict[str, Any] = {}
    evidence_path: Path | None = None
    traceability_mode = str(gate.get("traceability_mode") or "registry")
    if gate.get("code"):
        evidence_path, evidence = gate_state.evidence_for_gate(gate, product_root=product_root)
        _, completion_manifest = work_items.load_manifest(
            str(gate.get("work_reference") or gate.get("code")), product_root=product_root
        )
        traceability_mode = str(completion_manifest.get("traceability_mode") or traceability_mode)
        errors.extend(
            (
                f"evidence schema: {value}"
                for value in gate_state.validate_json_record(
                    evidence, product_root / "schemas" / "evidence.schema.json"
                )
            )
        )
        if (
            gate.get("manifest_status") is None
            or completion_manifest.get("status") != gate.get("manifest_status")
            or completion_manifest.get("approvals", {}) != gate.get("manifest_approvals", {})
        ):
            errors.append("current work-item status or approvals changed after gate opening")
    output_raw = configured_output
    output_path: Path | None = None
    output_text = ""
    if output_raw:
        root = (
            work_items.work_item_root(
                str(gate.get("work_reference") or gate["code"]), product_root=product_root
            )
            if gate.get("work_reference") or gate.get("code")
            else runtime.project_root(product_root=product_root)
        )
        output_path = (
            (root / output_raw).resolve()
            if not Path(output_raw).is_absolute()
            else Path(output_raw).resolve()
        )
        if not storage.path_is_within(output_path, root) or not gate_state.meaningful_file(
            output_path
        ):
            errors.append(f"required output is missing or empty: {output_path}")
        elif output_path.suffix.casefold() in {".md", ".txt"}:
            output_text = output_path.read_text(encoding="utf-8-sig", errors="replace")
            if gate.get("operation") == "technical-implementation":
                try:
                    sections_policy.validate_section_content(
                        "technical-implementation", output_text
                    )
                except sections_policy.SectionPolicyError as exc:
                    errors.append(exc.message)
    if stage.get("context_role") and (not evidence.get("context_sha256")):
        errors.append("role context was not built")
    if gate.get("template_result"):
        try:
            template_service = template_library.TemplateService(
                product_root, storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
            )
            template_root = templates_service.document_root(
                gate, template_service, product_root=product_root
            )
            template_service.document_validate(
                {"plan_id": gate["template_result"]["plan_id"]}, template_root
            )
            templates_service.check_formal_content(
                gate,
                template_service,
                {"plan_id": gate["template_result"]["plan_id"]},
                template_root,
                product_root=product_root,
            )
        except (template_policy.TemplateError, OSError, ValueError) as exc:
            errors.append("template output integrity/policy: " + str(exc))
    if evidence.get("context_path"):
        context_path = Path(evidence["context_path"])
        if not context_path.is_file() or storage.sha256(context_path) != evidence.get(
            "context_sha256"
        ):
            errors.append("recorded role context has changed")
    if evidence.get("diff"):
        _, manifest = work_items.load_manifest(str(gate["code"]), product_root=product_root)
        current_diff, error = system.extension_git_state(
            str(gate["code"]), manifest, product_root=product_root
        )
        if (
            error
            or not current_diff
            or any(
                (current_diff.get(k) != evidence["diff"].get(k) for k in ("head", "base", "branch"))
            )
        ):
            errors.append("extension Git state changed after the recorded diff")
    if stage.get("requires_rlm") and (not evidence.get("rlm_queries")):
        errors.append("no gated RLM evidence was recorded")
    if stage.get("requires_diff") and (not evidence.get("diff")):
        errors.append("bounded extension diff was not recorded")
    if stage.get("requires_bsl_ls") and (evidence.get("bsl_ls") or {}).get("state") != "passed":
        errors.append("BSL Language Server did not pass")
    if output_text and output_has_unverified_claims(output_text, evidence):
        errors.append("output contains verified/confirmed claims without evidence")
    if gate["state"] == "READY_WITH_DEVIATIONS" or traceability_mode == "provisional":
        if output_text and "UNVERIFIED_DRAFT" not in output_text:
            errors.append("deviated output watermark is missing")
        integrity_markers = (
            "required output is missing or empty",
            "deviated output watermark is missing",
            "recorded role context has changed",
            "extension Git state changed",
            "output contains verified/confirmed claims",
            "evidence schema:",
            "current work-item status or approvals changed",
        )
        integrity_errors = [
            error for error in errors if any((marker in error for marker in integrity_markers))
        ]
        waived_condition_ids = {
            str(condition_id)
            for deviation in gate.get("deviations", [])
            if isinstance(deviation, dict)
            for condition_id in deviation.get(
                "condition_ids", deviation.get("waived_conditions", [])
            )
        }
        unwaived_errors = []
        for error in errors:
            error_conditions = policy.build_conditions(stage, errors=[error])
            if not error_conditions or any(
                (item["id"] not in waived_condition_ids for item in error_conditions)
            ):
                unwaived_errors.append(error)
        state = (
            "NON_COMPLIANT" if integrity_errors or unwaived_errors else "COMPLETE_WITH_DEVIATIONS"
        )
    elif gate["state"] == "UNVERIFIED_DRAFT":
        if output_text and "UNVERIFIED_DRAFT" not in output_text:
            errors.append("unverified draft watermark is missing")
        state = "UNVERIFIED_DRAFT" if not errors else "NON_COMPLIANT"
    else:
        state = "COMPLETE" if not errors else "NON_COMPLIANT"
    snapshot = None
    if state == "COMPLETE" and gate.get("code"):
        snapshot = publication.publication_snapshot(
            str(gate["code"]),
            include_extension=bool(
                stage.get("requires_diff") or stage.get("requires_extension_branch")
            ),
            product_root=product_root,
        )
    gate["state"] = state
    gate["completed_at"] = storage.utc_now()
    gate["completion_errors"] = errors
    evidence_patch: dict[str, Any] = {}
    if evidence_path is not None:
        evidence_patch.update(completed_at=gate["completed_at"], completion_state=state,
                              completion_errors=errors)
        if state == "COMPLETE":
            evidence_patch.update(validation_snapshot=snapshot,
                                  validated_output=str(output_path) if output_path else None)
    result = {
        "state": state,
        "ready": state == "COMPLETE",
        "compliance": (
            "DEVIATED"
            if state == "COMPLETE_WITH_DEVIATIONS"
            else gate.get("compliance", "COMPLIANT")
        ),
        "errors": errors,
        "deviations": gate.get("deviations", []),
        "output": str(output_path) if output_path else None,
    }
    _value = result
    result = OperationResult(
        _value, 0 if state in {"COMPLETE", "COMPLETE_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"} else 2
    )
    if result.exit_code == 0:
        handoff.save_completion(gate, result, product_root=product_root, evidence_patch=evidence_patch)
    else:
        gate_state.save_gate(gate, product_root=product_root)
        if evidence_path is not None:
            evidence.update(evidence_patch)
            storage.write_json(evidence_path, evidence)
    return result


def agent_complete(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    """Commit once; retries only repair the sealed transfer and persisted copies."""
    result = None
    try:
        with handoff.writer(args.gate_id, product_root=product_root):
            gate = handoff.load_producer(args.gate_id, product_root=product_root)
            if gate.get("completion_record") or (gate.get("state") in COMPLETED_STATES and
                    (gate.get("state") != "UNVERIFIED_DRAFT" or gate.get("completed_at"))):
                receipt = gate.get("completion_record")
                if receipt:
                    handoff.verify_completion(gate, receipt, product_root=product_root)
                    result = OperationResult(receipt["result"], receipt["exit_code"])
            else:
                result = _agent_complete(args, product_root=product_root)
                if result.exit_code:
                    return result
                gate = handoff.load_producer(args.gate_id, product_root=product_root)
            if gate.get("handoff_status") == "READY":
                handoff.read_locked(gate, product_root=product_root)
            else:
                handoff.recover_locked(gate, product_root=product_root)
            if result is None:
                result = OperationResult(gate["completion_record"]["result"], gate["completion_record"]["exit_code"])
            return OperationResult({**result.value, "handoff": gate["handoff"],
                                    "handoff_status": "READY"}, result.exit_code)
    except HandoffError as exc:
        exc.gate_id = args.gate_id
        return OperationResult({**(result.value if result else {}), "handoff_error": exc.payload()}, 2)
    except OSError:
        # Do not roll back a durable completion or leak full paths/materials in diagnostics.
        exc = HandoffError("HANDOFF_RECOVERY_REQUIRED", "Transfer persistence interrupted; recover the same gate", args.gate_id)
        return OperationResult({**(result.value if result else {}), "handoff_error": exc.payload()}, 2)
