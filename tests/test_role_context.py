from __future__ import annotations
from flow1c import context as svc_context
from flow1c.workflow import state as svc_workflow_state
from flow1c.storage import read_json
from flow1c.storage import sha256
from flow1c.storage import write_json
from flow1c.storage import write_text
import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from flow1c import cli

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/cli-baseline/role-context.json"


def prepare_context(root: Path, mode: str) -> Path:
    item = root / "work-items" / "TASK-42"
    write_json(
        item / "manifest.yaml",
        {
            "code": "TASK-42",
            "work_reference": "TASK-42",
            "title": "Приёмка товара",
            "status": "clarification",
            "traceability_mode": mode,
            "requirements": ["SLS-001", "SLS-002"],
        },
    )
    write_json(
        item / "input/requirements.snapshot.yaml",
        {"SLS-001": {"text": "Сверить количество", "process": {"code": "01.01"}}},
    )
    write_text(item / "input/user-brief.md", "Описание пользователя: проверить количество.")
    for name in (
        "analysis/questions.md",
        "analysis/answers.md",
        "analysis/decisions.md",
        "analysis/traceability.md",
        "specification/functional-spec.md",
        "specification/technical-design.md",
        "testing/test-plan.md",
    ):
        write_text(item / name, f"Содержимое {name}")
    write_json(
        item / "input/artifacts.json",
        {
            "artifacts": [
                {
                    "category": "meeting",
                    "relative_path": "input/meetings/notes.md",
                    "sha256": "a" * 64,
                }
            ],
            "confirmed_absent": ["screenshots"],
        },
    )
    return item


def capture_contexts(root: Path) -> dict[str, str]:
    results = {}
    write_json(root / "config/stages.json", read_json(ROOT / "config/stages.json"))
    with mock.patch.object(cli, "ROOT", root):
        for mode in ("registry", "provisional"):
            item = prepare_context(root, mode)
            for role in svc_context.VALID_ROLES:
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli.main(["context-build", "--code", "TASK-42", "--role", role])
                if code != 0:
                    raise AssertionError(f"Context failed: {output.getvalue()}")
                path = Path(output.getvalue().strip())
                if path != item / "context" / f"{role}.md":
                    raise AssertionError(f"Context destination changed: {path}")
                results[f"{mode}:{role}"] = path.read_text(encoding="utf-8")
    return results


class RoleContextTests(unittest.TestCase):

    def test_all_roles_and_traceability_modes_match_baseline(self) -> None:
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(capture_contexts(Path(directory).resolve()), expected)

    def test_gated_context_uses_service_and_isolates_hashed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            item = prepare_context(root, "registry")
            evidence_path = item / "evidence" / "functional-spec-fixture.json"
            gate = {
                "gate_id": "00000000-0000-0000-0000-000000000042",
                "mode": "formal",
                "state": "READY",
                "code": "TASK-42",
                "work_reference": "TASK-42",
                "operation": "functional-spec",
                "context_role": "analyst",
                "evidence_path": str(evidence_path),
            }
            write_json(evidence_path, {"schema_version": 2, "gate_id": gate["gate_id"]})
            stages = read_json(ROOT / "config/stages.json")
            output = io.StringIO()
            with (
                mock.patch.object(cli, "ROOT", root),
                mock.patch.object(svc_workflow_state, "load_gate", return_value=gate),
                mock.patch.object(svc_context, "load_stages", return_value=stages),
                mock.patch.object(
                    cli,
                    "cmd_context_build",
                    side_effect=AssertionError("Do not call a CLI handler"),
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    cli.cmd_agent_context(argparse.Namespace(gate_id=gate["gate_id"])), 0
                )
            result = json.loads(output.getvalue())
            saved = read_json(evidence_path)
            isolated = Path(result["path"])
            self.assertEqual(isolated.parent, evidence_path.parent)
            self.assertEqual(
                result["content"], (item / "context/analyst.md").read_text(encoding="utf-8")
            )
            self.assertEqual(result["sha256"], sha256(isolated))
            self.assertEqual(saved["context_sha256"], result["sha256"])
            write_text(item / "context/analyst.md", "Changed after context generation")
            self.assertEqual(sha256(isolated), result["sha256"])


if __name__ == "__main__":
    unittest.main()
