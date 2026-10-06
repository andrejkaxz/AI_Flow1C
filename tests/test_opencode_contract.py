from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OpenCodeContractTests(unittest.TestCase):
    def test_controller_is_default_primary_agent(self) -> None:
        config = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))
        self.assertEqual(config["default_agent"], "flow1c-controller")
        agent = (ROOT / ".opencode" / "agents" / "flow1c-controller.md").read_text(encoding="utf-8")
        self.assertIn("mode: primary", agent)
        self.assertIn("steps: 80", agent)
        self.assertIn('"flow1c_*": allow', agent)

    def test_controller_has_guarded_git_builtins_and_denies_other_builtins(self) -> None:
        agent = (ROOT / ".opencode" / "agents" / "flow1c-controller.md").read_text(encoding="utf-8")
        for permission in ("read", "glob", "list", "edit", "task", "todowrite", "lsp", "external_directory"):
            self.assertIn(f"  {permission}: deny", agent)
        self.assertIn("  grep: allow", agent)
        self.assertIn('    "*": deny', agent)
        self.assertIn('    "git log *": allow', agent)
        self.assertNotIn("  bash: allow", agent)

    def test_query_analysis_has_narrow_cc_skill_and_tool_access(self) -> None:
        agent = (ROOT / ".opencode" / "agents" / "flow1c-controller.md").read_text(encoding="utf-8")
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")
        stage = json.loads((ROOT / "config" / "stages.json").read_text(encoding="utf-8"))["operations"]["query-analysis"]

        self.assertIn('    "*": deny', agent)
        self.assertIn("    meta-info: allow", agent)
        self.assertIn("    skd-info: allow", agent)
        self.assertNotIn("meta-edit: allow", agent)
        self.assertIn("export const cc_inspect", adapter)
        self.assertIn("export const query_schema", adapter)
        self.assertIn("export const query_check", adapter)
        self.assertIn('query_intent: tool.schema.enum(["create", "review", "optimize"])', adapter)
        self.assertIn("сразу переходи к `flow1c_query_check` без пробы RLM", agent)
        self.assertIn("flow1c_cc_inspect", stage["allowed_tools"])
        self.assertIn("flow1c_query_schema", stage["allowed_tools"])
        self.assertIn("flow1c_query_check", stage["allowed_tools"])
        self.assertEqual(stage["writable_targets"], [])

    def test_functional_section_tools_use_the_cli_contract(self) -> None:
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")
        tool_map = (ROOT / "docs" / "agent-tool-map.md").read_text(encoding="utf-8")

        self.assertIn('"functional-section"', adapter.split("] as const", 1)[0])
        self.assertIn("export const section = tool({", adapter)
        self.assertIn("export const docx = tool({", adapter)
        self.assertIn('section-catalog", "--json-stdin"', adapter)
        self.assertIn("const payload = args.action === \"save\"", adapter)
        self.assertIn("const payload = args.action === \"inspect\"", adapter)
        self.assertIn("`flow1c_section`", tool_map)
        self.assertIn("`flow1c_docx`", tool_map)

    def test_every_stage_has_a_real_skill_and_tool_allowlist(self) -> None:
        stages = json.loads((ROOT / "config" / "stages.json").read_text(encoding="utf-8"))
        for operation, stage in stages["operations"].items():
            self.assertTrue((ROOT / ".agents" / "skills" / stage["skill"] / "SKILL.md").is_file(), operation)
            self.assertIn("flow1c_complete", stage["allowed_tools"], operation)
            self.assertTrue(all(name.startswith("flow1c_") for name in stage["allowed_tools"]), operation)

    def test_guard_persists_gate_and_non_compliance_state(self) -> None:
        guard = (ROOT / ".opencode" / "plugins" / "flow1c-guard.js").read_text(encoding="utf-8")
        self.assertIn("The supplied FLOW1C gate does not exist", guard)
        self.assertIn("PENDING_CONTINUATION", guard)
        self.assertIn("non-compliant-sessions", guard)
        self.assertIn("experimental.session.compacting", guard)

    def test_custom_tools_do_not_require_bun_globals(self) -> None:
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")
        self.assertNotIn("Bun.", adapter)
        self.assertIn('from "node:child_process"', adapter)

    def test_template_tools_and_runtime_recovery_are_exposed(self) -> None:
        adapter = (ROOT / ".opencode/tools/flow1c.ts").read_text(encoding="utf-8")
        self.assertIn("export const template = tool({", adapter)
        self.assertIn("export const document = tool({", adapter)
        self.assertIn('"template-management", "template-document"', adapter)
        self.assertIn('if (args[0] !== "agent-complete"', adapter)
        self.assertIn('isSavedUpdateContinuation(worktree, stdin)', adapter)
        updater = (ROOT / "scripts/update.ps1").read_text(encoding="utf-8")
        self.assertIn("agent_runtime = $AgentRuntime", updater)
        self.assertIn('"RESTART_REQUIRED"', updater)
        self.assertIn('".opencode", "opencode.json"', updater)
        self.assertIn("restart_required = [bool]$AdapterChanges.Count", updater)

    def test_redmine_tools_expose_read_only_listing_and_explicit_dmsf_selection(self) -> None:
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")
        self.assertIn("export const redmine_files = tool({", adapter)
        self.assertIn('["redmine", "files", args.issue, "--json"]', adapter)
        self.assertIn("export const redmine_relations = tool({", adapter)
        self.assertIn('["redmine", "relations", args.issue, "--json"]', adapter)
        self.assertIn('dms_file_ids: tool.schema.array(tool.schema.string()).optional()', adapter)
        self.assertIn('all_dms: tool.schema.boolean().optional()', adapter)
        self.assertIn('cliArgs.push("--dms-file", fileId)', adapter)

    def test_code_review_exposes_a_separate_read_only_git_ref_tool(self) -> None:
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")
        controller = (ROOT / ".opencode" / "agents" / "flow1c-controller.md").read_text(encoding="utf-8")

        self.assertIn("export const git_inspect", adapter)
        self.assertIn('git_ref: tool.schema.string().optional()', adapter)
        self.assertIn('git_refs: tool.schema.array(tool.schema.string()).max(20).optional()', adapter)
        self.assertIn('target_ref: tool.schema.string().optional()', adapter)
        self.assertIn('"integration"', adapter)
        self.assertIn('"merge-search"', adapter)
        self.assertIn("export const git_refresh", adapter)
        self.assertIn("export const git_snapshot", adapter)
        self.assertIn('mode: tool.schema.enum(["explore", "draft", "formal"]).optional()', adapter)
        self.assertIn("числовая ветка `9760`", controller)
        self.assertIn("action=merge-search", controller)
        self.assertIn("не используй `flow1c_source_query`/`git_search` для истории git", controller.casefold())

    def test_setup_treats_functional_spec_template_as_optional(self) -> None:
        configure = (ROOT / "scripts" / "configure-project.ps1").read_text(encoding="utf-8")
        setup_state = (ROOT / "scripts" / "setup-state.ps1").read_text(encoding="utf-8")
        setup_agent = (ROOT / ".opencode" / "agents" / "flow1c-setup.md").read_text(encoding="utf-8")

        self.assertIn('[string]$FunctionalSpecTemplate = ""', configure)
        self.assertNotIn('[Parameter(Mandatory = $true)][string]$FunctionalSpecTemplate', configure)
        required_block = setup_state.split("$RequiredConfirmations = @(", 1)[1].split(")", 1)[0]
        optional_block = setup_state.split("optional_inputs = @(", 1)[1].split(")", 1)[0]
        self.assertNotIn("functional_spec_template", required_block)
        self.assertIn("functional_spec_template", optional_block)
        self.assertNotIn("reasoningEffort: xhigh", setup_agent)

    def test_update_agent_inherits_selected_model_reasoning_effort(self) -> None:
        update_agent = (ROOT / ".opencode" / "agents" / "flow1c-update.md").read_text(encoding="utf-8")

        self.assertNotIn("reasoningEffort:", update_agent)

    def test_update_command_uses_gated_action_without_shell_runner(self) -> None:
        command = (ROOT / ".opencode" / "commands" / "update.md").read_text(encoding="utf-8")
        agent = (ROOT / ".opencode" / "agents" / "flow1c-update.md").read_text(encoding="utf-8")

        self.assertNotIn("!`", command)
        self.assertIn('operation="update"', command)
        self.assertIn('mode="formal"', command)
        self.assertIn('action="update"', command)
        self.assertIn('parameters_json=\'{"confirmed":true}\'', command)
        self.assertIn('`flow1c_complete`', agent)
        self.assertIn("  bash: deny", agent)
        self.assertNotIn("JSON result injected by `/update`", agent)

    def test_update_can_repair_python_and_forward_per_run_external_consent(self) -> None:
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")
        cli = (ROOT / "scripts" / "flow1c.py").read_text(encoding="utf-8")
        updater = (ROOT / "scripts" / "update.ps1").read_text(encoding="utf-8")

        self.assertIn('gate.operation === "update"', adapter)
        self.assertIn('updateArgs.push("-AllowExternalUpdates")', adapter)
        self.assertIn('updateArgs.push("-RepairPrerequisites")', adapter)
        self.assertIn('command.append("-AllowExternalUpdates")', cli)
        self.assertIn('command.append("-RepairPrerequisites")', cli)
        self.assertIn('[switch]$RepairPrerequisites', updater)
        self.assertIn('Emit-Result "NEEDS_CONFIRMATION"', updater)
        self.assertRegex(updater, r"Stop-OwnedRlm\s+\$BootstrapOutput")
        self.assertIn('start-rlm-tools-bsl.ps1") *>&1', updater)

    def test_update_refreshes_git_extension_and_uses_incremental_rlm_index(self) -> None:
        updater = (ROOT / "scripts" / "update.ps1").read_text(encoding="utf-8")
        indexer = (ROOT / "scripts" / "rlm-index.ps1").read_text(encoding="utf-8")

        self.assertIn('if ([string]$LocalConfig.extension_mode -ne "git")', updater)
        self.assertIn('"status", "--porcelain", "--untracked-files=no"', updater)
        self.assertIn('"merge", "--ff-only", $Upstream', updater)
        self.assertLess(updater.index('$ExtensionUpdate = Update-Extension $LocalConfig'), updater.index('$Indexes = Update-Indexes $LocalConfig'))
        self.assertIn('-ForceUpdate:$ForceUpdate', updater)
        self.assertIn('rlm_index_runtime.py', indexer)

    def test_opencode_accepts_current_and_future_python_versions(self) -> None:
        adapter = (ROOT / ".opencode" / "tools" / "flow1c.ts").read_text(encoding="utf-8")

        self.assertIn('["py", "-3"], ["py", "-3.14"], ["py", "-3.13"]', adapter)
        self.assertIn("sys.version_info[:2] >= (3,10)", adapter)
        self.assertNotIn("sys.version_info[:2] <", adapter)


if __name__ == "__main__":
    unittest.main()
