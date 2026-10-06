"""Readiness checks return data for CLI and workflow callers."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from flow1c import context as runtime
from flow1c import setup as setup_service
from flow1c import sources as sources
from flow1c import storage as storage
from flow1c import system as system
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state
from scripts import external_tools as external_tools
from scripts import flow1c_interview_policy as interview_policy
from scripts import flow1c_policy as policy
from scripts import flow1c_templates as template_library


def doctor_report(rows: list[tuple[str, str, str]]) -> OperationResult:
    ready = all((state != "ERROR" for _, state, _ in rows))
    return OperationResult(
        {
            "schema_version": 1,
            "state": "READY" if ready else "BLOCKED",
            "ready": ready,
            "checks": [
                {"name": name, "state": state, "detail": detail} for name, state, detail in rows
            ],
        },
        0 if ready else 1,
    )


def _module_check(module: str, label: str) -> tuple[str, str, str]:
    try:
        __import__(module)
        return (label, "OK", "installed")
    except ImportError:
        return (label, "ERROR", "not installed; run bootstrap.ps1 with the required profile")


def _capability_checks(
    name: str, model: dict[str, Any], *, product_root: Path
) -> list[tuple[str, str, str]]:
    """Probe only the requested capability. Dependencies are resolved by policy."""
    if name == "core":
        version = sys.version_info[:2]
        python_ok = policy.python_version_supported(version, model)
        stages_ok = False
        try:
            stages = runtime.load_stages(product_root=product_root)
            stages_ok = bool(stages.get("operations"))
        except WorkflowError:
            pass
        skills = list((product_root / ".agents" / "skills").glob("flow1c-*/SKILL.md"))
        policy_state, policy_detail = git_read_policy_check(product_root=product_root)
        return [
            (
                "Python",
                "OK" if python_ok else "ERROR",
                f"{sys.version.split()[0]}; supported >= {model['python']['minimum']}",
            ),
            (
                ".venv",
                "OK" if (product_root / ".venv").is_dir() else "ERROR",
                str(product_root / ".venv"),
            ),
            (
                runtime.CONFIG_FILE,
                "OK" if (product_root / runtime.CONFIG_FILE).is_file() else "ERROR",
                str(product_root / runtime.CONFIG_FILE),
            ),
            (
                "Workflow stages",
                "OK" if stages_ok else "ERROR",
                str(product_root / "config" / "stages.json"),
            ),
            ("Built-in flow1c skills", "OK" if skills else "ERROR", f"{len(skills)} discovered"),
            ("Git read policy", policy_state, policy_detail),
        ]
    if name == "git":
        git = system.command_path("git")
        return [("Git", "OK" if git else "ERROR", git or "not found")]
    if name == "project":
        rows: list[tuple[str, str, str]] = []
        local_path = product_root / runtime.LOCAL_CONFIG_FILE
        if not local_path.is_file():
            return [(runtime.LOCAL_CONFIG_FILE, "ERROR", "not configured")]
        try:
            _, local = runtime.load_config(product_root=product_root)
        except WorkflowError as exc:
            return [(runtime.LOCAL_CONFIG_FILE, "ERROR", str(exc))]
        setup = local.get("setup", {}) if isinstance(local.get("setup"), dict) else {}
        configured_root = str(setup.get("workflow_root", "")).strip()
        validated = (
            setup.get("validation_version") == setup_service.SETUP_VALIDATION_VERSION
            and bool(configured_root)
            and (Path(configured_root).resolve() == product_root.resolve())
        )
        rows.append(
            (
                "Setup confirmation",
                "OK" if validated else "ERROR",
                (
                    "validated for this checkout"
                    if validated
                    else "run setup-resume or configure-project.ps1"
                ),
            )
        )
        for key in ("documentation_path", "configuration_path", "extension_path"):
            raw = str(local.get(key, "")).strip()
            path = Path(raw) if raw else None
            valid = bool(
                path
                and path.exists()
                and (not storage.path_is_within(path, product_root))
                and (system.containing_workflow_root(path) is None)
            )
            rows.append((key, "OK" if valid else "ERROR", raw or "not configured"))
        documentation = runtime.project_root(local, product_root=product_root)
        rows.append(
            (
                "Docs Git repo",
                "OK" if (documentation / ".git").exists() else "ERROR",
                str(documentation),
            )
        )
        _, state, detail = system.extension_source_state(local)
        rows.append(("Extension source", state, system.redact_url_credentials(detail)))
        return rows
    if name == "template-core":
        return [_module_check("jsonschema", "jsonschema")]
    if name == "template-markdown":
        return [_module_check("markdown_it", "markdown-it-py")]
    if name == "template-docx":
        return [_module_check("docx", "python-docx")]
    if name == "office":
        return [
            _module_check("openpyxl", "openpyxl"),
            _module_check("markitdown", "MarkItDown"),
            _module_check("docx", "python-docx"),
            _module_check("docxtpl", "docxtpl"),
        ]
    if name == "rlm":
        rows: list[tuple[str, str, str]] = []
        try:
            manifest = external_tools.load_manifest(product_root / "config" / "external-tools.json")
            version = external_tools.package_version("rlm-tools-bsl")
            requirement = manifest["rlm_tools_bsl"]["requirement"]
            compatible = bool(version and external_tools.version_satisfies(version, requirement))
        except Exception as exc:
            return [("rlm-tools-bsl", "ERROR", str(exc))]
        rows.append(
            (
                "rlm-tools-bsl",
                "OK" if compatible else "ERROR",
                f"{version or 'not installed'}; required {requirement}",
            )
        )
        index = product_root / ".venv" / "Scripts" / "rlm-bsl-index.exe"
        rows.append(("rlm-bsl-index", "OK" if index.is_file() else "ERROR", str(index)))
        local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
        config = storage.read_json(product_root / runtime.CONFIG_FILE, {})
        endpoint = str(
            (local.get("rlm") or {}).get("endpoint")
            or (config.get("quality") or {}).get("rlm_endpoint")
            or ""
        )
        healthy = sources.endpoint_is_healthy(endpoint)
        rows.append(
            ("RLM MCP endpoint", "OK" if healthy else "ERROR", endpoint or "not configured")
        )
        index_command = str((local.get("rlm") or {}).get("index_command", ""))
        for label, raw in (
            ("configuration", local.get("configuration_path")),
            ("extension", local.get("extension_path")),
        ):
            source = system.resolve_1c_source_root(Path(str(raw))) if raw else None
            state, detail = (
                sources.rlm_index_state(index_command, source, product_root=product_root)
                if source
                else ("ERROR", "source is not configured")
            )
            rows.append((f"RLM {label} index", state, detail))
        return rows
    if name == "bsl-ls":
        local = storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
        bsl = (
            local.get("bsl_language_server", {})
            if isinstance(local.get("bsl_language_server"), dict)
            else {}
        )
        executable = Path(str(bsl.get("command", "")))
        if not executable.is_file():
            return [("BSL LS", "ERROR", "not installed or configured")]
        smoke_ok, detail = external_tools.bsl_smoke(executable)
        return [("BSL LS", "OK" if smoke_ok else "ERROR", detail)]
    if name == "cc-1c-skills":
        repository = product_root / ".tools" / "cc-1c-skills"
        commit = ""
        if (repository / ".git").is_dir():
            result = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=False,
            )
            commit = result.stdout.strip() if result.returncode == 0 else ""
        ok = bool(commit) and external_tools.skills_connected(product_root)
        return [
            (
                "cc-1c-skills",
                "OK" if ok else "ERROR",
                commit or "not installed; explicit opt-in required",
            )
        ]
    return [(name, "ERROR", "unknown capability")]


def profile_doctor(profile: str, *, product_root: Path) -> OperationResult:
    model = runtime.load_capabilities(product_root=product_root)
    try:
        required = policy.resolve_capabilities(model, profile)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    capabilities: dict[str, Any] = {}
    all_rows: list[tuple[str, str, str]] = []
    states: dict[str, str] = {}
    optional_missing: list[str] = []
    for name, definition in model["capabilities"].items():
        if name not in required:
            optional_present = bool(
                name == "cc-1c-skills"
                and (product_root / ".tools" / "cc-1c-skills" / ".git").is_dir()
                and external_tools.skills_connected(product_root)
            )
            state = (
                ("OPTIONAL" if optional_present else "OPTIONAL_MISSING")
                if definition.get("optional")
                else "NOT_REQUESTED"
            )
            checks: list[tuple[str, str, str]] = []
            if state == "OPTIONAL_MISSING":
                optional_missing.append(name)
        else:
            checks = _capability_checks(name, model, product_root=product_root)
            state = "READY" if all((row[1] != "ERROR" for row in checks)) else "BLOCKED"
            all_rows.extend(checks)
        states[name] = state
        capabilities[name] = {
            "state": state,
            "checks": [
                {"name": label, "state": check_state, "detail": detail}
                for label, check_state, detail in checks
            ],
        }
    ready = policy.capability_readiness(required, states)
    blocking = [item for item in all_rows if item[1] == "ERROR"]
    next_actions: list[str] = []
    blocked_names = {name for name in required if states.get(name) != "READY"}
    installable = blocked_names & {
        "core",
        "office",
        "rlm",
        "bsl-ls",
        "cc-1c-skills",
        "template-core",
        "template-markdown",
        "template-docx",
    }
    if installable:
        suffix = " -InstallCc1cSkills" if "cc-1c-skills" in installable else ""
        next_actions.append(f"bootstrap.ps1 -Profile {profile}{suffix}")
    if "project" in blocked_names:
        next_actions.append(
            "resume setup or run configure-project.ps1 with confirmed project values"
        )
    if "git" in blocked_names:
        next_actions.append("install Git or open a valid Git checkout")
    result = {
        "schema_version": 1,
        "state": "READY" if ready else "BLOCKED",
        "ready": ready,
        "checks": [{"name": n, "state": s, "detail": d} for n, s, d in all_rows],
        "requested_profile": profile,
        "required_capabilities": required,
        "capabilities": capabilities,
        "blocking_checks": [{"name": n, "state": s, "detail": d} for n, s, d in blocking],
        "optional_missing": optional_missing,
        "next_actions": next_actions,
    }
    _value = result
    return OperationResult(_value, 0 if ready else 1)


def git_read_policy_check(*, product_root: Path) -> tuple[str, str]:
    policy_path = product_root / "config" / "git-read-policy.json"
    try:
        policy = storage.read_json(policy_path)
        if not isinstance(policy, dict) or policy.get("schema_version") != 1:
            return ("ERROR", "git read policy schema must be 1")
        commands = policy.get("commands", {})
        git_commands = set(commands.get("git", [])) if isinstance(commands, dict) else set()
        denied = set(policy.get("denied_git_subcommands", []))
        if not git_commands or git_commands & denied:
            return ("ERROR", "Git read allowlist is empty or overlaps the mutation denylist")
        limits = policy.get("limits", {})
        if not all(
            (
                isinstance(limits.get(key), int) and limits[key] > 0
                for key in (
                    "pipeline_segments",
                    "timeout_seconds",
                    "max_output_chars",
                    "max_processes",
                )
            )
        ):
            return ("ERROR", "Git read policy resource limits are invalid")
        guard = product_root / ".opencode" / "plugins" / "flow1c-guard.js"
        if not guard.is_file() or "validateSegment" not in guard.read_text(encoding="utf-8"):
            return ("ERROR", "OpenCode Git guard validator is unavailable")
        return ("OK", "schema 1; guarded read-only commands")
    except (OSError, WorkflowError) as exc:
        return ("ERROR", str(exc))


def doctor(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    requested_profile = getattr(args, "profile", None)
    requested_operation = getattr(args, "operation", None)
    template_gate = (
        gate_state.load_gate(args.gate_id, product_root=product_root)
        if getattr(args, "gate_id", None)
        else None
    )
    if template_gate and (not requested_operation):
        requested_operation = template_gate.get("operation")
    if requested_operation == "interview-preparation":
        selected_format = getattr(args, "format", None) or "xlsx"
        if selected_format not in {"xlsx", "markdown"}:
            raise interview_policy.InterviewError(
                "INTERVIEW_INVALID_INPUT", "Сценарий интервью поддерживает XLSX или Markdown."
            )
        checks = [
            ("Python", "OK" if sys.version_info >= (3, 10) else "ERROR", sys.version.split()[0]),
            (
                "flow1c-interview-preparation",
                (
                    "OK"
                    if (
                        product_root / ".agents/skills/flow1c-interview-preparation/SKILL.md"
                    ).is_file()
                    else "ERROR"
                ),
                "canonical skill",
            ),
        ]
        if selected_format == "xlsx":
            checks.append(_module_check("openpyxl", "openpyxl"))
        ready = all((state != "ERROR" for _, state, _ in checks))
        result = {
            "schema_version": 1,
            "operation": requested_operation,
            "format": selected_format,
            "ready": ready,
            "checks": [
                {"name": name, "state": state, "detail": detail} for name, state, detail in checks
            ],
            "next_actions": (
                []
                if ready
                else [
                    "Restore the canonical skill or run the consented bootstrap.ps1 -Profile documents. No project sources or RLM are required."
                ]
            ),
        }
        _value = result
        return OperationResult(_value, 0 if ready else 1)
    if requested_operation in {"template-management", "template-document"}:
        pin_format = (template_gate or {}).get("template_pin", {}).get("source_format")
        selected_format = getattr(args, "format", None) or pin_format or "markdown"
        result = template_library.readiness(
            product_root,
            storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {}),
            selected_format,
            requested_operation,
        )
        result["format_source"] = (
            "explicit" if getattr(args, "format", None) else "gate-pin" if pin_format else "default"
        )
        _value = result
        return OperationResult(_value, 0 if result["ready"] else 1)
    if requested_operation:
        try:
            requested_profile = policy.operation_profile(
                runtime.load_stages(product_root=product_root), requested_operation
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
    if requested_profile:
        return profile_doctor(str(requested_profile), product_root=product_root)
    rows: list[tuple[str, str, str]] = []
    policy_state, policy_detail = git_read_policy_check(product_root=product_root)
    rows.append(("Git read policy", policy_state, policy_detail))
    external_manifest: dict[str, Any] | None = None
    try:
        external_manifest = external_tools.load_manifest(
            product_root / "config" / "external-tools.json"
        )
        rows.append(("External tools manifest", "OK", "schema 1"))
    except Exception as exc:
        rows.append(("External tools manifest", "ERROR", str(exc)))
    rows.append(
        (
            "Python",
            "OK" if sys.version_info[:2] >= (3, 10) else "ERROR",
            sys.version.split()[0] + "; supported >=3.10",
        )
    )
    rows.append(
        (
            "Git",
            "OK" if system.command_path("git") else "ERROR",
            system.command_path("git") or "not found",
        )
    )
    try:
        import openpyxl  # noqa: F401 -- probe imports to retain readiness semantics

        rows.append(("openpyxl", "OK", "installed"))
    except ImportError:
        rows.append(("openpyxl", "ERROR", "run bootstrap.ps1"))
    try:
        import markitdown  # noqa: F401 -- probe imports to retain readiness semantics

        rows.append(("MarkItDown", "OK", "installed"))
    except ImportError:
        rows.append(("MarkItDown", "ERROR", "run bootstrap.ps1"))
    try:
        import docx  # noqa: F401 -- probe imports to retain readiness semantics

        rows.append(("python-docx", "OK", "installed"))
    except ImportError:
        rows.append(("python-docx", "ERROR", "run bootstrap.ps1"))
    try:
        import docxtpl  # noqa: F401 -- probe imports to retain readiness semantics

        rows.append(("docxtpl", "OK", "installed"))
    except ImportError:
        rows.append(("docxtpl", "ERROR", "run bootstrap.ps1"))
    try:
        import rlm_tools_bsl  # noqa: F401 -- probe imports to retain readiness semantics

        installed_rlm = external_tools.package_version("rlm-tools-bsl")
        requirement = external_manifest["rlm_tools_bsl"]["requirement"] if external_manifest else ""
        compatible = bool(
            installed_rlm
            and external_manifest
            and external_tools.version_satisfies(installed_rlm, requirement)
        )
        rows.append(
            (
                "rlm-tools-bsl",
                "OK" if compatible else "ERROR",
                f"{installed_rlm or 'unknown'}; required {requirement or 'valid manifest'}",
            )
        )
    except ImportError:
        rows.append(("rlm-tools-bsl", "ERROR", "run bootstrap.ps1"))
    index_executable = product_root / ".venv" / "Scripts" / "rlm-bsl-index.exe"
    rows.append(
        ("rlm-bsl-index", "OK" if index_executable.is_file() else "ERROR", str(index_executable))
    )
    rows.append(
        (
            "Java",
            "OK" if system.command_path("java") else "OPTIONAL",
            system.command_path("java") or "not found",
        )
    )
    config_path = product_root / runtime.CONFIG_FILE
    local_path = product_root / runtime.LOCAL_CONFIG_FILE
    rows.append((runtime.CONFIG_FILE, "OK" if config_path.exists() else "ERROR", str(config_path)))
    rows.append(
        (runtime.LOCAL_CONFIG_FILE, "OK" if local_path.exists() else "ERROR", str(local_path))
    )
    if config_path.exists():
        config, local = runtime.load_config(product_root=product_root)
        local_schema = local.get("schema_version")
        rows.append(
            (
                "Local config schema",
                "OK" if local_schema == runtime.LOCAL_CONFIG_SCHEMA_VERSION else "ERROR",
                (
                    f"schema {local_schema}"
                    if local_schema is not None
                    else "run scripts/update.ps1 or /setup"
                ),
            )
        )
        setup = local.get("setup", {}) if isinstance(local.get("setup", {}), dict) else {}
        configured_root = str(setup.get("workflow_root", "")).strip()
        root_matches = (
            bool(configured_root) and Path(configured_root).resolve() == product_root.resolve()
        )
        validation_ok = (
            setup.get("validation_version") == setup_service.SETUP_VALIDATION_VERSION
            and root_matches
        )
        rows.append(
            (
                "Setup confirmation",
                "OK" if validation_ok else "ERROR",
                (
                    "validated for this checkout"
                    if validation_ok
                    else "rerun configure-project.ps1 and confirm every value"
                ),
            )
        )
        configured_workflow_url = str(setup.get("workflow_repository_url", "")).strip()
        actual_workflow_url = system.git_remote_url(product_root)
        workflow_url_ok = bool(
            configured_workflow_url and actual_workflow_url
        ) and system.normalize_git_url(configured_workflow_url) == system.normalize_git_url(
            actual_workflow_url
        )
        rows.append(
            (
                "Workflow checkout",
                "OK" if workflow_url_ok else "ERROR",
                system.redact_url_credentials(actual_workflow_url) or "origin is not configured",
            )
        )
        for key in ("documentation_path", "configuration_path", "extension_path"):
            raw = str(local.get(key, "")).strip()
            path = Path(raw) if raw else None
            exists = bool(path and path.exists())
            outside_workflow = bool(path and (not storage.path_is_within(path, product_root)))
            other_workflow = system.containing_workflow_root(path) if exists and path else None
            belongs_to_other_checkout = bool(
                other_workflow and other_workflow.resolve() != product_root.resolve()
            )
            state = (
                "OK" if exists and outside_workflow and (not belongs_to_other_checkout) else "ERROR"
            )
            detail = raw or "not configured"
            if exists and (not outside_workflow):
                detail += " (must be outside the Flow1C checkout)"
            elif belongs_to_other_checkout:
                detail += f" (belongs to another Flow1C checkout: {other_workflow})"
            rows.append((key, state, detail))
        template_raw = str(local.get("functional_spec_template", "")).strip()
        if not template_raw:
            rows.append(
                (
                    "functional_spec_template",
                    "OPTIONAL",
                    "not configured; required only for formal functional-spec generation",
                )
            )
        else:
            template_path = Path(template_raw)
            exists = template_path.is_file()
            outside_workflow = not storage.path_is_within(template_path, product_root)
            other_workflow = system.containing_workflow_root(template_path) if exists else None
            belongs_to_other_checkout = bool(
                other_workflow and other_workflow.resolve() != product_root.resolve()
            )
            is_docx = template_path.suffix.lower() == ".docx"
            template_ok = (
                exists and outside_workflow and (not belongs_to_other_checkout) and is_docx
            )
            detail = template_raw
            if exists and (not outside_workflow):
                detail += " (must be outside the Flow1C checkout)"
            elif belongs_to_other_checkout:
                detail += f" (belongs to another Flow1C checkout: {other_workflow})"
            elif exists and (not is_docx):
                detail += " (must be a DOCX file)"
            if not template_ok:
                detail += "; ignored until formal functional-spec generation"
            rows.append(("functional_spec_template", "OK" if template_ok else "OPTIONAL", detail))
        documentation = runtime.project_root(local, product_root=product_root)
        extension, extension_state, extension_detail = system.extension_source_state(local)
        rows.append(
            (
                "Docs Git repo",
                "OK" if (documentation / ".git").exists() else "ERROR",
                str(documentation),
            )
        )
        documentation_url = str(local.get("documentation_repository_url", "")).strip()
        actual_documentation_url = system.git_remote_url(documentation)
        docs_initialized_locally = bool(setup.get("documentation_repository_initialized_locally"))
        docs_remote_matches = bool(
            documentation_url and actual_documentation_url
        ) and system.normalize_git_url(documentation_url) == system.normalize_git_url(
            actual_documentation_url
        )
        docs_remote_ok = docs_remote_matches or (
            docs_initialized_locally and (not documentation_url) and (not actual_documentation_url)
        )
        rows.append(
            (
                "Docs Git remote",
                "OK" if docs_remote_ok else "ERROR",
                system.redact_url_credentials(actual_documentation_url)
                or (
                    "local repository initialization explicitly confirmed"
                    if docs_initialized_locally and (not documentation_url)
                    else "origin is not configured"
                ),
            )
        )
        shared_confirmed = bool(setup.get("allow_shared_workflow_documentation_repository"))
        repositories_differ = (
            docs_initialized_locally
            and documentation.resolve() != product_root.resolve()
            or (
                bool(actual_workflow_url and actual_documentation_url)
                and system.normalize_git_url(actual_workflow_url)
                != system.normalize_git_url(actual_documentation_url)
            )
        )
        rows.append(
            (
                "Workflow/docs separation",
                "OK" if repositories_differ or shared_confirmed else "ERROR",
                (
                    "separate repositories"
                    if repositories_differ
                    else (
                        "same repository explicitly confirmed"
                        if shared_confirmed
                        else "workflow and documentation repositories are the same"
                    )
                ),
            )
        )
        rows.append(
            ("Extension source", extension_state, system.redact_url_credentials(extension_detail))
        )
        git_name = ""
        git_email = ""
        if system.command_path("git") and (documentation / ".git").exists():
            name_result = subprocess.run(
                ["git", "config", "user.name"],
                cwd=documentation,
                text=True,
                capture_output=True,
                check=False,
            )
            email_result = subprocess.run(
                ["git", "config", "user.email"],
                cwd=documentation,
                text=True,
                capture_output=True,
                check=False,
            )
            git_name = name_result.stdout.strip()
            git_email = email_result.stdout.strip()
        identity = (
            f"{git_name} <{git_email}>"
            if git_name and git_email
            else "configure user.name and user.email"
        )
        rows.append(("Git identity", "OK" if git_name and git_email else "ERROR", identity))
        bsl = local.get("bsl_language_server", {})
        command = str(bsl.get("command", "")).strip() if isinstance(bsl, dict) else ""
        legacy_jar = str(config.get("quality", {}).get("bsl_language_server_jar", "")).strip()
        executable = command or legacy_jar
        state = "OK" if executable and Path(executable).is_file() else "ERROR"
        rows.append(("BSL LS", state, executable or "not configured"))
        if state == "OK" and external_manifest:
            expected_version = external_manifest["bsl_language_server"]["version"]
            configured_version = str(bsl.get("version", "")) if isinstance(bsl, dict) else ""
            smoke_ok, smoke_detail = external_tools.bsl_smoke(Path(executable))
            rows.append(
                (
                    "BSL LS manifest/smoke",
                    "OK" if configured_version == expected_version and smoke_ok else "ERROR",
                    f"configured {configured_version or 'unknown'}, expected {expected_version}; {smoke_detail}",
                )
            )
        skills_repository = product_root / ".tools" / "cc-1c-skills"
        skills_commit = ""
        if (skills_repository / ".git").is_dir():
            commit_result = subprocess.run(
                ["git", "-C", str(skills_repository), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=False,
            )
            skills_commit = commit_result.stdout.strip() if commit_result.returncode == 0 else ""
        skills_ok = bool(skills_commit) and external_tools.skills_connected(product_root)
        rows.append(
            (
                "cc-1c-skills",
                "OK" if skills_ok else "OPTIONAL",
                (
                    skills_commit
                    if skills_ok
                    else "repository/commit or connected .agents skills are missing"
                ),
            )
        )
        quality = config.get("quality", {}) if isinstance(config.get("quality", {}), dict) else {}
        rlm_endpoint = str(quality.get("rlm_endpoint", "")).strip()
        endpoint_healthy = sources.endpoint_is_healthy(rlm_endpoint)
        rows.append(
            (
                "RLM MCP endpoint",
                "OK" if endpoint_healthy else "ERROR",
                rlm_endpoint or "not configured",
            )
        )
        rlm = local.get("rlm", {}) if isinstance(local.get("rlm", {}), dict) else {}
        index_command = str(rlm.get("index_command", "")).strip()
        configuration_raw = str(local.get("configuration_path", "")).strip()
        configuration_source = (
            system.resolve_1c_source_root(Path(configuration_raw)) if configuration_raw else None
        )
        extension_source = system.resolve_1c_source_root(extension) if extension else None
        rlm_sources = (
            ("RLM configuration index", configuration_source),
            ("RLM extension index", extension_source),
        )
        endpoint_indexes_ok = endpoint_healthy
        for label, source in rlm_sources:
            index_state, index_detail = (
                sources.rlm_index_state(index_command, source, product_root=product_root)
                if source
                else ("ERROR", "source is not configured")
            )
            rows.append((label, index_state, index_detail))
            endpoint_state, endpoint_detail = (
                sources.rlm_endpoint_index_state(rlm_endpoint, source)
                if endpoint_healthy and source
                else ("ERROR", "endpoint or source is unavailable")
            )
            rows.append((f"{label} via MCP", endpoint_state, endpoint_detail))
            endpoint_indexes_ok = endpoint_indexes_ok and endpoint_state == "OK"
        session_state, session_detail = (
            sources.rlm_endpoint_session_state(rlm_endpoint, configuration_source)
            if endpoint_indexes_ok and configuration_source
            else ("ERROR", "endpoint indexes are unavailable")
        )
        rows.append(("RLM MCP sandbox session", session_state, session_detail))
        gitea = {**config.get("gitea", {}), **local.get("gitea", {})}
        token_env = str(gitea.get("token_env", "FLOW1C_GITEA_TOKEN"))
        token_state = "OK" if os.environ.get(token_env) else "SETUP"
        rows.append(("Gitea token", token_state, token_env))
    return doctor_report(rows)
