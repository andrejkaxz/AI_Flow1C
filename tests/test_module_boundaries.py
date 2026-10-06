"""Regressions for the concrete module boundaries of the extracted runtime."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ModuleBoundaryTests(unittest.TestCase):
    def test_services_have_no_cli_dependencies_output_capture_or_cycles(self) -> None:
        paths = list((ROOT / "flow1c").rglob("*.py"))
        modules = {".".join(path.relative_to(ROOT).with_suffix("").parts): path for path in paths}
        graph: dict[str, set[str]] = {name: set() for name in modules}
        for name, path in modules.items():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if name != "flow1c.cli":
                        self.assertNotIn(module, {"flow1c.cli", "scripts.flow1c"}, str(path))
                    if module in modules:
                        graph[name].add(module)
                    for alias in node.names:
                        dependency = module + "." + alias.name
                        if dependency in modules:
                            graph[name].add(dependency)
                        self.assertNotIn(alias.name, {"redirect_stdout", "run_captured"}, str(path))
                    self.assertFalse(module.startswith("flow1c_"), str(path))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotEqual(node.func.id, "globals", str(path))
                    if name != "flow1c.cli":
                        self.assertNotEqual(node.func.id, "print", str(path))
                        self.assertFalse(node.func.id.startswith("cmd_"), str(path))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if name != "flow1c.cli":
                        self.assertFalse(node.func.attr.startswith("cmd_"), str(path))

        def visit(name: str, chain: tuple[str, ...]) -> None:
            self.assertNotIn(name, chain, " -> ".join((*chain, name)))
            for dependency in graph[name]:
                visit(dependency, (*chain, name))

        for name in graph:
            visit(name, ())

        launcher = ast.parse((ROOT / "scripts/flow1c.py").read_text(encoding="utf-8"))
        self.assertFalse(
            any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in launcher.body)
        )

    def test_every_runtime_module_imports_without_optional_packages_or_io(self) -> None:
        modules = sorted(
            ".".join(path.relative_to(ROOT).with_suffix("").parts)
            for path in (ROOT / "flow1c").rglob("*.py")
            if path.name != "__init__.py"
        )
        program = """
import importlib, json, pathlib, subprocess, urllib.request
from unittest import mock
from contextlib import ExitStack
def forbidden(*args, **kwargs):
    raise AssertionError('Import attempted runtime I/O')
with ExitStack() as stack:
    for name in ('read_text', 'read_bytes', 'write_text', 'write_bytes', 'mkdir', 'open'):
        stack.enter_context(mock.patch.object(pathlib.Path, name, forbidden))
    stack.enter_context(mock.patch.object(subprocess, 'run', forbidden))
    stack.enter_context(mock.patch.object(subprocess, 'Popen', forbidden))
    stack.enter_context(mock.patch.object(urllib.request, 'urlopen', forbidden))
    for name in MODULES:
        importlib.import_module(name)
import sys
assert not {'openpyxl', 'docx', 'markitdown', 'rlm_tools_bsl'} & set(sys.modules)
print(json.dumps({'imported': len(MODULES)}))
""".replace("MODULES", repr(modules))
        result = subprocess.run(
            [sys.executable, "-S", "-c", program],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"imported": len(modules)})

    def test_registry_reconciliation_calls_the_data_service(self) -> None:
        from unittest import mock
        from flow1c import registry, work_items
        from flow1c.results import OperationResult

        failure = {"state": "BLOCKED", "errors": [{"kind": "invalid_workbook"}]}
        with (
            mock.patch.object(
                work_items,
                "load_manifest",
                return_value=(None, {"traceability_mode": "provisional"}),
            ),
            mock.patch.object(
                registry, "registry_import", return_value=OperationResult(failure, 2)
            ) as imported,
        ):
            code, value = work_items.reconcile_registry("USER-7", "missing.xlsx", product_root=ROOT)
        self.assertEqual((code, value), (2, failure))
        self.assertEqual(imported.call_args.kwargs, {"product_root": ROOT})

    def test_template_document_dispatch_receives_a_path_and_returns_data(self) -> None:
        from argparse import Namespace
        from unittest import mock
        from flow1c import storage, templates
        from flow1c.workflow import state
        from scripts.flow1c_templates import TemplateService

        gate = {
            "gate_id": "synthetic",
            "mode": "draft",
            "operation": "template-document",
            "state": "READY",
        }
        service = mock.Mock(spec=TemplateService)
        service.documentation = ROOT.parent / "synthetic-documentation"
        service.dispatch.return_value = {"state": "PLANNED", "plan_id": "synthetic-plan"}
        args = Namespace(
            action="document-plan", request={"operations": []}, gate_id=gate["gate_id"]
        )
        with (
            mock.patch.object(state, "load_gate", return_value=gate),
            mock.patch.object(state, "require_gate_tool"),
            mock.patch.object(state, "save_gate"),
            mock.patch.object(
                storage,
                "read_json",
                return_value={"documentation_path": str(service.documentation)},
            ),
            mock.patch("scripts.flow1c_templates.TemplateService", return_value=service),
        ):
            result = templates.command(args, product_root=ROOT)
        self.assertEqual(result.value["state"], "PLANNED")
        self.assertEqual(result.exit_code, 0)
        service.dispatch.assert_called_once_with(
            "document-plan", {"operations": []}, service.documentation / "drafts/synthetic"
        )
