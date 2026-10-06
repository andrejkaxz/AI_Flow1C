from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "external_tools.py"
SPEC = importlib.util.spec_from_file_location("external_tools", MODULE_PATH)
assert SPEC and SPEC.loader
external_tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(external_tools)


def manifest() -> dict:
    return {
        "schema_version": 1,
        "policy": {"check_updates": True, "apply_updates": False, "allow_network": True, "rollback_on_failure": True},
        "rlm_tools_bsl": {"package": "rlm-tools-bsl", "requirement": ">=1.34,<2", "index_package": "rlm-bsl-index"},
        "cc_1c_skills": {
            "repository": "https://github.com/Nikolay-Shirokov/cc-1c-skills.git",
            "remote": "origin", "ref": "main", "path": ".tools/cc-1c-skills",
            "switch_command": ["scripts/switch.py", "agents"],
        },
        "bsl_language_server": {
            "platform": "windows", "version": "1.0.7",
            "url": "https://github.com/1c-syntax/bsl-language-server/releases/download/v1.0.7/bsl-language-server_win.zip",
            "sha256": "0" * 64, "executable": "bsl-language-server/bsl-language-server.exe",
        },
    }


class ManifestTests(unittest.TestCase):
    def test_manifest_is_valid(self) -> None:
        self.assertEqual(external_tools.validate_manifest(manifest())["schema_version"], 1)

    def test_unknown_schema_is_rejected(self) -> None:
        value = manifest()
        value["schema_version"] = 2
        with self.assertRaises(external_tools.ExternalToolsError):
            external_tools.validate_manifest(value)

    def test_untrusted_download_source_is_rejected(self) -> None:
        value = manifest()
        value["bsl_language_server"]["url"] = "https://example.invalid/tool.zip"
        with self.assertRaises(external_tools.ExternalToolsError):
            external_tools.validate_manifest(value)


class VersionTests(unittest.TestCase):
    def test_compatible_rlm_version_is_recognized(self) -> None:
        self.assertTrue(external_tools.version_satisfies("1.35.2", ">=1.34,<2"))
        self.assertFalse(external_tools.version_satisfies("2.0.0", ">=1.34,<2"))

    def test_latest_compatible_package_candidate_is_selected(self) -> None:
        completed = mock.Mock(returncode=0, stdout="Available versions: 2.0.0, 1.36.0, 1.35.0\n", stderr="")
        with mock.patch.object(external_tools, "run", return_value=completed):
            candidate, error = external_tools.available_package_version(Path("python.exe"), "rlm-tools-bsl", ">=1.34,<2")
        self.assertEqual(candidate, "1.36.0")
        self.assertEqual(error, "")


class BslInstallTests(unittest.TestCase):
    def test_bad_checksum_keeps_previous_install_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / ".tools/bsl-language-server-v1.0.6/bsl-language-server/bsl-language-server.exe"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            local = {"bsl_language_server": {"version": "1.0.6", "command": str(old)}}
            archive = io.BytesIO(b"not the expected archive")
            with mock.patch("urllib.request.urlopen", return_value=archive):
                with self.assertRaises(external_tools.ExternalToolsError):
                    external_tools.install_bsl(manifest(), local, root=root)
            self.assertTrue(old.is_file())
            self.assertEqual(local["bsl_language_server"]["version"], "1.0.6")

    def test_verified_archive_is_staged_and_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as package:
                package.writestr("bsl-language-server/bsl-language-server.exe", b"binary")
            payload = data.getvalue()
            value = manifest()
            value["bsl_language_server"]["sha256"] = hashlib.sha256(payload).hexdigest()
            local: dict = {}
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(payload)), mock.patch.object(
                external_tools, "bsl_smoke", return_value=(True, "1.0.7")
            ):
                external_tools.install_bsl(value, local, root=root)
            command = Path(local["bsl_language_server"]["command"])
            self.assertTrue(command.is_file())
            self.assertEqual(local["bsl_language_server"]["version"], "1.0.7")


class ReadOnlyCheckTests(unittest.TestCase):
    def test_offline_check_does_not_change_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / ".venv/Scripts"
            scripts.mkdir(parents=True)
            (scripts / "rlm-tools-bsl.exe").write_text("", encoding="utf-8")
            (scripts / "rlm-bsl-index.exe").write_text("", encoding="utf-8")
            local_path = root / ".flow1c.local.json"
            local_path.write_text("{}\n", encoding="utf-8")
            before = {item.relative_to(root): item.read_bytes() for item in root.rglob("*") if item.is_file()}
            with mock.patch.object(external_tools, "package_version", return_value="1.35.0"):
                result = external_tools.check_tools(manifest(), root=root, network=False)
            after = {item.relative_to(root): item.read_bytes() for item in root.rglob("*") if item.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(result["rlm_tools_bsl"]["state"], "CURRENT")

    def test_recoverable_missing_tools_make_update_available(self) -> None:
        tools = {
            "rlm_tools_bsl": {"state": "MISSING", "recoverable": True},
            "cc_1c_skills": {"state": "CURRENT"},
            "bsl_language_server": {"state": "CURRENT"},
        }
        self.assertEqual(external_tools.overall_state(tools), "UPDATE_AVAILABLE")

    def test_dirty_skills_checkout_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / ".tools/cc-1c-skills"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
            (repository / "tracked.txt").write_text("before", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-m", "initial"], check=True, capture_output=True)
            (repository / "tracked.txt").write_text("dirty", encoding="utf-8")
            scripts = root / ".venv/Scripts"
            scripts.mkdir(parents=True)
            (scripts / "rlm-tools-bsl.exe").write_text("", encoding="utf-8")
            (scripts / "rlm-bsl-index.exe").write_text("", encoding="utf-8")
            bsl = root / ".tools/bsl-language-server-v1.0.7/bsl-language-server/bsl-language-server.exe"
            bsl.parent.mkdir(parents=True)
            bsl.write_text("", encoding="utf-8")
            (root / ".flow1c.local.json").write_text(
                json.dumps({"bsl_language_server": {"version": "1.0.7", "command": str(bsl)}}), encoding="utf-8"
            )
            with mock.patch.object(external_tools, "package_version", return_value="1.35.0"), mock.patch.object(
                external_tools, "bsl_smoke", return_value=(True, "1.0.7")
            ):
                result = external_tools.check_tools(manifest(), root=root, network=False)
            self.assertEqual(result["cc_1c_skills"]["state"], "BLOCKED")


class SkillsConnectionTests(unittest.TestCase):
    @staticmethod
    def prepare_checkout(root: Path, switch_body: str) -> Path:
        source = root / ".tools/cc-1c-skills/.claude/skills/cf-info"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("external", encoding="utf-8")
        switch = root / ".tools/cc-1c-skills/scripts/switch.py"
        switch.parent.mkdir(parents=True)
        switch.write_text(switch_body, encoding="utf-8")
        target = root / ".agents/skills"
        for name in external_tools.REQUIRED_WORKFLOW_SKILLS:
            skill = target / name
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(name, encoding="utf-8")
        (target / ".gitignore").write_text("tracked", encoding="utf-8")
        return target

    def test_connection_preserves_project_workflow_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.prepare_checkout(
                root,
                "import pathlib, shutil, sys\n"
                "root = pathlib.Path(sys.argv[sys.argv.index('--project-dir') + 1])\n"
                "target = root / '.agents/skills'\n"
                "shutil.rmtree(target, ignore_errors=True)\n"
                "skill = target / 'cf-info'\n"
                "skill.mkdir(parents=True)\n"
                "(skill / 'SKILL.md').write_text('external', encoding='utf-8')\n",
            )

            external_tools.connect_skills(manifest(), root=root, python=Path(sys.executable))

            self.assertEqual((target / "cf-info/SKILL.md").read_text(encoding="utf-8"), "external")
            self.assertEqual((target / ".gitignore").read_text(encoding="utf-8"), "tracked")
            for name in external_tools.REQUIRED_WORKFLOW_SKILLS:
                self.assertTrue((target / name / "SKILL.md").is_file())

    def test_failed_connection_restores_all_existing_skills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = self.prepare_checkout(
                root,
                "import pathlib, shutil, sys\n"
                "root = pathlib.Path(sys.argv[sys.argv.index('--project-dir') + 1])\n"
                "shutil.rmtree(root / '.agents/skills', ignore_errors=True)\n"
                "raise SystemExit(7)\n",
            )
            custom = target / "custom-skill/SKILL.md"
            custom.parent.mkdir()
            custom.write_text("custom", encoding="utf-8")

            with self.assertRaises(external_tools.ExternalToolsError):
                external_tools.connect_skills(manifest(), root=root, python=Path(sys.executable))

            self.assertEqual(custom.read_text(encoding="utf-8"), "custom")
            for name in external_tools.REQUIRED_WORKFLOW_SKILLS:
                self.assertTrue((target / name / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
