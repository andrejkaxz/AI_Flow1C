from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from flow1c.documentation import SERVICE_FOLDERS
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates" / "project-documentation"
USER_FOLDERS = (
    "Материалы встреч",
    "Шаблоны документов",
    "Реестр процессов и требований",
    "Документы для анализа",
)


@unittest.skipUnless(os.name == "nt", "Windows PowerShell setup contract")
class ProjectDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.powershell = shutil.which("powershell")
        if not cls.powershell:
            raise unittest.SkipTest("Windows PowerShell is unavailable")

    def setUp(self) -> None:
        self.fixture = tempfile.TemporaryDirectory(prefix="flow1c-docs-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.destination = self.root / "Документация проекта"

    def run_scaffold(
        self, *, source: Path = TEMPLATES, command: str = ""
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            FLOW1C_TEST_HELPER=str(ROOT / "scripts" / "project-documentation.ps1"),
            FLOW1C_TEST_SOURCE=str(source),
            FLOW1C_TEST_DESTINATION=str(self.destination),
            FLOW1C_TEST_OUTSIDE=str(self.root / "outside"),
        )
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
            ". $env:FLOW1C_TEST_HELPER; "
            + (
                command
                or "ConvertTo-Json -Compress -InputObject @(Initialize-Flow1CDocumentation "
                "-DocumentationPath $env:FLOW1C_TEST_DESTINATION "
                "-TemplateRoot $env:FLOW1C_TEST_SOURCE)"
            )
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return subprocess.run(
            [
                self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-EncodedCommand", encoded,
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            timeout=30,
            check=False,
        )

    def test_clean_scaffold_and_resume_preserve_user_documents(self) -> None:
        result = self.run_scaffold(command=(
            "ConvertTo-Json -Compress -InputObject @(Initialize-Flow1CDocumentation "
            "-DocumentationPath $env:FLOW1C_TEST_DESTINATION)"
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {
            str((Path(".flow1c") / path.relative_to(TEMPLATES))
                if path.relative_to(TEMPLATES).parts[0] in SERVICE_FOLDERS
                else path.relative_to(TEMPLATES))
            for path in TEMPLATES.rglob("*.md")
        } | {str(Path(".flow1c/layout.json"))}
        self.assertEqual(set(json.loads(result.stdout)), expected)
        for relative in expected:
            self.assertTrue((self.destination / relative).is_file())
        self.assertEqual(
            {p.name for p in self.destination.iterdir()},
            {*USER_FOLDERS, "README.md", ".flow1c"},
        )
        self.assertIn("(.flow1c/wiki/status.md)", (self.destination / "README.md").read_text(encoding="utf-8"))
        for name in USER_FOLDERS:
            self.assertTrue((self.destination / name / "README.md").is_file())

        user_readme = self.destination / "README.md"
        user_readme.write_text("Инструкция заказчика", encoding="utf-8")
        empty_readme = self.destination / USER_FOLDERS[0] / "README.md"
        empty_readme.write_bytes(b"")
        material = self.destination / USER_FOLDERS[0] / "Встреча.txt"
        material.write_text("Протокол", encoding="utf-8")
        missing = self.destination / USER_FOLDERS[1] / "README.md"
        missing.unlink()
        result = self.run_scaffold()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [str(missing.relative_to(self.destination))])
        self.assertEqual(user_readme.read_text(encoding="utf-8"), "Инструкция заказчика")
        self.assertEqual(empty_readme.read_bytes(), b"")
        self.assertEqual(material.read_text(encoding="utf-8"), "Протокол")
        result = self.run_scaffold()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_conflicts_are_rejected_before_any_file_is_added(self) -> None:
        for occupied_by_directory in (False, True):
            with self.subTest(occupied_by_directory=occupied_by_directory):
                self.destination = self.root / str(occupied_by_directory)
                self.destination.mkdir()
                conflict = self.destination / USER_FOLDERS[0]
                if occupied_by_directory:
                    (conflict / "README.md").mkdir(parents=True)
                else:
                    conflict.write_bytes(b"user file")
                before = {str(path.relative_to(self.destination)) for path in self.destination.rglob("*")}
                result = self.run_scaffold()
                self.assertNotEqual(result.returncode, 0)
                after = {str(path.relative_to(self.destination)) for path in self.destination.rglob("*")}
                self.assertEqual(after, before)

    def test_junctions_in_source_or_destination_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "README.md"
        sentinel.write_bytes(b"external document")
        for in_source in (False, True):
            with self.subTest(in_source=in_source):
                source = self.root / ("source" + str(in_source))
                shutil.copytree(TEMPLATES, source)
                self.destination = self.root / ("destination" + str(in_source))
                self.destination.mkdir()
                junction_parent = source if in_source else self.destination
                junction_name = "linked" if in_source else USER_FOLDERS[0]
                env_key = "FLOW1C_TEST_SOURCE" if in_source else "FLOW1C_TEST_DESTINATION"
                name_literal = junction_name.replace("'", "''")
                result = self.run_scaffold(
                    source=source,
                    command=(
                        f"$null = New-Item -ItemType Junction -Path (Join-Path $env:{env_key} "
                        f"'{name_literal}') -Target $env:FLOW1C_TEST_OUTSIDE; "
                        "Initialize-Flow1CDocumentation "
                        "-DocumentationPath $env:FLOW1C_TEST_DESTINATION "
                        "-TemplateRoot $env:FLOW1C_TEST_SOURCE"
                    ),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symlink or junction", result.stderr)
                self.assertFalse((self.destination / "README.md").exists())
                self.assertEqual(sentinel.read_bytes(), b"external document")

    def test_relative_root_and_overlapping_paths_are_rejected(self) -> None:
        for destination in (Path("relative"), Path(self.root.anchor), TEMPLATES, TEMPLATES / "nested"):
            with self.subTest(destination=destination):
                self.destination = destination
                result = self.run_scaffold()
                self.assertNotEqual(result.returncode, 0)

    def test_loading_helper_does_not_create_documentation(self) -> None:
        result = self.run_scaffold(command="'loaded'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
