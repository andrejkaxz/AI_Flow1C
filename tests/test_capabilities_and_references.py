from __future__ import annotations
from flow1c import storage as svc_storage
from flow1c import context as svc_context
from scripts import external_tools as ext_scripts_external_tools
from flow1c import readiness as svc_readiness
from flow1c import setup as svc_setup
from flow1c import system as svc_system
from flow1c import work_items as svc_work_items
from flow1c.workflow import state as svc_workflow_state
from flow1c.errors import WorkflowError
from flow1c.storage import read_json
import subprocess
from flow1c.storage import write_json
import argparse
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
POLICY_SPEC = importlib.util.spec_from_file_location(
    "flow1c_policy_new", ROOT / "scripts" / "flow1c_policy.py"
)
assert POLICY_SPEC and POLICY_SPEC.loader
policy = importlib.util.module_from_spec(POLICY_SPEC)
POLICY_SPEC.loader.exec_module(policy)
from flow1c import cli as flow1c


class CapabilityPolicyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.model = json.loads((ROOT / "config" / "capabilities.json").read_text(encoding="utf-8"))
        cls.stages = json.loads((ROOT / "config" / "stages.json").read_text(encoding="utf-8"))

    def test_profiles_resolve_transitively(self) -> None:
        self.assertEqual(policy.resolve_capabilities(self.model, "conversation"), ["core"])
        self.assertEqual(
            policy.resolve_capabilities(self.model, "analysis"),
            ["core", "git", "project", "office", "rlm"],
        )
        self.assertEqual(policy.resolve_capabilities(self.model, "implementation")[-1], "bsl-ls")
        self.assertEqual(policy.resolve_capabilities(self.model, "full")[-1], "cc-1c-skills")

    def test_free_mode_tool_sets_are_separate_and_fresh(self) -> None:
        explore = policy.allowed_tools_for_mode("explore")
        draft = policy.allowed_tools_for_mode("draft")
        self.assertNotIn("flow1c_source_read", explore)
        self.assertIn("flow1c_source_read", draft)
        self.assertEqual(len(draft), len(set(draft)))
        draft.append("mutated")
        self.assertNotIn("mutated", policy.DRAFT_TOOLS)
        self.assertNotIn("flow1c_source_read", policy.FREE_TOOLS)
        self.assertIn("flow1c_git_inspect", explore)
        self.assertIn("flow1c_git_inspect", draft)
        self.assertNotIn("flow1c_cc_inspect", explore)
        self.assertIn(
            "flow1c_cc_inspect", policy.allowed_tools_for_mode("explore", "query-analysis")
        )
        self.assertIn("query-analysis", policy.FREE_OPERATIONS)
        self.assertIn("flow1c_analyze_bsl", policy.allowed_tools_for_mode("explore", "code-review"))
        self.assertIn("flow1c_analyze_bsl", policy.allowed_tools_for_mode("draft", "code-review"))
        self.assertNotIn(
            "flow1c_analyze_bsl", policy.allowed_tools_for_mode("explore", "consultation")
        )

    def test_blocked_formal_review_keeps_read_only_bsl_and_provisional_recovery(self) -> None:
        stage = self.stages["operations"]["code-review"]
        actions = policy.available_actions(
            mode="formal", operation="code-review", state="BLOCKED", stage=stage
        )
        self.assertIn("flow1c_analyze_bsl", actions)
        self.assertIn("flow1c_action", actions)

    def test_bsl_tool_uses_selected_source_and_records_result(self) -> None:
        source = ROOT / "config"
        gate = {
            "gate_id": "review-1",
            "operation": "code-review",
            "mode": "explore",
            "state": "READY",
            "evidence_path": str(source / "evidence.json"),
        }
        saved: dict = {}
        args = argparse.Namespace(gate_id="review-1", source={"kind": "configuration"})
        with (
            mock.patch.object(svc_workflow_state, "load_gate", return_value=gate),
            mock.patch.object(
                svc_storage, "read_json", return_value={"configuration_path": str(source)}
            ),
            mock.patch.object(svc_system, "command_path", return_value="powershell"),
            mock.patch.object(
                svc_workflow_state,
                "evidence_for_gate",
                return_value=(source / "evidence.json", saved),
            ),
            mock.patch.object(svc_storage, "write_json") as write,
            mock.patch.object(
                subprocess, "run", return_value=mock.Mock(returncode=0, stdout="ok", stderr="")
            ) as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(flow1c.cmd_agent_analyze_bsl(args), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-SourcePath") + 1], str(source.resolve()))
        self.assertEqual(saved["bsl_ls"]["source"]["kind"], "configuration")
        self.assertIn("report_path", json.loads(output.getvalue()))
        write.assert_called_once()

    def test_bsl_tool_rejects_snapshot_from_another_request(self) -> None:
        gate = {
            "gate_id": "review-1",
            "operation": "code-review",
            "mode": "explore",
            "state": "READY",
        }
        args = argparse.Namespace(
            gate_id="review-1", source={"kind": "git_snapshot", "snapshot_id": "foreign"}
        )
        with mock.patch.object(svc_workflow_state, "load_gate", return_value=gate):
            with self.assertRaisesRegex(WorkflowError, "does not belong to this gate"):
                flow1c.cmd_agent_analyze_bsl(args)

    def test_bsl_tool_runs_on_formal_gate_without_work_item(self) -> None:
        gate = {
            "gate_id": "review-2",
            "operation": "code-review",
            "mode": "formal",
            "state": "NEEDS_CODE",
        }
        args = argparse.Namespace(gate_id="review-2", source={"kind": "extension"})
        with (
            mock.patch.object(svc_workflow_state, "load_gate", return_value=gate),
            mock.patch.object(svc_context, "load_stages", return_value=self.stages),
            mock.patch.object(
                svc_storage, "read_json", return_value={"extension_path": str(ROOT / "config")}
            ),
            mock.patch.object(svc_system, "command_path", return_value="powershell"),
            mock.patch.object(svc_storage, "write_json") as write,
            mock.patch.object(
                subprocess, "run", return_value=mock.Mock(returncode=0, stdout="ok", stderr="")
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(flow1c.cmd_agent_analyze_bsl(args), 0)
        self.assertIn("diagnostics", str(write.call_args.args[0]))

    def test_consultation_skill_uses_batch_integration_instead_of_log_scanning(self) -> None:
        instructions = (ROOT / ".agents" / "skills" / "flow1c-consultation" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("action=merge-search", instructions)
        self.assertIn("`integration` remains a compatibility alias", instructions)
        self.assertIn("git_refs", instructions)
        self.assertIn("Do not scan bounded logs", instructions)

    def test_code_review_defaults_to_free_mode_unless_formal_is_requested(self) -> None:
        selected = policy.select_request_mode("code-review", "Проведи review ветки 9760", None)
        self.assertEqual((selected["mode"], selected["actor"]), ("explore", "workflow"))
        self.assertEqual(
            policy.select_request_mode("code-review", "Подготовь Markdown-отчёт", None)["mode"],
            "draft",
        )
        self.assertEqual(
            policy.select_request_mode("code-review", "Формальное согласование ветки", None)[
                "mode"
            ],
            "formal",
        )
        self.assertEqual(
            policy.select_request_mode("code-review", "review", "formal")["actor"], "caller"
        )

    def test_query_analysis_defaults_to_read_only_explore(self) -> None:
        selected = policy.select_request_mode("query-analysis", "Оптимизируй запрос 1С", None)
        self.assertEqual(
            (selected["mode"], selected["reason"]), ("explore", "read_only_query_analysis_default")
        )
        self.assertEqual(
            policy.select_request_mode("query-analysis", "Проверь", "explore")["actor"], "caller"
        )

    def test_numeric_branch_is_extracted_as_git_ref(self) -> None:
        self.assertEqual(policy.extract_git_ref("Проведи review ветки 9760"), "9760")
        self.assertEqual(
            policy.extract_git_ref("Сделай код-ревью merge-коммита ветки 9760"), "9760"
        )
        self.assertEqual(policy.validate_git_ref("9760"), "9760")

    def test_formal_source_read_allowlists_are_unchanged(self) -> None:
        enabled = {
            name
            for name, stage in self.stages["operations"].items()
            if "flow1c_source_read" in stage["allowed_tools"]
        }
        self.assertEqual(enabled, {"development", "technical-implementation", "code-review"})

    def test_operation_profiles_match_legacy_flags(self) -> None:
        for operation, stage in self.stages["operations"].items():
            profile = policy.operation_profile(self.stages, operation)
            capabilities = policy.resolve_capabilities(self.model, profile)
            self.assertEqual(bool(stage.get("requires_rlm")), "rlm" in capabilities)
            self.assertEqual(bool(stage.get("requires_bsl_ls")), "bsl-ls" in capabilities)

    def test_optional_missing_does_not_block_unrequested_profile(self) -> None:
        self.assertTrue(
            policy.capability_readiness(
                ["core"], {"core": "READY", "cc-1c-skills": "OPTIONAL_MISSING"}
            )
        )

    def test_optional_cc_skills_blocks_only_when_full_requests_it(self) -> None:
        states = {
            name: "READY" for name in policy.resolve_capabilities(self.model, "implementation")
        }
        states["cc-1c-skills"] = "OPTIONAL_MISSING"
        self.assertTrue(
            policy.capability_readiness(
                policy.resolve_capabilities(self.model, "implementation"), states
            )
        )
        self.assertFalse(
            policy.capability_readiness(policy.resolve_capabilities(self.model, "full"), states)
        )

    def test_python_supported_range_has_no_upper_bound(self) -> None:
        self.assertFalse(policy.python_version_supported((3, 9), self.model))
        self.assertTrue(policy.python_version_supported((3, 10), self.model))
        self.assertTrue(policy.python_version_supported((3, 12), self.model))
        self.assertTrue(policy.python_version_supported((3, 13), self.model))
        self.assertTrue(policy.python_version_supported((3, 14), self.model))
        self.assertTrue(policy.python_version_supported((3, 15), self.model))
        self.assertTrue(policy.python_version_supported((3, 99), self.model))
        self.assertNotIn("maximum_exclusive", self.model["python"])

    def test_profile_doctor_preserves_legacy_fields(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                svc_readiness, "_capability_checks", return_value=[("check", "OK", "ready")]
            ),
            mock.patch.object(ext_scripts_external_tools, "skills_connected", return_value=False),
            redirect_stdout(output),
        ):
            self.assertEqual(flow1c.cmd_profile_doctor("analysis", json_output=True), 0)
        payload = json.loads(output.getvalue())
        for field in ("schema_version", "state", "ready", "checks"):
            self.assertIn(field, payload)
        self.assertEqual(payload["capabilities"]["cc-1c-skills"]["state"], "OPTIONAL_MISSING")


class ReferencePolicyTests(unittest.TestCase):

    def test_supported_references_are_opaque(self) -> None:
        for value in ("12345", "ERP-742", "G-104", "Проект-15", "ИТСМ-38129"):
            self.assertEqual(policy.validate_reference(value, required=True), value)

    def test_task_wins_and_project_is_inherited(self) -> None:
        self.assertEqual(
            policy.resolve_work_reference("ERP-742", "ERP-2026")["reference_source"], "task"
        )
        inherited = policy.resolve_work_reference(None, "ERP-2026")
        self.assertEqual(
            (inherited["work_reference"], inherited["reference_source"]), ("ERP-2026", "project")
        )

    def test_empty_and_control_characters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            policy.validate_reference("   ", required=True)
        with self.assertRaises(ValueError):
            policy.validate_reference("ERP\n742", required=True)

    def test_path_characters_are_slugged_without_changing_original(self) -> None:
        original = "ERP/742: Интеграция"
        resolved = policy.resolve_work_reference(original, None)
        self.assertEqual(resolved["work_reference"], original)
        self.assertEqual(policy.reference_slug(original), "ERP-742-Интеграция")

    def test_slug_collision_has_stable_suffix_and_remains_resolvable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_root = flow1c.ROOT
            try:
                flow1c.ROOT = Path(directory)
                items = flow1c.ROOT / "work-items"
                direct = items / "ERP-742"
                direct.mkdir(parents=True)
                (direct / "manifest.yaml").write_text(
                    json.dumps({"code": "ERP/742"}), encoding="utf-8"
                )
                second = svc_work_items.work_item_root(
                    "ERP:742", for_create=True, product_root=flow1c.ROOT
                )
                self.assertRegex(second.name, "^ERP-742-[a-f0-9]{8}$")
                second.mkdir()
                (second / "manifest.yaml").write_text(
                    json.dumps({"work_reference": "ERP:742"}), encoding="utf-8"
                )
                self.assertEqual(
                    svc_work_items.work_item_root("ERP:742", product_root=flow1c.ROOT), second
                )
                self.assertEqual(
                    svc_work_items.work_item_root("ERP/742", product_root=flow1c.ROOT), direct
                )
            finally:
                flow1c.ROOT = old_root

    def test_missing_reference_is_requested_not_generated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_root = flow1c.ROOT
            try:
                flow1c.ROOT = Path(directory)
                (flow1c.ROOT / "config").mkdir()
                shutil.copy2(
                    ROOT / "config" / "stages.json", flow1c.ROOT / "config" / "stages.json"
                )
                output = io.StringIO()
                args = argparse.Namespace(
                    operation="functional-spec",
                    mode="formal",
                    code=None,
                    task_reference=None,
                    project_reference=None,
                    g_number=None,
                    summary="spec",
                    path=[],
                    requirements=[],
                    mismatch="",
                    allow_incomplete_draft=False,
                    profile=None,
                )
                with redirect_stdout(output):
                    self.assertEqual(flow1c.cmd_agent_begin(args), 2)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["state"], "BLOCKED")
                self.assertTrue(payload["awaiting_user_input"])
                self.assertEqual(
                    payload["clarification"]["options"],
                    ["provide-reference", "create-provisional", "independent-draft"],
                )
            finally:
                flow1c.ROOT = old_root


class DeviationAndCheckpointTests(unittest.TestCase):

    def test_document_gate_can_continue_with_visible_deviation(self) -> None:
        gate = {
            "operation": "functional-spec",
            "state": "NEEDS_INPUT",
            "errors": [],
            "missing_required": [{"id": "template"}],
            "missing_conditional": [],
        }
        result = policy.accept_deviation(gate, reason="Пользователь отказался предоставлять шаблон")
        self.assertEqual(result["state"], "READY_WITH_DEVIATIONS")
        self.assertEqual(result["compliance"], "DEVIATED")

    def test_hard_mutation_and_publication_gates_are_not_waivable(self) -> None:
        for operation in ("development", "publish"):
            with self.assertRaises(ValueError):
                policy.accept_deviation({"operation": operation}, reason="continue")

    def test_registry_bypass_is_typed_and_waits_for_provisional_creation(self) -> None:
        gate = {
            "operation": "functional-spec",
            "state": "NEEDS_CONFIRMATION",
            "errors": ["registry absent"],
            "missing_required": [],
            "missing_conditional": [],
            "work_item_exists": False,
        }
        result = policy.accept_deviation(
            gate, reason="continue from user brief", deviation_type="registry_bypass"
        )
        self.assertEqual(result["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(result["deviations"][0]["type"], "registry_bypass")

    def test_checkpoint_is_atomic_and_rejects_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_root = flow1c.ROOT
            try:
                flow1c.ROOT = Path(directory)
                checkpoint = svc_setup.create_setup_checkpoint(
                    "analysis", {"documentation_path": "C:/docs"}, product_root=flow1c.ROOT
                )
                self.assertEqual(
                    json.loads(
                        svc_setup.setup_checkpoint_path(
                            checkpoint["setup_id"], product_root=flow1c.ROOT
                        ).read_text(encoding="utf-8")
                    )["state"],
                    "IN_PROGRESS",
                )
                checkpoint["completed_steps"] = ["bootstrap"]
                svc_setup.save_setup_checkpoint(checkpoint, product_root=flow1c.ROOT)
                resumed = svc_setup.create_setup_checkpoint(
                    "analysis",
                    {"documentation_path": "C:/docs"},
                    checkpoint["setup_id"],
                    product_root=flow1c.ROOT,
                )
                self.assertEqual(resumed["completed_steps"], ["bootstrap"])
                with self.assertRaises(WorkflowError):
                    svc_setup.create_setup_checkpoint(
                        "analysis", {"api_token": "secret"}, product_root=flow1c.ROOT
                    )
            finally:
                flow1c.ROOT = old_root


class ClaudeContractTests(unittest.TestCase):

    def test_claude_import_and_wrappers_match_canonical_frontmatter(self) -> None:
        self.assertTrue((ROOT / "CLAUDE.md").read_text(encoding="utf-8").startswith("@AGENTS.md\n"))
        canonical = {
            path.parent.name: path
            for path in (ROOT / ".agents" / "skills").glob("flow1c-*/SKILL.md")
        }
        wrappers = {
            path.parent.name: path
            for path in (ROOT / ".claude" / "skills").glob("flow1c-*/SKILL.md")
        }
        self.assertEqual(set(canonical), set(wrappers))
        for name in canonical:
            canonical_header = (
                canonical[name].read_text(encoding="utf-8").split("---", 2)[1].strip()
            )
            wrapper_header = wrappers[name].read_text(encoding="utf-8").split("---", 2)[1].strip()
            self.assertEqual(canonical_header, wrapper_header)

    def test_shared_contract_and_onboarding_are_complete(self) -> None:
        stages = json.loads((ROOT / "config" / "stages.json").read_text(encoding="utf-8"))
        tool_map = (ROOT / "docs" / "agent-tool-map.md").read_text(encoding="utf-8")
        for stage in stages["operations"].values():
            for tool in stage["allowed_tools"]:
                self.assertIn(f"`{tool}`", tool_map)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for heading in (
            "## OpenCode",
            "## Codex",
            "## Claude Code",
            "## ZIP и offline bootstrap",
            "## Восстановление прерванной установки",
        ):
            self.assertIn(heading, readme)
        json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))

    def test_initialize_repository_flag_is_wired_and_guarded(self) -> None:
        cli = (ROOT / "flow1c/setup.py").read_text(encoding="utf-8")
        configure = (ROOT / "scripts" / "configure-project.ps1").read_text(encoding="utf-8")
        self.assertIn("-InitializeDocumentationRepository", cli)
        self.assertIn(
            "$DocumentationRepositoryUrl -and $InitializeDocumentationRepository", configure
        )
        self.assertIn("$InitializeDocumentationRepository -and -not $Confirmed", configure)


@unittest.skipUnless(os.name == "nt", "PowerShell contracts apply on Windows")
class PowerShellSetupContractTests(unittest.TestCase):

    def test_bootstrap_reports_child_failures_and_bsl_download_is_bounded(self) -> None:
        bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8-sig")
        installer = (ROOT / "scripts" / "install-bsl-language-server.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("BSL Language Server installation failed", bootstrap)
        self.assertIn("RLM startup failed", bootstrap)
        self.assertIn("DownloadTimeoutSeconds", installer)
        self.assertIn("-TimeoutSec $DownloadTimeoutSeconds", installer)
        self.assertIn("Remove-Item -LiteralPath $ArchivePath", installer)
        self.assertIn("Checksum mismatch", installer)

    @classmethod
    def setUpClass(cls) -> None:
        cls.powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not cls.powershell:
            raise unittest.SkipTest("PowerShell is unavailable")

    def run_setup_state(
        self, *, path_value: str | None = None, corrupt_local: bool = False
    ) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "setup-state.ps1", scripts / "setup-state.ps1")
            shutil.copy2(ROOT / ".flow1c.json", root / ".flow1c.json")
            if corrupt_local:
                (root / ".flow1c.local.json").write_text("{broken", encoding="utf-8")
            env = os.environ.copy()
            if path_value is not None:
                env["PATH"] = path_value
            result = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(scripts / "setup-state.ps1"),
                    "-Json",
                    "-Profile",
                    "conversation",
                ],
                text=True,
                encoding="utf-8-sig",
                capture_output=True,
                env=env,
                check=False,
            )
            return json.loads(result.stdout)

    def test_setup_state_returns_json_without_git(self) -> None:
        payload = self.run_setup_state(path_value="")
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("git", payload["prerequisites"])

    def test_setup_state_returns_structured_error_for_invalid_local_config(self) -> None:
        payload = self.run_setup_state(corrupt_local=True)
        self.assertTrue(payload["errors"])
        self.assertEqual(payload["errors"][0]["code"], "CONFIG_JSON_INVALID")

    def test_future_python_is_ready_without_project_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "setup-state.ps1", scripts / "setup-state.ps1")
            shutil.copy2(ROOT / ".flow1c.json", root / ".flow1c.json")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "python.cmd").write_text("@echo 3.99.0\r\n", encoding="ascii")
            venv_python = root / ".venv" / "Scripts" / "python.exe"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_bytes(b"")
            env = os.environ.copy()
            env["PATH"] = str(fake_bin)
            result = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(scripts / "setup-state.ps1"),
                    "-Json",
                    "-Profile",
                    "conversation",
                ],
                text=True,
                encoding="utf-8-sig",
                capture_output=True,
                env=env,
                check=False,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["state"], "READY")
            self.assertTrue(payload["ready"])
            self.assertFalse(payload["confirmation_required"])
            self.assertEqual(payload["required_confirmations"], [])

    def test_conversation_bootstrap_plan_is_json_and_does_not_create_venv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "bootstrap.ps1", scripts / "bootstrap.ps1")
            result = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(scripts / "bootstrap.ps1"),
                    "-Profile",
                    "conversation",
                    "-Plan",
                    "-Json",
                ],
                text=True,
                encoding="utf-8-sig",
                capture_output=True,
                check=False,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["state"], "PLAN")
            self.assertFalse((root / ".venv").exists())
            details = json.dumps(payload, ensure_ascii=False).casefold()
            self.assertNotIn("rlm-tools-bsl", details)
            self.assertNotIn("bsl language server", details)

    def test_bootstrap_plan_preserves_incompatible_venv_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "bootstrap.ps1", scripts / "bootstrap.ps1")
            venv_python = root / ".venv" / "Scripts" / "python.exe"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_bytes(b"")
            result = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(scripts / "bootstrap.ps1"),
                    "-Profile",
                    "conversation",
                    "-Plan",
                    "-Json",
                ],
                text=True,
                encoding="utf-8-sig",
                capture_output=True,
                check=False,
            )
            payload = json.loads(result.stdout)
            details = json.dumps(payload, ensure_ascii=False)
            self.assertIn("preserve incompatible .venv", details)
            self.assertTrue(venv_python.exists())


if __name__ == "__main__":
    unittest.main()
