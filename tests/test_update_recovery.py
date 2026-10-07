"""Regressions for the real Windows/OpenCode update recovery scenario."""

from __future__ import annotations

import base64
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

from jsonschema import Draft202012Validator

from flow1c import cli, setup, storage, update_diagnostics
from flow1c.errors import WorkflowError
from flow1c.update_policy import migrate_update_scope
from flow1c.workflow import state
from test_cli_contract import CliScenario

ROOT = Path(__file__).resolve().parents[1]
UPDATE_ID = "33333333-3333-4333-8333-333333333333"


class UpdateRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="flow1c-update-recovery-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = CliScenario(self.base).root
        self.extension = self.base / "extension"
        self.extension.mkdir()
        (self.extension / ".git").mkdir()
        self.local = {
            "schema_version": 2, "project_reference": "G", "extension_mode": "git",
            "extension_path": str(self.extension),
            "extension_repository_url": "https://user:synthetic-password@git.example.org/team/extension.git?token=synthetic-query",
            "credentials": {"api_key": "synthetic-private-key"},
        }
        storage.write_json(self.root / ".flow1c.local.json", self.local)

    def call(self, command: str, request: dict) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch.object(cli, "ROOT", self.root), \
             mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(output):
            code = cli.main([command, "--json-stdin"])
        return code, json.loads(output.getvalue())

    def git_read(self, command, **kwargs):
        values = {
            ("symbolic-ref", "--quiet", "--short", "HEAD"): "main",
            ("config", "--get", "branch.main.remote"): "origin",
            ("remote", "get-url", "--", "origin"): "git@git.example.org:team/extension.git",
        }
        self.assertEqual(command[:2], ["git", "--no-optional-locks"])
        self.assertEqual(command[2:4], ["-C", str(self.extension)])
        key = tuple(command[4:])
        self.assertIn(key, values, "only bounded Git metadata reads are allowed")
        return subprocess.CompletedProcess(command, 0, values[key], "")

    def old_gate(self, operation: str = "update") -> dict:
        return state.new_gate(
            operation, "G", "BLOCKED", product_root=self.root,
            project_reference="G", work_reference="G", work_item_exists=False,
            errors=["Work item not found: G"], update_id=UPDATE_ID,
            update_result={"state": "BLOCKED", "phase": "workflow", "options": {}},
        )

    def test_begin_update_never_reads_work_items_or_artifacts(self) -> None:
        with mock.patch("flow1c.work_items.load_manifest", side_effect=AssertionError("no work item")), \
             mock.patch("flow1c.intake.load_artifact_index", side_effect=AssertionError("no artifacts")):
            code, gate = self.call("agent-begin", {"operation": "update", "mode": "formal", "summary": "Обновить Flow1C"})
        self.assertEqual((code, gate["state"]), (0, "READY"))
        self.assertEqual(gate["project_reference"], "G")
        self.assertIsNone(gate["code"])
        self.assertIsNone(gate["work_reference"])
        self.assertEqual(gate["conditions"], [])
        self.assertFalse(gate.get("action_completed"))

    def test_blocked_diagnostics_hide_secrets_and_do_not_change_state(self) -> None:
        gate = self.old_gate()
        before = (self.root / ".flow1c.local.json").read_bytes()
        with mock.patch.object(update_diagnostics.subprocess, "run", side_effect=self.git_read):
            code, result = self.call("agent-action", {"gate_id": gate["gate_id"], "action": "update-diagnose"})
        self.assertEqual((code, result["state"], result["ready"]), (0, "DIAGNOSTIC", False))
        self.assertEqual(result["extension_repository"]["state"], "MATCH")
        self.assertEqual(result["update_id"], UPDATE_ID)
        schema = storage.read_json(ROOT / "schemas/update-diagnostic.schema.json")
        Draft202012Validator(schema).validate(result)
        saved = storage.read_json(state.gate_path(gate["gate_id"], product_root=self.root))
        self.assertEqual(saved["state"], "BLOCKED")
        self.assertFalse(saved.get("action_completed"))
        self.assertEqual(saved["update_scope_migration"]["previous_work_item_context"]["code"], "G")
        self.assertEqual((self.root / ".flow1c.local.json").read_bytes(), before)
        text = json.dumps(result)
        for secret in ("synthetic-password", "synthetic-query", "synthetic-private-key"):
            self.assertNotIn(secret, text)

    def test_diagnostics_cannot_supply_paths_mutate_or_escape_other_operations(self) -> None:
        for operation, parameters in (("update", {"path": "arbitrary"}), ("setup", {})):
            gate = self.old_gate(operation)
            with mock.patch.object(update_diagnostics.subprocess, "run", side_effect=AssertionError("no Git")):
                code, result = self.call("agent-action", {"gate_id": gate["gate_id"], "action": "update-diagnose", "parameters_json": json.dumps(parameters)})
            self.assertEqual(code, 2)
            self.assertEqual(result["state"], "BLOCKED")
        gate = self.old_gate()
        self.assertEqual(self.call("agent-action", {"gate_id": gate["gate_id"], "action": "setup-configure", "parameters_json": '{"confirmed":true}'})[0], 2)

    def test_failed_git_never_leaks_stderr_credentials(self) -> None:
        with mock.patch.object(update_diagnostics.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "synthetic-password")):
            result = update_diagnostics.diagnose_update(self.old_gate(), product_root=self.root)
        self.assertNotIn("synthetic-password", json.dumps(result))
        self.assertEqual(result["extension_repository"]["state"], "DETACHED_OR_UNAVAILABLE")

    def test_matching_identity_only_recommends_retry_for_url_comparison_blocker(self) -> None:
        gate = self.old_gate()
        for error, retry in (("Extension remote URL does not match extension_repository_url.", True),
                             ("Doctor readiness check failed.", False)):
            with self.subTest(error=error), mock.patch.object(update_diagnostics.subprocess, "run", side_effect=self.git_read):
                gate["update_result"]["error"] = error
                result = update_diagnostics.diagnose_update(gate, product_root=self.root)
            self.assertEqual("Resume update on the same gate" in result["next_action"], retry)

    def test_real_git_diagnostics_read_only_the_selected_tracking_remote(self) -> None:
        # A real local Git metadata boundary; no remote connection is performed.
        for args in (("init", "-b", "main"), ("remote", "add", "origin", "git@git.example.org:team/extension.git"), ("config", "branch.main.remote", "origin")):
            subprocess.run(["git", "-C", str(self.extension), *args], capture_output=True, check=True)
        before = {str(p.relative_to(self.extension)): p.read_bytes() for p in self.extension.rglob("*") if p.is_file()}
        result = update_diagnostics.diagnose_update(self.old_gate(), product_root=self.root)
        self.assertEqual(result["extension_repository"]["state"], "MATCH")
        after = {str(p.relative_to(self.extension)): p.read_bytes() for p in self.extension.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_malformed_url_authentication_is_never_shown(self) -> None:
        for url in ("https://user:synthetic-password@", "https://[invalid", "ssh://user:synthetic-password@host:invalid/path"):
            self.assertEqual(update_diagnostics.safe_remote_url(url), "")

    def test_config_and_extension_junctions_are_rejected(self) -> None:
        for target in (self.root / ".flow1c.local.json", self.extension):
            with self.subTest(target=target), \
                 mock.patch.object(storage, "is_reparse_or_symlink", side_effect=lambda path: path == target), \
                 mock.patch.object(update_diagnostics.subprocess, "run", side_effect=AssertionError("no Git")):
                with self.assertRaises(WorkflowError):
                    update_diagnostics.diagnose_update(self.old_gate(), product_root=self.root)

    def test_scope_migration_is_idempotent_and_leaves_completed_and_other_gates(self) -> None:
        old = {"operation": "update", "code": "G", "work_reference": "G", "evidence_path": "historical-evidence"}
        migrated = migrate_update_scope(old)
        self.assertEqual(old["code"], "G")
        self.assertIs(migrate_update_scope(migrated), migrated)
        self.assertNotIn("evidence_path", migrated)
        self.assertEqual(migrated["update_scope_migration"]["schema_version"], 1)
        for fields in ({"operation": "functional-spec"}, {"completed_at": "historical"}):
            gate = {**old, **fields}
            self.assertIs(migrate_update_scope(gate), gate)

    def test_old_gate_recovers_and_completes_without_work_item_on_same_id(self) -> None:
        gate = self.old_gate()
        for updater_state in ("BLOCKED", "WAITING_BACKGROUND", "READY"):
            payload = {"schema_version": 2, "state": updater_state, "ready": updater_state == "READY", "update_id": UPDATE_ID, "phase": "workflow", "error": "URL mismatch" if updater_state == "BLOCKED" else "", "options": {}}
            with mock.patch("flow1c.system.command_path", return_value="powershell"), \
                 mock.patch.object(setup, "run_update_command", return_value=subprocess.CompletedProcess([], 0 if updater_state != "BLOCKED" else 1, json.dumps(payload), "")), \
                 mock.patch("flow1c.work_items.load_manifest", side_effect=AssertionError("no work item")):
                self.call("agent-action", {"gate_id": gate["gate_id"], "action": "update", "parameters_json": '{"confirmed":true}'})
            saved = state.load_gate(gate["gate_id"], product_root=self.root)
            self.assertEqual(saved["state"], updater_state)
            self.assertEqual(saved["update_id"], UPDATE_ID)
            self.assertIsNone(saved["code"])
            self.assertEqual(bool(saved.get("action_completed")), updater_state == "READY")
            if updater_state != "READY":
                self.assertEqual(self.call("agent-complete", {"gate_id": gate["gate_id"]})[0], 2)
        with mock.patch("flow1c.work_items.load_manifest", side_effect=AssertionError("no work item")):
            code, completed = self.call("agent-complete", {"gate_id": gate["gate_id"]})
        self.assertEqual((code, completed["state"]), (0, "COMPLETE"))


@unittest.skipUnless(os.name == "nt", "Windows PowerShell updater boundary")
class ExtensionUpdaterTests(unittest.TestCase):
    def run_extension(self, *, expected: str, actual: str, dirty: bool = False) -> dict:
        powershell = shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "FLOW1C_TEST_ROOT": str(ROOT), "FLOW1C_TEST_EXTENSION": directory,
                   "FLOW1C_TEST_EXPECTED": expected, "FLOW1C_TEST_ACTUAL": actual,
                   "FLOW1C_TEST_DIRTY": "yes" if dirty else ""}
            # Run the actual production function; only the Git boundary is synthetic.
            program = """
$ErrorActionPreference = 'Stop'
$PSScriptRoot = Join-Path $env:FLOW1C_TEST_ROOT 'scripts'
$Tokens = $null; $Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'update.ps1'), [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw 'Updater parse errors' }
$Function = $Ast.Find({param($Node) $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq 'Update-Extension'}, $true)
$FunctionPath = Join-Path $env:FLOW1C_TEST_EXTENSION 'update-extension.ps1'
[IO.File]::WriteAllText($FunctionPath, $Function.Extent.Text)
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'git-identity.ps1') -Destination $env:FLOW1C_TEST_EXTENSION
. $FunctionPath
$Calls = [Collections.Generic.List[string]]::new()
function Invoke-Git([string[]]$Arguments, [string]$Repository) {
    $Key = $Arguments -join ' '; $Calls.Add($Key)
    switch ($Key) {
        'rev-parse --show-toplevel' { return $env:FLOW1C_TEST_EXTENSION }
        'status --porcelain --untracked-files=no' { if ($env:FLOW1C_TEST_DIRTY) { return 'M user.bsl' }; return '' }
        'symbolic-ref --quiet --short HEAD' { return 'main' }
        'config --get branch.main.remote' { return 'origin' }
        'remote get-url origin' { return $env:FLOW1C_TEST_ACTUAL }
        'rev-parse --abbrev-ref --symbolic-full-name @{upstream}' { return 'origin/main' }
        'rev-parse HEAD' { return 'fixture-commit' }
        'fetch --prune origin' { return '' }
        'rev-list --left-right --count HEAD...origin/main' { return '0 0' }
        default { throw "Unexpected Git call: $Key" }
    }
}
try {
    $Result = Update-Extension ([pscustomobject]@{extension_mode='git'; extension_path=$env:FLOW1C_TEST_EXTENSION; extension_repository_url=$env:FLOW1C_TEST_EXPECTED})
    @{result=$Result; calls=@($Calls)} | ConvertTo-Json -Depth 6 -Compress
} catch { @{error=$_.Exception.Message; calls=@($Calls)} | ConvertTo-Json -Depth 6 -Compress }
"""
            encoded = base64.b64encode(program.encode("utf-16-le")).decode("ascii")
            process = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], env=env, capture_output=True, text=True, encoding="utf-8-sig", timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)

    def test_same_repository_accepts_https_ssh_and_git_suffix(self) -> None:
        for actual in ("git@git.example.org:team/extension.git", "ssh://git@git.example.org/team/extension", "https://git.example.org/team/extension/"):
            with self.subTest(actual=actual):
                result = self.run_extension(expected="https://git.example.org/team/extension.git", actual=actual)
                self.assertNotIn("error", result)
                self.assertIn("fetch --prune origin", result["calls"])

    def test_other_repository_and_local_changes_block_before_fetch(self) -> None:
        for actual, dirty in (("git@other.example.org:team/extension.git", False), ("git@git.example.org:team/other.git", False), ("git@git.example.org:team/extension.git", True)):
            with self.subTest(actual=actual, dirty=dirty):
                result = self.run_extension(expected="https://git.example.org/team/extension.git", actual=actual, dirty=dirty)
                self.assertIn("error", result)
                self.assertIn("tracked local changes" if dirty else "remote URL does not match", result["error"])
                self.assertNotIn("fetch --prune origin", result["calls"])


if __name__ == "__main__":
    unittest.main()
