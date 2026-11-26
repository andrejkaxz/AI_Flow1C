from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from flow1c import storage, workbook_format, workbook_safety
from flow1c.workbook_format_policy import WorkbookFormatError, format_map
from memory_fixture import review_before_complete
import test_flow1c as fixtures

flow1c = fixtures.flow1c


MAPPING = {
    "target_sheet": "Load", "target_range": "A11:K11",
    "reference_sheet": "Reference", "reference_range": "B3", "row_heights": True,
}


def workbook(path: Path, *, content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml") -> Path:
    # The native Excel boundary is mocked. A synthetic container exercises preflight.
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", f'<Types><Override ContentType="{content_type}" /></Types>')
        archive.writestr("xl/workbook.xml", "<workbook />")
    return path


class WorkbookFormatPolicyTests(unittest.TestCase):
    def test_single_reference_cell_repeats_without_changing_dimensions_by_default(self) -> None:
        self.assertEqual(format_map([MAPPING], ".xls", ".xlsx"), [{**MAPPING, "column_widths": False}])
        for invalid in (
            {"target_range": "IW11"}, {"target_range": "[Other.xlsx]Load!A1"},
            {"target_range": "C4:B9"}, {"target_range": "A1:A4097"},
            {"reference_range": "B3:C3"}, {"row_heights": "true"}, {"number_format": "0.00"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(WorkbookFormatError):
                format_map([{**MAPPING, **invalid}], ".xls", ".xlsx")

    def test_legacy_macro_sheet_and_password_records_are_rejected(self) -> None:
        globals_bof = bytes.fromhex("0908100000060500") + bytes(12)
        for item in (bytes.fromhex("850008000000000000010000"), bytes.fromhex("2f000000")):
            with patch.object(workbook_safety, "_biff_workbook", return_value=globals_bof + item + b"\n\0\0\0"):
                with self.assertRaises(ValueError):
                    workbook_safety._check_biff(b"")


class WorkbookFormatGateTests(unittest.TestCase):
    setUp = fixtures.NaturalLanguageGateTests.setUp
    tearDown = fixtures.NaturalLanguageGateTests.tearDown

    def call(self, verb: str, expected: int = 0, **request: object) -> dict:
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request))), redirect_stdout(output):
            code = flow1c.main([verb, "--json-stdin"])
        self.assertEqual(code, expected, output.getvalue())
        return json.loads(output.getvalue())

    def scenario(self) -> tuple[str, Path, Path, Path]:
        for relative in (
            ".agents/skills/flow1c-workbook-format/SKILL.md",
            "docs/workbook-format.md",
        ):
            path = self.root / relative
            path.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(self.old_root / relative, path)
        folder = self.root / "test-project-workbooks"
        folder.mkdir()
        local = storage.read_json(self.root / ".flow1c.local.json", {})
        storage.write_json(self.root / ".flow1c.local.json", {**local, "documentation_path": str(folder)})
        source = workbook(folder / "Исходник.xlsx")
        reference = workbook(folder / "Эталон.xlsx")
        profile = folder / "Оформление.json"
        storage.write_json(profile, {"schema_version": 1, "reference": reference.name, "mappings": [MAPPING]})
        gate = self.call("agent-begin", route_proposal={
            "schema_version": 1, "expected_outcome": "Оформи Excel по образцу",
            "operation": "workbook-format", "mode": "draft",
            "sources": [{"kind": "attachment", "version": "provided", "selector": source.name}],
        })
        self.assertIn("flow1c_workbook", gate["available_actions"])
        return gate["gate_id"], source, reference, profile

    def test_profile_and_multiple_outputs_are_idempotent_and_sealed(self) -> None:
        gate_id, source, reference, profile = self.scenario()
        hashes = storage.sha256(source), storage.sha256(reference)
        second = workbook(source.with_name("Второй.xlsx"))
        listing = self.call("workbook-format", gate_id=gate_id, action="list", request={"folder": "."})
        self.assertCountEqual([Path(path) for path in listing["files"]],
                              [path.resolve() for path in (source, second, reference)])

        def verified_copy(action: str, request: dict, *_: object) -> dict:
            self.assertEqual(action, "write")
            self.assertNotEqual(request["source"], str(source))
            shutil.copyfile(request["source"], request["output"])
            return {"state": "VERIFIED", "preservation": "PASSED"}

        def write(book: Path) -> dict:
            return self.call("workbook-format", gate_id=gate_id, action="write",
                             request={"source": str(book), "profile": profile.name})

        with patch.object(workbook_format, "_excel", side_effect=verified_copy) as native:
            first = write(source)
            retry = write(source)
            other = write(second)
        self.assertTrue(retry["reused"])
        self.assertEqual(retry["output"], first["output"])
        self.assertEqual(native.call_count, 2)
        self.assertEqual(hashes, (storage.sha256(source), storage.sha256(reference)))
        review_before_complete(self.root, gate_id)
        complete = self.call("agent-complete", gate_id=gate_id)
        self.assertEqual(complete["state"], "DRAFT_COMPLETE")
        self.assertEqual(complete["outputs"], [first["output"], other["output"]])
        self.assertTrue(all(Path(path).is_file() for path in complete["outputs"]))

    def test_invalid_paths_macros_and_unverified_results_stop_before_publication(self) -> None:
        gate_id, source, reference, profile = self.scenario()

        def write(book: Path) -> dict:
            return self.call("workbook-format", expected=2, gate_id=gate_id, action="write",
                             request={"source": str(book), "profile": profile.name})

        outside = workbook(self.root / "outside.xlsx")
        macro = workbook(source.with_name("Macro.xlsx"), content_type="application/vnd.ms-excel.sheet.macroEnabled.main+xml")
        with patch.object(workbook_format, "_excel", return_value={"state": "VERIFIED"}) as native:
            self.assertEqual(write(outside)["errors"][0]["code"], "WORKBOOK_PATH")
            self.assertEqual(write(macro)["errors"][0]["code"], "WORKBOOK_FILE")
            native.assert_not_called()
            self.assertEqual(write(source)["errors"][0]["code"], "WORKBOOK_VERIFICATION_FAILED")
        self.assertEqual(list(source.parent.rglob("*-formatted-*.xlsx")), [])
        review_before_complete(self.root, gate_id)
        self.assertEqual(self.call("agent-complete", expected=2, gate_id=gate_id)["errors"][0]["code"],
                         "WORKBOOK_OUTPUT_REQUIRED")
