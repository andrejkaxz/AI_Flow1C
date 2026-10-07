"""Public CLI baseline captured before the modularization, without live services."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from flow1c import cli
from routing_fixture import install_route_assets

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "cli-baseline"


def parser_contract(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Record every command, option, default, choice and parser constraint."""
    actions = []
    commands = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands = {name: parser_contract(child) for name, child in action.choices.items()}
        else:
            actions.append(
                {
                    "options": action.option_strings,
                    "dest": action.dest,
                    "action": type(action).__name__,
                    "required": action.required,
                    "nargs": action.nargs,
                    "default": action.default,
                    "const": action.const,
                    "choices": list(action.choices) if action.choices is not None else None,
                    "type": getattr(action.type, "__name__", None),
                    "help": action.help,
                }
            )
    return {
        "actions": actions,
        "commands": commands,
        "subcommand_required": any(
            (isinstance(a, argparse._SubParsersAction) and a.required for a in parser._actions)
        ),
        "exclusive_groups": [
            {"required": group.required, "fields": [a.dest for a in group._group_actions]}
            for group in parser._mutually_exclusive_groups
        ],
    }


class CliScenario:
    """Run separate CLI processes in a minimal standalone checkout."""

    def __init__(self, directory: Path) -> None:
        self.root = (directory / "checkout").resolve()
        self.outside = (directory / "outside").resolve()
        self.outside.mkdir()
        for name in ("scripts", "config", "schemas", "flow1c"):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source, self.root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                )
        install_route_assets(self.root)
        self.gate_id: str | None = None

    def run(self, arguments: list[str], request: Any = None, *, outside: bool = False) -> dict:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-B", str(self.root / "scripts" / "flow1c.py"), *arguments],
            cwd=self.outside if outside else self.root,
            input=json.dumps(request, ensure_ascii=False) if request is not None else None,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=environment,
            timeout=30,
        )
        value = json.loads(result.stdout) if result.stdout.strip() else None
        if isinstance(value, dict) and value.get("gate_id"):
            self.gate_id = value["gate_id"]
        return self.normalize(
            {"exit_code": result.returncode, "stdout": value, "stderr": result.stderr}
        )

    def normalize(self, value: Any, key: str = "") -> Any:
        if key in {"created_at", "updated_at", "completed_at", "selected_at"} and isinstance(
            value, str
        ):
            return "<TIME>"
        if isinstance(value, dict):
            return {k: self.normalize(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.normalize(v) for v in value]
        if isinstance(value, str):
            value = value.replace(str(self.root), "<CHECKOUT>")
            value = value.replace(self.root.as_posix(), "<CHECKOUT>")
            if "<CHECKOUT>" in value:
                value = value.replace("\\", "/")
            if self.gate_id:
                value = value.replace(self.gate_id, "<GATE_ID>")
        return value

    def capture(self) -> dict:
        results = {}
        results["unknown_json_field"] = self.run(
            ["agent-begin", "--json-stdin"], {"unknown_field": True}
        )
        results["non_object_json"] = self.run(["agent-begin", "--json-stdin"], ["invalid"])
        results["missing_gate"] = self.run(
            ["agent-complete", "--json-stdin"], {"gate_id": "00000000-0000-0000-0000-000000000000"}
        )
        results["begin"] = self.run(
            [
                "agent-begin",
                "--operation",
                "functional-spec",
                "--mode",
                "draft",
                "--summary",
                "Описать приёмку товара",
            ],
            outside=True,
        )
        results["resume"] = self.run(
            ["agent-dialogue", "--json-stdin"],
            {
                "gate_id": self.gate_id,
                "action": "record",
                "answer": "Количество сверяют с накладной.",
            },
        )
        results["empty_completion"] = self.run(
            ["agent-complete", "--json-stdin"], {"gate_id": self.gate_id}
        )
        results["unsafe_write"] = self.run(
            ["agent-write", "--json-stdin"],
            {
                "gate_id": self.gate_id,
                "target": "draft",
                "path": "../escape.md",
                "content": "unsafe",
            },
        )
        results["write"] = self.run(
            ["agent-write", "--json-stdin"],
            {
                "gate_id": self.gate_id,
                "target": "draft",
                "path": "result.md",
                "content": "# Приёмка\n\nОператор сверяет количество.",
            },
            outside=True,
        )
        results["complete"] = self.run(
            ["agent-complete", "--json-stdin"], {"gate_id": self.gate_id}, outside=True
        )
        request_root = self.root / ".workspace" / "drafts" / str(self.gate_id)
        results["saved_request"] = self.normalize(
            json.loads((request_root / "request.json").read_text(encoding="utf-8"))
        )
        results["saved_evidence"] = self.normalize(
            json.loads((request_root / "evidence.json").read_text(encoding="utf-8"))
        )
        results["document"] = (request_root / "result.md").read_text(encoding="utf-8")
        return results


class CliContractTests(unittest.TestCase):

    def test_all_parser_contracts_match_baseline(self) -> None:
        expected = json.loads((FIXTURES / "parser.json").read_text(encoding="utf-8"))
        actual = parser_contract(cli.build_parser())
        self.assertEqual(set(actual["commands"]) - set(expected["commands"]), {"route-catalog", "route-check", "agent-handoff", "context-read"})
        for name in ("route-catalog", "route-check", "agent-handoff", "context-read"):
            actual["commands"].pop(name)
        # New explicit context options are additive; every legacy option/default is exact.
        additions = {"context-build": {"view", "gate_id"},
                     "agent-context": {"view", "action", "entry_id", "section_id", "cursor", "max_chars"}}
        for name, fields in additions.items():
            added = [a for a in actual["commands"][name]["actions"] if a["dest"] in fields]
            self.assertEqual({a["dest"] for a in added}, fields)
            self.assertTrue(all(not a["required"] for a in added))
            self.assertEqual(next(a["default"] for a in added if a["dest"] == "view"), "full")
            actual["commands"][name]["actions"] = [a for a in actual["commands"][name]["actions"] if a["dest"] not in fields]
        self.assertEqual(actual, expected)

    def test_help_runs_from_an_unrelated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = CliScenario(Path(directory))
            result = subprocess.run(
                [sys.executable, str(scenario.root / "scripts" / "flow1c.py"), "--help"],
                cwd=scenario.outside,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Flow1C CLI", result.stdout)

    def test_checkout_on_pythonpath_does_not_shadow_the_package_with_the_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = CliScenario(Path(directory))
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(scenario.root)
            result = subprocess.run(
                [sys.executable, str(scenario.root / "scripts" / "flow1c.py"), "--help"],
                cwd=scenario.outside,
                env=environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Flow1C CLI", result.stdout)

    def test_process_restart_errors_and_draft_completion_match_baseline(self) -> None:
        expected = json.loads((FIXTURES / "draft-lifecycle.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            scenario = CliScenario(Path(directory))
            actual = scenario.capture()
            for name in (
                "unknown_json_field",
                "non_object_json",
                "missing_gate",
                "empty_completion",
                "unsafe_write",
            ):
                self.assertEqual(actual[name]["exit_code"], 2, name)
            self.assertEqual(actual["complete"]["stdout"]["state"], "DRAFT_COMPLETE")
            self.assertEqual(
                actual["saved_request"]["notes"][0]["text"], "Количество сверяют с накладной."
            )
            document_path = scenario.root / ".workspace/drafts" / str(scenario.gate_id) / "result.md"
            # Persisted evidence must match actual bytes on both platforms. The
            # historical fixture was captured with Windows CRLF; keep it intact
            # and derive its expected digest for the native text writer.
            actual_digest = hashlib.sha256(document_path.read_bytes()).hexdigest()
            self.assertEqual(actual["saved_evidence"]["changed_files"][0]["sha256"], actual_digest)
            windows_digest = hashlib.sha256(expected["document"].replace("\n", "\r\n").encode("utf-8")).hexdigest()
            self.assertEqual(expected["saved_evidence"]["changed_files"][0]["sha256"], windows_digest)
            expected["saved_evidence"]["changed_files"][0]["sha256"] = hashlib.sha256(
                expected["document"].replace("\n", os.linesep).encode("utf-8")
            ).hexdigest()
            def legacy_fields(value, parent=""):
                if isinstance(value, dict):
                    return {key: legacy_fields(item, key) for key, item in value.items()
                            if key not in {"route_decision", "route_origin", "completion_record", "handoff", "handoff_status"}}
                if isinstance(value, list):
                    if parent in {"available_actions", "allowed_tools"}:
                        value = [item for item in value if item != "flow1c_context"]
                    return [legacy_fields(item) for item in value]
                return value
            self.assertEqual(legacy_fields(actual), expected)


if __name__ == "__main__":
    unittest.main()
