"""Both layouts, actual operations, safe sources and repository publication paths."""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from flow1c import cli, context, context_manifest, documentation, documents, handoff
from flow1c import intake, publication, registry, storage, work_items
from flow1c.errors import WorkflowError
from flow1c.workflow import state
from scripts.flow1c_templates import TemplateService
from scripts.flow1c_templates_store import LibraryStore
from test_cli_contract import CliScenario
from test_flow1c import make_registry

ROOT = Path(__file__).resolve().parents[1]


class DocumentationLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = tempfile.TemporaryDirectory(prefix="flow1c-layout-")
        self.addCleanup(fixture.cleanup)
        self.base = Path(fixture.name).resolve()
        self.scenario = CliScenario(self.base)
        self.root = self.scenario.root
        for name in ("templates", "standards"):
            shutil.copytree(ROOT / name, self.root / name)
        shutil.copy2(ROOT / ".flow1c.json", self.root / ".flow1c.json")
        self.docs = self.base / "documentation"
        documentation.initialize(self.docs, self.root / "templates/project-documentation")
        self.local = {"schema_version": 2, "documentation_path": str(self.docs), "user": "retained"}
        storage.write_json(self.root / ".flow1c.local.json", self.local)

    def call(self, command: str, request: dict) -> dict:
        output = io.StringIO()
        with mock.patch.object(cli, "ROOT", self.root), \
                mock.patch.object(cli.sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(output):
            arguments = [command, request["action"]] if command == "template" else [command]
            code = cli.main([*arguments, "--json-stdin"])
        value = json.loads(output.getvalue())
        self.assertTrue(code == 0 or code == 1 and value.get("state") == "WAITING_USER", value)
        return value

    def start_item(self) -> Path:
        source = self.docs / "Реестр процессов и требований/requirements.xlsx"
        make_registry(source)
        result = registry.registry_import(argparse.Namespace(file=str(source), allow_errors=False), product_root=self.root)
        self.assertEqual(result.exit_code, 0)
        work_items.fs_start(argparse.Namespace(code="G-001", title=None, requirements="", create_branch=False), product_root=self.root)
        return work_items.work_item_root("G-001", product_root=self.root)

    def test_legacy_nonempty_and_configured_empty_projects_are_preserved(self) -> None:
        for name, populated in (("legacy", True), ("configured-empty", False)):
            with self.subTest(name=name):
                docs = self.base / name
                docs.mkdir()
                if populated:
                    storage.write_json(docs / "drafts/request/evidence.json", {"approval": "retained"})
                documentation.initialize(docs, self.root / "templates/project-documentation", preserve_legacy=not populated)
                self.assertEqual(documentation.data_root(docs), docs)
                self.assertFalse((docs / ".flow1c/layout.json").exists())
                self.assertTrue((docs / "registry/README.md").is_file())
                if populated:
                    self.assertEqual(storage.read_json(docs / "drafts/request/evidence.json"), {"approval": "retained"})

    def test_marker_is_explicit_unknown_versions_and_path_conflicts_fail(self) -> None:
        docs = self.base / "arbitrary-service-folder"
        (docs / ".flow1c").mkdir(parents=True)
        self.assertEqual(documentation.data_root(docs), docs)
        for value in ({"schema_version": 1, "layout_version": 3}, {"schema_version": True, "layout_version": 2}):
            storage.write_json(docs / ".flow1c/layout.json", value)
            before = (docs / ".flow1c/layout.json").read_bytes()
            with self.assertRaises(WorkflowError):
                documentation.initialize(docs, self.root / "templates/project-documentation")
            self.assertEqual((docs / ".flow1c/layout.json").read_bytes(), before)
            self.assertFalse((docs / "README.md").exists())
        (self.docs / ".flow1c/requests").write_text("user file", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            documentation.regular_path(self.docs / ".flow1c/requests/result.md")

    def test_registry_item_intake_promotion_and_formal_context_keep_repository_references(self) -> None:
        item = self.start_item()
        self.assertEqual(item, self.docs / ".flow1c/work-items/G-001")
        self.assertFalse((self.docs / "registry").exists())
        source = self.docs / "Материалы встреч/meeting.txt"
        source.write_text("Synthetic meeting decisions", encoding="utf-8")
        accepted = intake.persist_intake_files([(source, Path(source.name))], [], code=None,
            category="meeting_materials", received_via="user-path", product_root=self.root)
        self.assertTrue(accepted["copied"][0]["relative_path"].startswith(".flow1c/inbox/"))
        gate = state.new_gate("functional-spec", "G-001", "READY", mode="formal", product_root=self.root)
        state.save_gate(gate, product_root=self.root)
        result = intake.artifact_intake(argparse.Namespace(gate_id=gate["gate_id"], category="meeting_materials",
            code="G-001", promote_intake_id=accepted["intake_id"]), product_root=self.root)
        self.assertEqual(result.exit_code, 0)
        artifacts = intake.load_artifact_index("G-001", product_root=self.root)["artifacts"]
        self.assertTrue(artifacts[0]["relative_path"].startswith(".flow1c/work-items/G-001/"))
        self.assertEqual((self.docs / artifacts[0]["relative_path"]).read_bytes(), source.read_bytes())
        _, manifest = work_items.load_manifest("G-001", product_root=self.root)
        specs = context_manifest._specs(item, gate, manifest, {}, product_root=self.root)
        self.assertTrue(specs)
        accepted_specs = [spec for spec in specs if spec["reason"] == "accepted artifact"]
        self.assertTrue(accepted_specs)
        self.assertTrue(all(spec["path"].startswith("input/") for spec in accepted_specs))
        self.assertTrue(all((item / spec["path"]).is_file() for spec in accepted_specs))
        self.assertEqual(documents._resolve_section_source(str(source), product_root=self.root), source)
        self.assertEqual(documents._sections_root("G-001", product_root=self.root), self.docs / ".flow1c")
        self.assertEqual(documents._plan_directory(self.docs / ".flow1c"), self.docs / ".flow1c/.workspace/docx-plans")

    def test_draft_context_handoff_restart_and_write_containment(self) -> None:
        gate = self.call("agent-begin", {"operation": "consultation", "mode": "draft", "summary": "Synthetic goal"})
        request = state.request_root(gate, product_root=self.root)
        self.assertEqual(request.parent, self.docs / ".flow1c/drafts")
        self.call("agent-write", {"gate_id": gate["gate_id"], "target": "draft", "path": "result.md", "content": "Synthetic result"})
        source = request / "input/source.md"
        storage.write_text(source, "# Source\n\nSynthetic facts")
        path, evidence = state.evidence_for_gate(gate, product_root=self.root)
        evidence["artifacts"] = [{"path": "input/source.md", "sha256": storage.sha256(source)}]
        storage.write_json(path, evidence)
        context_manifest.build(gate, product_root=self.root)
        with self.assertRaises(WorkflowError):
            state.safe_request_file(request, "../../../../Документы для анализа/result.md")
        finished = self.call("agent-complete", {"gate_id": gate["gate_id"], "summary": "Synthetic conclusion"})
        self.assertEqual(finished["state"], "DRAFT_COMPLETE")
        saved = state.load_gate(gate["gate_id"], product_root=self.root)
        self.assertEqual(state.request_root(saved, product_root=self.root), request)
        self.assertEqual(self.call("agent-handoff", {"gate_id": gate["gate_id"]})["gate_id"], gate["gate_id"])
        self.assertTrue((request / "handoffs").is_dir())
        before = (request / "result.md").read_bytes()
        documentation.initialize(self.docs, self.root / "templates/project-documentation", preserve_legacy=True)
        self.assertEqual((request / "result.md").read_bytes(), before)
        self.assertEqual(context.project_root(self.local, product_root=self.root), self.docs)

    def test_template_only_configure_selects_new_layout_and_preserves_old(self) -> None:
        fresh = self.base / "template-only"
        fresh.mkdir()
        (self.root / ".flow1c.local.json").unlink()
        gate = self.call("agent-begin", {"operation": "template-management", "mode": "explore", "summary": "Templates"})
        self.call("template", {"gate_id": gate["gate_id"], "action": "configure", "request": {"documentation_path": str(fresh)}})
        configured = storage.read_json(self.root / ".flow1c.local.json")
        service = TemplateService(self.root, configured)
        self.assertEqual(service.documentation, fresh)
        self.assertEqual(service.store.root, fresh / ".flow1c/document-templates")
        index = service.store.index(create=True)
        before = (service.store.root / "library.json").read_bytes()
        self.call("template", {"gate_id": gate["gate_id"], "action": "configure", "request": {"documentation_path": str(fresh)}})
        self.assertEqual((service.store.root / "library.json").read_bytes(), before)
        destination = self.base / "other-new-project"
        documentation.initialize(destination, self.root / "templates/project-documentation")
        relocated = service.store.relocate(destination, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(relocated["library_id"], index["library_id"])
        self.assertEqual(LibraryStore(destination).index(), index)
        self.assertEqual((service.store.root / "library.json").read_bytes(), before)

    def test_git_commit_uses_repository_paths_and_rejects_unrelated_staged_files(self) -> None:
        self.start_item()
        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(self.docs), *args], capture_output=True,
                text=True, encoding="utf-8", check=True,
            )
        git("init", "-b", "main")
        git("config", "user.name", "Layout fixture")
        git("config", "user.email", "layout@example.invalid")
        git("add", ".")
        git("commit", "-m", "fixture")
        publication.status(argparse.Namespace(write=True), product_root=self.root)
        storage.write_text(self.docs / ".flow1c/work-items/G-001/analysis/notes.md", "Changed notes")
        source = self.docs / "Документы для анализа/unrelated.md"
        source.write_text("User original", encoding="utf-8")
        args = argparse.Namespace(code="G-001", include_registry=False, message="Fixture notes")
        publication.git_commit(args, product_root=self.root)
        changed = git("show", "--pretty=", "--name-only", "HEAD").stdout
        self.assertIn(".flow1c/work-items/G-001/analysis/notes.md", changed)
        self.assertNotIn("unrelated.md", changed)
        git("add", ".")
        with self.assertRaisesRegex(WorkflowError, "Unexpected staged paths"):
            publication.git_commit(args, product_root=self.root)

    @unittest.skipUnless(os.name == "nt", "Windows project configuration")
    def test_real_configure_in_external_clone_and_repeat_preserve_v2(self) -> None:
        def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result
        # A separate seed and clone prevent inheritance of author-workspace instructions.
        (self.root / ".flow1c.local.json").unlink()
        shutil.copytree(ROOT / ".opencode", self.root / ".opencode")
        shutil.copy2(ROOT / ".gitignore", self.root / ".gitignore")
        run(["git", "-C", str(self.root), "init", "-b", "main"])
        run(["git", "-C", str(self.root), "config", "user.name", "Layout fixture"])
        run(["git", "-C", str(self.root), "config", "user.email", "layout@example.invalid"])
        run(["git", "-C", str(self.root), "add", "."])
        run(["git", "-C", str(self.root), "commit", "-m", "synthetic installation"])
        checkout = self.base / "user-clone"
        run(["git", "clone", "--no-hardlinks", str(self.root), str(checkout)])
        run([sys.executable, "-m", "venv", "--without-pip", str(checkout / ".venv")])
        docs = self.base / "configured-documentation"
        configuration, extension = self.base / "configuration", self.base / "extension"
        for folder in (configuration, extension):
            folder.mkdir()
            (folder / "Configuration.xml").write_text("<SyntheticFixture/>", encoding="utf-8")
        arguments = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(checkout / "scripts/configure-project.ps1"), "-WorkflowRepositoryUrl", str(self.root),
            "-DocumentationPath", str(docs), "-InitializeDocumentationRepository", "-Confirmed",
            "-ExtensionMode", "LocalExport", "-ExtensionPath", str(extension),
            "-ConfigurationPath", str(configuration), "-GiteaBaseUrl", "https://example.invalid",
            "-GiteaOwner", "synthetic", "-GiteaRepository", "documentation",
            "-GitUserName", "Layout fixture", "-GitUserEmail", "layout@example.invalid",
            "-Profile", "project-basic", "-Json"]
        self.assertEqual(json.loads(run(arguments).stdout)["state"], "COMPLETE")
        self.assertEqual({p.name for p in docs.iterdir()}, {".git", ".flow1c", "README.md",
            "Материалы встреч", "Шаблоны документов", "Реестр процессов и требований", "Документы для анализа"})
        self.assertEqual(storage.read_json(checkout / ".flow1c.local.json")["template_library"]["root"], ".flow1c/document-templates")
        evidence = docs / ".flow1c/drafts/saved/evidence.json"
        storage.write_json(evidence, {"answers": ["retained"], "state": "WAITING_USER"})
        before = evidence.read_bytes()
        self.assertEqual(json.loads(run(arguments).stdout)["state"], "COMPLETE")
        self.assertEqual(evidence.read_bytes(), before)
        self.assertFalse((docs / "drafts").exists())


if __name__ == "__main__":
    unittest.main()
