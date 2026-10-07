"""Setup checkpoints, background status and updater process boundary."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from flow1c import storage as storage
from flow1c import system as system
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state

SETUP_VALIDATION_VERSION = 1


def begin_update_gate(
    *, summary: str, project_reference: str | None, mode_selection: dict[str, Any],
    route_metadata: dict[str, Any], product_root: Path,
) -> OperationResult:
    """Authorize installation maintenance independently of work-item state."""
    skill = "flow1c-project-update"
    skill_path = product_root / ".agents" / "skills" / skill / "SKILL.md"
    gate = gate_state.new_gate(
        "update", None, "READY", product_root=product_root,
        summary=summary, project_reference=project_reference, task_reference=None,
        work_reference=None, reference_source="none", work_item_exists=False,
        mode_selection=mode_selection, mode_history=[mode_selection], skill=skill,
        skill_instructions=skill_path.read_text(encoding="utf-8") if skill_path.is_file() else "",
        output=None, errors=[], **route_metadata,
    )
    return OperationResult(gate, 0)


def _contains_secret(value: Any, path: str = "") -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            current = f"{path}.{key}" if path else str(key)
            if any(
                (
                    marker in str(key).casefold()
                    for marker in ("token", "secret", "password", "credential", "api_key")
                )
            ):
                return True
            if _contains_secret(child, current):
                return True
    elif isinstance(value, list):
        return any((_contains_secret(child, path) for child in value))
    return False


def setup_checkpoint_path(setup_id: str, *, product_root: Path) -> Path:
    if not re.fullmatch("[A-Za-z0-9-]{8,64}", str(setup_id or "")):
        raise WorkflowError("Invalid setup ID")
    return product_root / ".workspace" / "setup" / f"{setup_id}.json"


def save_setup_checkpoint(checkpoint: dict[str, Any], *, product_root: Path) -> None:
    if _contains_secret(checkpoint):
        raise WorkflowError("Setup checkpoints must not contain tokens, passwords or secrets")
    if checkpoint.get("schema_version") != 1:
        raise WorkflowError("Unsupported setup checkpoint schema")
    checkpoint["updated_at"] = storage.utc_now()
    storage.write_json(
        setup_checkpoint_path(str(checkpoint.get("setup_id", "")), product_root=product_root),
        checkpoint,
    )


def create_setup_checkpoint(
    profile: str, confirmed_values: dict[str, Any], setup_id: str = "", *, product_root: Path
) -> dict[str, Any]:
    setup_id = setup_id or str(uuid.uuid4())
    path = setup_checkpoint_path(setup_id, product_root=product_root)
    existing = storage.read_json(path, {})
    if isinstance(existing, dict) and existing.get("schema_version") == 1:
        existing["confirmed_values"] = {**existing.get("confirmed_values", {}), **confirmed_values}
        existing["requested_profile"] = profile
        save_setup_checkpoint(existing, product_root=product_root)
        return existing
    now = storage.utc_now()
    checkpoint = {
        "schema_version": 1,
        "setup_id": setup_id,
        "requested_profile": profile,
        "state": "IN_PROGRESS",
        "confirmed_values": confirmed_values,
        "completed_steps": [],
        "pending_jobs": [],
        "errors": [],
        "created_at": now,
        "updated_at": now,
    }
    save_setup_checkpoint(checkpoint, product_root=product_root)
    return checkpoint


def read_setup_state(profile: str = "analysis", *, product_root: Path) -> dict[str, Any]:
    powershell = system.command_path("powershell") or system.command_path("pwsh")
    if not powershell:
        raise WorkflowError("PowerShell is not available; setup cannot be audited.")
    result = subprocess.run(
        [
            powershell,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(product_root / "scripts" / "setup-state.ps1"),
            "-Json",
            "-Profile",
            profile,
        ],
        cwd=product_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=120,
    )
    try:
        value = json.loads(result.stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        if result.returncode != 0:
            raise WorkflowError(
                (result.stderr or result.stdout or "setup-state failed").strip()
            ) from exc
        raise WorkflowError(f"setup-state returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError("setup-state returned an invalid payload.")
    return value


def setup_user_message(state: dict[str, Any]) -> str:
    if state.get("state") == "READY" and state.get("ready"):
        return f"Профиль {state.get('requested_profile', 'conversation')} готов; можно продолжать работу."
    if state.get("state") == "BLOCKED":
        errors = state.get("errors", []) if isinstance(state.get("errors"), list) else []
        details = "; ".join(
            (str(item.get("message", "")) for item in errors if isinstance(item, dict))
        )
        actions = (
            state.get("next_actions", []) if isinstance(state.get("next_actions"), list) else []
        )
        return (
            "Аудит setup заблокирован: "
            + (details or "неизвестная ошибка")
            + (" Следующий шаг: " + str(actions[0]) if actions else "")
        )
    prerequisites = (
        state.get("prerequisites", {}) if isinstance(state.get("prerequisites"), dict) else {}
    )
    bootstrap = state.get("bootstrap", {}) if isinstance(state.get("bootstrap"), dict) else {}
    lines = ["Проверка окружения выполнена."]
    if prerequisites.get("missing"):
        lines.append(
            "Отсутствуют обязательные компоненты: " + ", ".join(map(str, prerequisites["missing"]))
        )
        lines.append(
            "Подтвердите установку через setup-bootstrap либо укажите абсолютные пути к одобренным offline-установщикам."
        )
    elif bootstrap.get("missing"):
        lines.append("Требуется bootstrap: " + ", ".join(map(str, bootstrap["missing"])))
        lines.append("Подтвердите установку зависимостей выбранного профиля.")
    else:
        lines.append(
            "Локальные зависимости готовы. Подтвердите значения внешних репозиториев, каталогов, Gitea и Git identity."
        )
    confirmations = state.get("required_confirmations", [])
    if confirmations:
        lines.append("Обязательные подтверждения: " + ", ".join(map(str, confirmations)))
    optional_inputs = state.get("optional_inputs", [])
    if optional_inputs:
        lines.append(
            "Необязательные значения: "
            + ", ".join(map(str, optional_inputs))
            + ". Шаблон DOCX можно подключить позже перед формальной подготовкой ТЗ."
        )
    lines.append(
        "Пути передавайте абсолютными; существующие значения считаются только подсказками до явного подтверждения."
    )
    return "\n".join(lines)


def parse_updater_result(stdout: str) -> dict[str, Any] | None:
    """Read the updater JSON, including output from the older RLM launcher."""
    lines = stdout.lstrip("\ufeff").strip().splitlines()
    while lines and re.match(
        "^(?:Compatible shared )?RLM MCP (?:is already running|started): ", lines[0]
    ):
        lines.pop(0)
    try:
        result = json.loads("\n".join(lines))
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def run_update_command(
    command: list[str], *, product_root: Path
) -> subprocess.CompletedProcess[str]:
    """Capture updater output without waiting for background services to close pipes."""
    runtime = product_root / ".workspace"
    runtime.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace", dir=runtime
        ) as stdout_file,
        tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace", dir=runtime
        ) as stderr_file,
    ):
        try:
            result = subprocess.run(
                command,
                cwd=product_root,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkflowError(
                "scripts/update.ps1 exceeded 600 seconds. Inspect .workspace/backups and the RLM index jobs before retrying this update gate."
            ) from exc
        stdout_file.seek(0)
        stderr_file.seek(0)
        return subprocess.CompletedProcess(
            command, result.returncode, stdout_file.read(), stderr_file.read()
        )


def execute_action(
    action: str, parameters: dict[str, Any], gate: dict[str, Any], *, product_root: Path
) -> OperationResult:
    if action in {"setup-status", "setup-resume"}:
        setup_id = str(parameters.get("setup_id") or gate.get("setup_id") or "")
        checkpoint = storage.read_json(setup_checkpoint_path(setup_id, product_root=product_root))
        if not isinstance(checkpoint, dict):
            raise WorkflowError(f"Setup checkpoint not found: {setup_id}")
        if checkpoint.get("state") == "COMPLETE":
            _value = checkpoint
            return OperationResult(_value, 0)
        if action == "setup-status":
            jobs = checkpoint.get("pending_jobs", [])
            powershell_status = system.command_path("powershell") or system.command_path("pwsh")
            if jobs and (not powershell_status):
                raise WorkflowError("PowerShell is not available to inspect background setup jobs.")
            refreshed: list[dict[str, Any]] = []
            for job in jobs:
                source = (
                    str(job.get("source") or job.get("source_path") or "")
                    if isinstance(job, dict)
                    else ""
                )
                result = subprocess.run(
                    [
                        powershell_status,
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(product_root / "scripts" / "rlm-index.ps1"),
                        "-Action",
                        "Status",
                        "-SourcePath",
                        source,
                        "-Json",
                    ],
                    cwd=product_root,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
                try:
                    status = json.loads(result.stdout.lstrip("\ufeff"))
                except json.JSONDecodeError:
                    status = {
                        "kind": "rlm-index",
                        "source": source,
                        "state": "FAILED",
                        "detail": (result.stderr or result.stdout or "invalid status JSON").strip(),
                    }
                status["kind"] = "rlm-index"
                status["source"] = status.get("source_path") or source
                refreshed.append(status)
            states = {str(job.get("state")) for job in refreshed}
            if not refreshed or states <= {"FRESH"}:
                checkpoint["state"] = "IN_PROGRESS"
                checkpoint["pending_jobs"] = []
                checkpoint["next_action"] = "setup-resume"
            elif states & {"FAILED"}:
                checkpoint["state"] = "BLOCKED"
                checkpoint["pending_jobs"] = refreshed
                checkpoint["next_action"] = "repair-index-then-setup-resume"
            else:
                checkpoint["state"] = "WAITING_BACKGROUND"
                checkpoint["pending_jobs"] = refreshed
                checkpoint["next_action"] = "setup-status"
            save_setup_checkpoint(checkpoint, product_root=product_root)
            _value = checkpoint
            return OperationResult(_value, 2 if checkpoint["state"] == "BLOCKED" else 0)
        parameters = {
            **checkpoint.get("confirmed_values", {}),
            **parameters,
            "setup_id": setup_id,
            "confirmed": True,
        }
        action = "setup-configure"
    powershell = system.command_path("powershell") or system.command_path("pwsh")
    if action in {"setup-bootstrap", "setup-configure", "update", "publish"} and (
        not parameters.get("confirmed")
    ):
        result = {
            "state": "NEEDS_CONFIRMATION",
            "user_message": "Для этого изменения требуется явное подтверждение пользователя.",
        }
        _value = result
        return OperationResult(_value, 1)
    if not powershell:
        raise WorkflowError("PowerShell is not available.")
    if action == "setup-audit":
        command = [
            powershell,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(product_root / "scripts" / "setup-state.ps1"),
            "-Json",
            "-Profile",
            str(parameters.get("profile") or "analysis"),
        ]
    elif action == "update":
        command = [
            powershell,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(product_root / "scripts" / "update.ps1"),
            "-Json",
        ]
        if parameters.get("allow_external_updates") or parameters.get("allowExternalUpdates"):
            command.append("-AllowExternalUpdates")
        if parameters.get("skip_external_tool_updates") or parameters.get(
            "skipExternalToolUpdates"
        ):
            command.append("-SkipExternalToolUpdates")
        if parameters.get("repair_prerequisites") or parameters.get("repairPrerequisites"):
            command.append("-RepairPrerequisites")
        if parameters.get("retry_failed_indexes"):
            command.append("-RetryFailedIndexes")
        update_id = parameters.get("update_id") or (
            None if parameters.get("new_run") else gate.get("update_id")
        )
        if update_id:
            command.extend(["-UpdateId", str(update_id)])
            wait_seconds = parameters.get("wait_seconds", 30)
            if (
                isinstance(wait_seconds, bool)
                or not isinstance(wait_seconds, int)
                or (not 0 <= wait_seconds <= 30)
            ):
                raise WorkflowError("update wait_seconds must be an integer from 0 to 30")
            command.extend(["-WaitSeconds", str(wait_seconds)])
    elif action in {"setup-plan", "setup-bootstrap"}:
        profile = str(parameters.get("profile") or "analysis")
        command = [
            powershell,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(product_root / "scripts" / "bootstrap.ps1"),
            "-Profile",
            profile,
            "-Json",
        ]
        if action == "setup-plan":
            command.append("-Plan")
        if parameters.get("resume"):
            command.append("-Resume")
        if parameters.get("install_prerequisites"):
            command.append("-InstallPrerequisites")
        if parameters.get("install_cc_1c_skills"):
            command.append("-InstallCc1cSkills")
        if parameters.get("wheelhouse"):
            command.extend(["-Wheelhouse", str(parameters["wheelhouse"])])
        if parameters.get("no_index"):
            command.append("-NoIndex")
    else:
        field_map = {
            "workflow_repository_url": "-WorkflowRepositoryUrl",
            "documentation_repository_url": "-DocumentationRepositoryUrl",
            "documentation_path": "-DocumentationPath",
            "extension_mode": "-ExtensionMode",
            "extension_repository_url": "-ExtensionRepositoryUrl",
            "extension_branch_prefix": "-ExtensionBranchPrefix",
            "extension_path": "-ExtensionPath",
            "configuration_path": "-ConfigurationPath",
            "functional_spec_template": "-FunctionalSpecTemplate",
            "gitea_base_url": "-GiteaBaseUrl",
            "gitea_owner": "-GiteaOwner",
            "gitea_repository": "-GiteaRepository",
            "git_user_name": "-GitUserName",
            "git_user_email": "-GitUserEmail",
            "project_reference": "-ProjectReference",
        }
        profile = str(parameters.get("profile") or "analysis")
        safe_values = {
            key: value for key, value in parameters.items() if key not in {"confirmed", "setup_id"}
        }
        checkpoint = create_setup_checkpoint(
            profile, safe_values, str(parameters.get("setup_id") or ""), product_root=product_root
        )
        gate["setup_id"] = checkpoint["setup_id"]
        gate_state.save_gate(gate, product_root=product_root)
        command = [
            powershell,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(product_root / "scripts" / "configure-project.ps1"),
        ]
        for key, flag in field_map.items():
            value = parameters.get(key)
            if value not in (None, ""):
                command.extend([flag, str(value)])
        if parameters.get("allow_shared_repository"):
            command.append("-AllowSharedWorkflowDocumentationRepository")
        if parameters.get("initialize_documentation_repository"):
            command.append("-InitializeDocumentationRepository")
        command.extend(
            ["-Profile", profile, "-SetupId", checkpoint["setup_id"], "-Confirmed", "-Json"]
        )
    result = (
        run_update_command(command, product_root=product_root)
        if action == "update"
        else subprocess.run(
            command,
            cwd=product_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=600,
        )
    )
    output = "\n".join((value.strip() for value in (result.stdout, result.stderr) if value.strip()))
    structured: dict[str, Any] | None = None
    if action == "update":
        structured = parse_updater_result(result.stdout)
    else:
        try:
            structured = (
                json.loads(result.stdout.lstrip("\ufeff")) if result.stdout.strip() else None
            )
        except json.JSONDecodeError:
            structured = None
    next_action = None
    if action == "update" and structured and structured.get("update_id"):
        gate["update_id"] = structured["update_id"]
        gate["update_result"] = structured
        gate.pop("action_completed", None)
        update_state = structured.get("state")
        if update_state == "READY" and structured.get("ready") is True:
            gate["state"] = "READY"
        elif update_state == "WAITING_BACKGROUND":
            gate["state"] = "WAITING_BACKGROUND"
        elif update_state in {"NEEDS_CONFIRMATION", "UPDATE_AVAILABLE", "REVIEW_REQUIRED"}:
            gate["state"] = "NEEDS_CONFIRMATION"
        else:
            gate["state"] = "BLOCKED"
        # Older update gates can contain unrelated registry/work-item blockers.
        # The updater result is authoritative for installation maintenance.
        gate["errors"] = [structured["error"]] if structured.get("error") else []
        gate["conditions"] = []
        gate["remaining_blockers"] = []
        gate["clarification"] = None
        gate["awaiting_user_input"] = gate["state"] in {"BLOCKED", "NEEDS_CONFIRMATION"}
        gate["user_message"] = structured.get("error") or "Continue the saved update according to its next_actions."
        gate_state.refresh_gate_actions(gate, product_root=product_root)
        gate_state.save_gate(gate, product_root=product_root)
    if (
        action == "setup-configure"
        and structured
        and (structured.get("state") == "WAITING_BACKGROUND")
    ):
        checkpoint = storage.read_json(
            setup_checkpoint_path(str(gate.get("setup_id")), product_root=product_root), {}
        )
        checkpoint["state"] = "WAITING_BACKGROUND"
        checkpoint["pending_jobs"] = structured.get("jobs", [])
        save_setup_checkpoint(checkpoint, product_root=product_root)
        gate["state"] = "WAITING_BACKGROUND"
        gate_state.save_gate(gate, product_root=product_root)
        _value = structured
        return OperationResult(_value, 0)
    if result.returncode == 0:
        if action == "setup-configure":
            gate["state"] = "READY"
            gate["action_completed"] = action
            gate_state.save_gate(gate, product_root=product_root)
            next_action = "flow1c_complete"
        elif (
            action == "update"
            and structured
            and (structured.get("state") == "READY")
            and (structured.get("ready") is True)
        ):
            gate["state"] = "READY"
            gate["action_completed"] = action
            gate_state.save_gate(gate, product_root=product_root)
            next_action = "flow1c_complete"
        elif action == "setup-bootstrap":
            next_action = "flow1c_begin"
    if structured is not None:
        _value = structured
        return OperationResult(_value, result.returncode)
    if action == "update":
        raise WorkflowError(
            "Updater returned no valid JSON result; update completion was not recorded. Inspect the command output and retry this gate safely. "
            + output[-1000:]
        )
    _value = {
        "state": "ACTION_COMPLETE" if result.returncode == 0 else "BLOCKED",
        "next": next_action,
        "exit_code": result.returncode,
        "output": output,
    }
    return OperationResult(_value, result.returncode)
