from __future__ import annotations

import argparse
import ctypes
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any
from unittest import mock

from scripts import redmine_client, redmine_credentials, flow1c
from scripts import redmine_policy


@contextmanager
def fake_credential_manager(
    values: dict[str, str], *, write_hook=None, delete_hook=None, verify_hook=None
):
    backups: dict[str, str] = {}

    def read(url):
        return values.get(url)

    def write(url, key):
        if write_hook:
            return write_hook(url, key, values)
        values[url] = key

    def verify(url, key):
        if verify_hook:
            return verify_hook(url, key, values)
        return values.get(url) == key

    def delete(url):
        if delete_hook:
            return delete_hook(url, values)
        return values.pop(url, None) is not None

    def read_backup(transaction_id):
        return backups.get(transaction_id)

    def write_backup(transaction_id, key):
        backups[transaction_id] = key

    def delete_backup(transaction_id):
        return backups.pop(transaction_id, None) is not None

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(flow1c.sys, "platform", "win32"))
        stack.enter_context(mock.patch.object(flow1c.getpass, "getpass", return_value="credential-test-key"))
        stack.enter_context(mock.patch.object(redmine_credentials, "read_api_key", side_effect=read))
        stack.enter_context(mock.patch.object(redmine_credentials, "write_api_key", side_effect=write))
        stack.enter_context(mock.patch.object(redmine_credentials, "verify_api_key", side_effect=verify))
        stack.enter_context(mock.patch.object(redmine_credentials, "delete_api_key", side_effect=delete))
        stack.enter_context(mock.patch.object(redmine_credentials, "read_transaction_backup", side_effect=read_backup))
        stack.enter_context(mock.patch.object(redmine_credentials, "write_transaction_backup", side_effect=write_backup))
        stack.enter_context(mock.patch.object(redmine_credentials, "delete_transaction_backup", side_effect=delete_backup))
        yield values, backups


def write_local_config(root: Path, value: dict) -> bytes:
    (root / "config").mkdir(exist_ok=True)
    stages = Path(flow1c.__file__).resolve().parents[1] / "config" / "stages.json"
    (root / "config" / "stages.json").write_bytes(stages.read_bytes())
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (root / ".flow1c.local.json").write_bytes(payload)
    return payload


class Response(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, responses: list[bytes]):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        return response if hasattr(response, "read") else Response(response)


class HeaderResponse(Response):
    def __init__(self, value: bytes, headers: dict[str, str] | None = None):
        super().__init__(value)
        self.headers = headers or {}


class RedmineClientTests(unittest.TestCase):
    def test_dmsf_upload_preflight_never_posts_without_confirmation(self) -> None:
        source = Path(__file__).resolve().parents[1] / "README.md"
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=FakeOpener([])):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        with mock.patch.object(client, "_dmsf_available"), \
                mock.patch.object(client, "_issue_page_dmsf_links", return_value=[]), \
                mock.patch.object(client, "dmsf_file_inventory", return_value=([], [])), \
                mock.patch.object(client, "_post") as post:
            result = client.upload_dmsf_file(
                {"id": 11993, "project_id": 42, "project": "ERP", "journals": []}, source, confirmed=False
            )
        self.assertEqual(result["state"], "NEEDS_CONFIRMATION")
        self.assertEqual(result["preflight"]["project_id"], 42)
        post.assert_not_called()

    def test_dmsf_upload_accepts_empty_issue_without_rendered_dms_section(self) -> None:
        source = Path(__file__).resolve().parents[1] / "README.md"
        page_without_dms = b'<div class="issue details"><div class="attachments"></div></div>'
        page_with_dms = (b'<div class="issue details"><div class="attachments dmsf-parent-container">'
                         b'<a class="dmsf-icon-file" href="/dmsf/files/7001/view">README.md</a></div></div>')
        opener = FakeOpener([page_without_dms, page_with_dms])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        with mock.patch.object(client, "_dmsf_available"), \
                mock.patch.object(client, "_write", side_effect=[
                    json.dumps({"upload": {"token": "temporary-token"}}).encode(), b"",
                ]) as write, \
                mock.patch.object(client, "issue", return_value={"id": 11993, "project_id": 42, "journals": []}):
            result = client.upload_dmsf_file(
                {"id": 11993, "project_id": 42, "project": "ERP", "journals": []},
                source,
                confirmed=True,
            )
        self.assertEqual(result["state"], "UPLOADED")
        self.assertEqual(result["preflight"]["issue_id"], 11993)
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(all(item[0].full_url == "https://redmine.example.org/issues/11993" for item in opener.requests))
        self.assertEqual(write.call_count, 2)

    def test_dmsf_upload_blocks_when_target_issue_page_cannot_be_verified(self) -> None:
        source = Path(__file__).resolve().parents[1] / "README.md"
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=FakeOpener([])):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        warning = {"code": "REDMINE_DMSF_UNAVAILABLE", "message": "issue page could not be verified"}
        with mock.patch.object(client, "_dmsf_available"), \
                mock.patch.object(client, "dmsf_file_inventory", return_value=([], [warning])), \
                mock.patch.object(client, "_post") as post:
            with self.assertRaises(redmine_client.RedmineError) as error:
                client.upload_dmsf_file(
                    {"id": 11993, "project_id": 42, "project": "ERP", "journals": []},
                    source,
                    confirmed=True,
                )
        self.assertEqual(error.exception.code, "REDMINE_DMSF_UNAVAILABLE")
        post.assert_not_called()

    def test_dmsf_upload_streams_unicode_file_and_verifies_issue_link(self) -> None:
        source = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "Документ.txt"
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=FakeOpener([])):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        calls: list[tuple[str, Any, dict[str, str], str, str]] = []

        def write(url, data, headers, *, operation, method="POST"):
            calls.append((url, data, headers, operation, method))
            if operation == "upload":
                self.assertTrue(hasattr(data, "read"))
                self.assertFalse(isinstance(data, (bytes, bytearray)))
                return json.dumps({"upload": {"token": "temporary-token-that-must-not-escape"}}).encode()
            payload = json.loads(data.decode("utf-8"))
            self.assertEqual(url, "https://redmine.example.org/issues/11993.json")
            self.assertEqual(method, "PUT")
            self.assertEqual(payload["issue"], {})
            self.assertEqual(payload["dmsf_attachments_upload_choice"], "DMSF")
            self.assertEqual(payload["dmsf_attachments"]["1"]["filename"], "Документ.txt")
            self.assertEqual(payload["dmsf_attachments"]["1"]["token"], "temporary-token-that-must-not-escape")
            self.assertEqual(payload["committed_files"]["1"]["title"], "Документ")
            self.assertNotIn("attachments", payload)
            return b""

        with mock.patch.object(client, "_dmsf_available"), \
                mock.patch.object(client, "dmsf_file_inventory", return_value=([], [])), \
                mock.patch.object(client, "_write", side_effect=write), \
                mock.patch.object(client, "issue", return_value={"id": 11993, "project_id": 42, "journals": []}), \
                mock.patch.object(client, "_issue_page_dmsf_links", return_value=[{"id": 7001, "name": "Документ"}]):
            result = client.upload_dmsf_file(
                {"id": 11993, "project_id": 42, "project": "ERP", "journals": []},
                source,
                confirmed=True,
            )
        self.assertEqual(result["state"], "UPLOADED")
        self.assertEqual([item[3] for item in calls], ["upload", "attach"])
        self.assertIn("filename=", calls[0][0])
        self.assertEqual(calls[0][2]["Content-Type"], "application/octet-stream")
        self.assertEqual(calls[1][2]["Content-Type"], "application/json")
        self.assertNotIn("/dmsf/commit", calls[1][0])

    def test_dmsf_upload_name_conflict_is_rejected_before_post(self) -> None:
        source = Path(__file__).resolve().parents[1] / "README.md"
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=FakeOpener([])):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        with mock.patch.object(client, "_dmsf_available"), \
                mock.patch.object(client, "_issue_page_dmsf_links", return_value=[]), \
                mock.patch.object(client, "dmsf_file_inventory", return_value=([{"id": 1, "name": "README.md"}], [])), \
                mock.patch.object(client, "_post") as post:
            with self.assertRaises(redmine_client.RedmineError) as error:
                client.upload_dmsf_file({"id": 11993, "project_id": 42, "journals": []}, source, confirmed=True)
        self.assertEqual(error.exception.code, "REDMINE_DMSF_UPLOAD_NAME_CONFLICT")
        post.assert_not_called()

    def test_dmsf_revision_targets_attached_file_folder_and_checks_previous_revision(self) -> None:
        source = Path(__file__).resolve().parents[1] / "README.md"
        client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        old = {"id": 5409, "name": "README.md", "title": "README", "project_id": 42,
               "dmsf_folder_id": 3209, "revision": {"id": 9059}}
        new = {**old, "revision": {"id": 9060}, "size": source.stat().st_size}
        issue = {"id": 11993, "project_id": 42, "journals": []}
        calls = []

        def post(url, data, headers, *, operation):
            calls.append(operation)
            if operation == "upload":
                return json.dumps({"upload": {"token": "secret-revision-token"}}).encode()
            payload = json.loads(data.decode("utf-8"))
            self.assertEqual(payload["attachments"]["folder_id"], 3209)
            self.assertNotIn("issue_id", payload["attachments"])
            self.assertEqual(payload["attachments"]["uploaded_file"]["name"], "README.md")
            return json.dumps({"dmsf_files": [{"id": 5409, "name": "README.md"}]}).encode()

        with mock.patch.object(client, "dmsf_file_inventory", return_value=([old], [])), \
                mock.patch.object(client, "_dmsf_available"), \
                mock.patch.object(client, "_post", side_effect=post), \
                mock.patch.object(client, "issue", return_value=issue), \
                mock.patch.object(client, "dmsf_file_metadata", return_value=new), \
                mock.patch.object(client, "is_dmsf_attached", return_value=True):
            preview = client.revise_dmsf_file(issue, 5409, source, expected_revision_id=9059, confirmed=False)
            self.assertEqual(preview["state"], "NEEDS_CONFIRMATION")
            self.assertEqual(calls, [])
            result = client.revise_dmsf_file(issue, 5409, source, expected_revision_id=9059, confirmed=True)
        self.assertEqual(result["state"], "REVISED")
        self.assertEqual(result["dms_file"]["revision_id"], 9060)
        self.assertEqual(calls, ["upload", "attach"])

    def test_dmsf_revision_rejects_stale_selection_before_post(self) -> None:
        source = Path(__file__).resolve().parents[1] / "README.md"
        client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        old = {"id": 5409, "name": "README.md", "project_id": 42,
               "dmsf_folder_id": 3209, "revision": {"id": 9060}}
        issue = {"id": 11993, "project_id": 42, "journals": []}
        with mock.patch.object(client, "dmsf_file_inventory", return_value=([old], [])), \
                mock.patch.object(client, "_post") as post:
            with self.assertRaises(redmine_client.RedmineError) as error:
                client.revise_dmsf_file(issue, 5409, source, expected_revision_id=9059, confirmed=True)
        self.assertEqual(error.exception.code, "REDMINE_DMSF_REVISION_CONFLICT")
        post.assert_not_called()

    def test_uncertain_commit_is_not_retried_and_token_is_not_returned(self) -> None:
        source = Path(__file__).resolve().parents[1] / "README.md"
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=FakeOpener([])):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        with mock.patch.object(client, "_dmsf_available"), \
                mock.patch.object(client, "_issue_page_dmsf_links", return_value=[]), \
                mock.patch.object(client, "dmsf_file_inventory", return_value=([], [])), \
                mock.patch.object(client, "_write", side_effect=[
                    json.dumps({"upload": {"token": "secret-temporary-token"}}).encode(),
                    redmine_client.RedmineError(
                        "issue attachment response was inconclusive",
                        code="REDMINE_DMSF_COMMIT_UNCERTAIN",
                    ),
                ]) as write:
            result = client.upload_dmsf_file({"id": 11993, "project_id": 42, "journals": []}, source, confirmed=True)
        self.assertEqual(result["state"], "COMMITTED_UNVERIFIED")
        self.assertEqual(result["errors"][0]["code"], "REDMINE_DMSF_COMMIT_UNCERTAIN")
        self.assertNotIn("secret-temporary-token", json.dumps(result))
        self.assertEqual(write.call_count, 2)

    def test_related_issues_reads_both_directions_and_issue_fields(self) -> None:
        source = {"issue": {"id": 11105, "subject": "Main", "description": "Description", "tracker": {"name": "Change"},
                            "status": {"name": "Closed"}, "project": {"name": "Project"}, "relations": [
                                {"id": 1, "issue_id": 11105, "issue_to_id": 11440, "relation_type": "relates"},
                                {"id": 2, "issue_id": 12001, "issue_to_id": 11105, "relation_type": "blocks"},
                                {"id": 3, "issue_id": 11105, "issue_to_id": 12002, "relation_type": "relates"},
                            ]}}
        related = lambda issue_id, tracker, status: {"issue": {"id": issue_id, "subject": f"Task {issue_id}",
            "tracker": {"name": tracker}, "status": {"name": status}, "project": {"name": "Project"}}}
        opener = FakeOpener([json.dumps(item).encode() for item in (
            source, related(11440, "Development", "Closed"), related(12001, "Defect", "Open"),
            related(12002, "Support", "In progress"))])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            result = redmine_client.RedmineClient("https://redmine.example.org", "key").related_issues(11105)
        self.assertEqual(result["issue"]["tracker"], "Change")
        self.assertEqual([item["relation_label"] for item in result["relations"]],
                         ["Связана с", "Заблокирована задачей", "Связана с"])
        self.assertEqual([(item["id"], item["direction"], item["tracker"], item["status"])
                          for item in result["relations"]], [
                              (11440, "outgoing", "Development", "Closed"),
                              (12001, "incoming", "Defect", "Open"),
                              (12002, "outgoing", "Support", "In progress")])
        self.assertEqual(len(opener.requests), 4)
        self.assertIn("include=relations", opener.requests[0][0].full_url)
        self.assertTrue(all(request.get_header("X-redmine-api-key") == "key" for request, _ in opener.requests))

    def test_related_issues_keeps_inaccessible_endpoint_and_rejects_bad_relations(self) -> None:
        source = {"issue": {"id": 42, "relations": [
            {"id": 7, "issue_id": 42, "issue_to_id": 99, "relation_type": "relates"}]}}
        with mock.patch.object(redmine_client.urllib.request, "build_opener"):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
        def open_issue(url):
            if url.endswith("/99.json"):
                raise redmine_client.RedmineError("Issue is not visible", code="REDMINE_ACCESS_DENIED")
            return Response(json.dumps(source).encode())
        with mock.patch.object(client, "_open", side_effect=open_issue):
            record = client.related_issues(42)["relations"][0]
            self.assertEqual(record["id"], 99)
            self.assertIsNone(record["status"])
            self.assertEqual(record["error"]["code"], "REDMINE_ACCESS_DENIED")
        source["issue"]["relations"][0]["issue_id"] = 1
        with mock.patch.object(client, "_open", side_effect=open_issue):
            with self.assertRaisesRegex(redmine_client.RedmineError, "does not connect"):
                client.related_issues(42)

    def test_related_issues_requires_relation_list(self) -> None:
        opener = FakeOpener([json.dumps({"issue": {"id": 42}}).encode()])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(redmine_client.RedmineError, "relation list"):
                redmine_client.RedmineClient("https://redmine.example.org", "key").related_issues(42)

    def test_relations_command_reports_partial_result(self) -> None:
        client = mock.Mock()
        client.related_issues.return_value = {"issue": {"id": 42}, "relations": [
            {"id": 99, "relation_type": "relates", "tracker": None, "status": None,
             "error": {"code": "REDMINE_ACCESS_DENIED", "message": "Issue is not visible"}}]}
        output = io.StringIO()
        with mock.patch.object(flow1c, "redmine_client_from_config", return_value=(client, "https://redmine.example.org", "test")), \
                mock.patch("sys.stdout", output):
            exit_code = flow1c.cmd_redmine_relations(argparse.Namespace(issue="42", json=True))
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(result["state"], "PARTIAL")
        self.assertEqual(result["relation_count"], 1)

    def test_relations_command_reports_empty_complete_result(self) -> None:
        client = mock.Mock()
        client.related_issues.return_value = {"issue": {"id": 42, "subject": "Main"}, "relations": []}
        output = io.StringIO()
        with mock.patch.object(flow1c, "redmine_client_from_config", return_value=(client, "https://redmine.example.org", "test")), \
                mock.patch("sys.stdout", output):
            exit_code = flow1c.cmd_redmine_relations(argparse.Namespace(issue="42", json=True))
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["state"], "COMPLETE")
        self.assertEqual(result["relation_count"], 0)

    def test_url_must_be_https_without_userinfo_or_query(self) -> None:
        self.assertEqual(redmine_policy.normalize_base_url(" https://redmine.example.org/rm/ "), "https://redmine.example.org/rm")
        for value in (
            "http://redmine.example.org", "https://user:secret@redmine.example.org",
            "https://redmine.example.org/?token=abc", "https://redmine.example.org/#issue",
        ):
            with self.subTest(value=value), self.assertRaises(redmine_policy.RedminePolicyError):
                redmine_policy.normalize_base_url(value)

    def test_issue_and_same_origin_attachment_use_api_header(self) -> None:
        payload = {"issue": {
            "id": 42, "subject": "Sample", "description": "Issue details", "status": {"name": "New"}, "project": {"name": "ERP"},
            "attachments": [{"id": 9, "filename": "../proposal?.pdf", "filesize": 4,
                             "content_type": "application/pdf", "content_url": "https://redmine.example.org/attachments/9/download"}],
        }}
        opener = FakeOpener([json.dumps(payload).encode(), b"data"])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "private-api-key")
            issue = client.issue("42")
            self.assertEqual(issue["description"], "Issue details")
            with tempfile.TemporaryDirectory() as directory:
                files, metadata = client.download_attachments(issue, Path(directory))
                self.assertEqual(files[0].name, "proposal_-9.pdf")
                self.assertEqual(files[0].read_bytes(), b"data")
                self.assertEqual(metadata[0]["size"], 4)
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(all(request.get_header("X-redmine-api-key") == "private-api-key" for request, _ in opener.requests))
        self.assertIn("include=attachments%2Cjournals", opener.requests[0][0].full_url)

    def test_journal_replay_selects_current_dmsf_files_and_excludes_detached(self) -> None:
        journals = [
            {"id": 9, "created_on": "2026-01-03T00:00:00Z", "details": [
                {"property": "dmsf_file", "name": "3882", "old_value": "Example", "new_value": None},
                {"property": "dmsf_file", "name": "3947", "old_value": "Old", "new_value": "Latest"},
            ]},
            {"id": 2, "created_on": "2026-01-01T00:00:00Z", "details": [
                {"property": "dmsf_file", "name": "3882", "old_value": None, "new_value": "Example"},
                {"property": "dmsf_file", "name": "3947", "old_value": None, "new_value": "Old"},
                {"property": "dmsf_file", "name": "3918", "old_value": None, "new_value": "A"},
            ]},
            {"id": 5, "created_on": "2026-01-02T00:00:00Z", "details": [
                {"property": "dmsf_file", "name": "3919", "old_value": None, "new_value": "B"},
                {"property": "dmsf_file", "name": "4000", "old_value": None, "new_value": "Before"},
                {"property": "dmsf_file", "name": "4000", "old_value": "Before", "new_value": "After"},
                {"property": "dmsf_file", "name": "4001", "old_value": None, "new_value": "C"},
            ]},
        ]
        current = redmine_policy.current_dmsf_file_ids(journals)
        self.assertEqual({item["id"] for item in current}, {3947, 3918, 3919, 4000, 4001})
        self.assertNotIn(3882, {item["id"] for item in current})
        self.assertEqual(next(item["name"] for item in current if item["id"] == 3947), "Latest")
        self.assertEqual(next(item["name"] for item in current if item["id"] == 4000), "After")

    def test_issue_page_finds_dmsf_file_missing_from_api_journals(self) -> None:
        issue_payload = {"issue": {"id": 10360, "attachments": [], "journals": []}}
        page = '''<div class="issue details"><a class="dmsf-icon-file" href="/dmsf/files/999/view">decoy</a>
          <div class="attachments dmsf-parent-container"><table><tr><td>
          <a class="icon dmsf-icon-file" href="/dmsf/files/4614/view">Document</a>
          <a class="icon dmsf-icon-file" href="https://other.example/dmsf/files/7/view">unsafe</a>
          </td></tr></table></div></div>'''
        metadata = {"dmsf_file": {"id": 4614, "name": "Document.docx", "project_id": 21,
                                   "size": 3, "content_type": "application/docx",
                                   "content_url": "https://redmine.example.org/dmsf/files/4614/download",
                                   "dmsf_file_revisions": [
                                       {"id": 7926, "name": "Document.docx", "size": 3, "version": "1",
                                        "mime_type": "application/docx",
                                        "content_url": "https://redmine.example.org/dmsf/files/4614/view?download=7926"}]}}
        detached_page = '<div class="issue details"><div class="attachments dmsf-parent-container"></div></div>'
        page_without_dms = '<div class="issue details"><div class="attachments"></div></div>'
        unrelated_dms = '<div class="attachments dmsf-parent-container"></div><div class="issue details"></div>'
        self.assertTrue(redmine_policy.issue_html_has_dmsf_section(page))
        self.assertTrue(redmine_policy.issue_html_has_dmsf_section(detached_page))
        self.assertFalse(redmine_policy.issue_html_has_dmsf_section(page_without_dms))
        self.assertFalse(redmine_policy.issue_html_has_dmsf_section(unrelated_dms))
        unavailable_opener = FakeOpener([page_without_dms.encode("utf-8")])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=unavailable_opener):
            unavailable_client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with self.assertRaises(redmine_client.RedmineError) as error:
                unavailable_client._issue_page_dmsf_links(10360, require_section=True)
        self.assertEqual(error.exception.code, "REDMINE_DMSF_ISSUE_ATTACH_UNAVAILABLE")
        opener = FakeOpener([json.dumps(issue_payload).encode(), page.encode(), json.dumps(metadata).encode(),
                             page.encode(), detached_page.encode()])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            issue = client.issue(10360)
            files, warnings = client.dmsf_file_inventory(issue)
            self.assertEqual(warnings, [])
            self.assertEqual([item["id"] for item in files], [4614])
            self.assertTrue(client.is_dmsf_attached(issue, 4614))
            self.assertFalse(client.is_dmsf_attached(issue, 4614))
        self.assertEqual(len(opener.requests), 5)

    def test_non_numeric_dmsf_journal_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(redmine_policy.RedminePolicyError, "REDMINE_DMSF_INVALID_ID"):
            redmine_policy.current_dmsf_file_ids([{"details": [
                {"property": "dmsf_file", "name": "not-an-id", "old_value": None, "new_value": "Name"}
            ]}])

    def test_dmsf_metadata_and_revision_download_are_validated(self) -> None:
        payload = {"dmsf_file": {
            "id": 3947, "project_id": 21, "title": "Quarterly plan", "filename": "plan.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "filesize": 4,
            "download_url": "/dmsf/files/3947/download", "revisions": [{"id": 6840, "version": "1"}, {"id": 6841, "version": "2"}],
        }}

        opener = FakeOpener([json.dumps(payload).encode(), b"DOCX"])
        opener.responses[1] = HeaderResponse(b"DOCX", {"Content-Type": "application/octet-stream", "Content-Length": "4"})
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            metadata = client.dmsf_file_metadata("3947")
            self.assertEqual(metadata["revision"]["id"], 6841)
            self.assertEqual(metadata["revision"]["version"], "2")
            self.assertEqual(metadata["revision"]["size"], 4)
            with tempfile.TemporaryDirectory() as directory:
                path, downloaded = client.download_dmsf_file(metadata, Path(directory), revision_id="6840")
                self.assertEqual(path.read_bytes(), b"DOCX")
                self.assertEqual(downloaded["revision"]["id"], 6840)
                self.assertEqual(downloaded["revision"]["version"], "1")
        self.assertIn("/dmsf/files/3947/view?download=6840", opener.requests[1][0].full_url)
        self.assertTrue(all(request.get_header("X-redmine-api-key") == "key" for request, _ in opener.requests))

    def test_nested_dmsf_revisions_use_first_revision_and_checked_urls(self) -> None:
        payload = {"dmsf_file": {
            "id": 3947, "project_id": 21, "name": "fallback.docx", "content_url": "/dmsf/files/3947/view",
            "dmsf_file_revisions": [
                {"id": 6841, "name": "plan.docx", "size": 4, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "version": "2", "content_url": "/dmsf/files/3947/view?download=6841"},
                {"id": 6840, "name": "plan-old.docx", "size": 3, "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "version": "1", "content_url": "/dmsf/files/3947/view?download=6840"},
            ],
        }}
        opener = FakeOpener([json.dumps(payload).encode(), HeaderResponse(b"OLD", {"Content-Length": "3"})])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org/rm", "key")
            metadata = client.dmsf_file_metadata(3947)
            self.assertEqual(metadata["name"], "plan.docx")
            self.assertEqual(metadata["size"], 4)
            self.assertEqual(metadata["mime_type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            self.assertEqual(metadata["revision"]["id"], 6841)
            self.assertEqual(metadata["revisions"][0]["id"], 6841)
            self.assertEqual(metadata["revisions"][1]["id"], 6840)
            self.assertEqual(metadata["download_url"], "https://redmine.example.org/dmsf/files/3947/view")
            self.assertEqual(metadata["revisions"][1]["download_url"], "https://redmine.example.org/dmsf/files/3947/view?download=6840")
            # Keep test artifacts outside the repository.  The default system
            # temp directory is cleaned up by TemporaryDirectory on exit.
            with tempfile.TemporaryDirectory() as directory:
                path, result = client.download_dmsf_file(metadata, Path(directory), revision_id=6840)
                self.assertEqual(path.read_bytes(), b"OLD")
                self.assertEqual(result["size"], 3)
                self.assertEqual(result["revision"], {"id": 6840, "version": "1"})
                self.assertEqual(result["download_url"], "https://redmine.example.org/dmsf/files/3947/view?download=6840")
        self.assertEqual(opener.requests[1][0].full_url, "https://redmine.example.org/dmsf/files/3947/view?download=6840")

    def test_nested_dmsf_url_security_is_checked_for_current_and_all_revisions(self) -> None:
        base_revision = {"id": 6841, "name": "plan.docx", "size": 4, "mime_type": "text/plain",
                         "version": "2", "content_url": "/dmsf/files/3947/view?download=6841"}
        for field, value in (
            ("content_url", "https://attacker.example/file"),
            ("content_url", "http://redmine.example.org/dmsf/files/3947/view"),
            ("content_url", "https://user:secret@redmine.example.org/dmsf/files/3947/view"),
        ):
            with self.subTest(field=field, value=value):
                record = {"id": 3947, "project_id": 21, "name": "plan.docx", "content_url": value,
                          "dmsf_file_revisions": [base_revision]}
                opener = FakeOpener([json.dumps({"dmsf_file": record}).encode()])
                with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
                    client = redmine_client.RedmineClient("https://redmine.example.org", "key")
                    with self.assertRaises(redmine_client.RedmineError) as error:
                        client.dmsf_file_metadata(3947)
                self.assertEqual(error.exception.code, "REDMINE_DMSF_UNSAFE_REDIRECT")
                self.assertEqual(len(opener.requests), 1)

        for value in ("https://attacker.example/file", "http://redmine.example.org/file", "https://user:secret@redmine.example.org/file"):
            with self.subTest(revision_url=value):
                revision = {**base_revision, "content_url": value}
                record = {"id": 3947, "project_id": 21, "name": "plan.docx", "content_url": "/dmsf/files/3947/view",
                          "dmsf_file_revisions": [revision]}
                opener = FakeOpener([json.dumps({"dmsf_file": record}).encode()])
                with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
                    client = redmine_client.RedmineClient("https://redmine.example.org", "key")
                    with self.assertRaises(redmine_client.RedmineError) as error:
                        client.dmsf_file_metadata(3947)
                self.assertEqual(error.exception.code, "REDMINE_DMSF_UNSAFE_REDIRECT")

        metadata = {"id": 3947, "name": "plan.docx", "size": 4, "download_url": "https://attacker.example/file",
                    "revisions": [], "revision": None}
        opener = FakeOpener([HeaderResponse(b"data", {"Content-Length": "4"})])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with self.assertRaises(redmine_client.RedmineError) as error:
                client.download_dmsf_file(metadata, Path.cwd())
        self.assertEqual(error.exception.code, "REDMINE_DMSF_UNSAFE_REDIRECT")
        self.assertEqual(opener.requests, [])

    def test_nested_dmsf_missing_and_invalid_sizes_have_distinct_errors(self) -> None:
        for value, expected_code in ((None, "REDMINE_DMSF_UNSUPPORTED_SCHEMA"), (-1, "REDMINE_DMSF_INVALID_METADATA"),
                                     ("4", "REDMINE_DMSF_INVALID_METADATA"), (True, "REDMINE_DMSF_INVALID_METADATA")):
            with self.subTest(size=value):
                revision = {"id": 6841, "name": "plan.docx", "mime_type": "text/plain", "version": "2",
                            "content_url": "/dmsf/files/3947/view?download=6841"}
                if value is not None:
                    revision["size"] = value
                record = {"id": 3947, "project_id": 21, "name": "plan.docx", "content_url": "/dmsf/files/3947/view",
                          "dmsf_file_revisions": [revision]}
                opener = FakeOpener([json.dumps({"dmsf_file": record}).encode()])
                with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
                    client = redmine_client.RedmineClient("https://redmine.example.org", "key")
                    with self.assertRaises(redmine_client.RedmineError) as error:
                        client.dmsf_file_metadata(3947)
                self.assertEqual(error.exception.code, expected_code)

    def test_unsupported_dmsf_schema_error_lists_logical_fields_without_payload(self) -> None:
        payload = {"dmsf_file": {"id": 3947, "project_id": 21, "filename": "secret-plan.docx", "filesize": 4,
                                 "payload_secret": "do-not-echo"}}
        opener = FakeOpener([json.dumps(payload).encode()])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with self.assertRaises(redmine_client.RedmineError) as error:
                client.dmsf_file_metadata(3947)
        self.assertEqual(error.exception.code, "REDMINE_DMSF_UNSUPPORTED_SCHEMA")
        self.assertIn("revisions", str(error.exception))
        self.assertIn("revision.id", str(error.exception))
        self.assertNotIn("secret-plan.docx", str(error.exception))
        self.assertNotIn("payload_secret", str(error.exception))

    def test_dmsf_html_response_and_cross_origin_download_url_are_rejected(self) -> None:
        payload = {"dmsf_file": {"id": 7, "project_id": 2, "filename": "note.txt", "filesize": 4,
                                 "revision": {"id": 11, "version": "1"},
                                 "download_url": "https://attacker.example/download"}}
        opener = FakeOpener([json.dumps(payload).encode()])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with self.assertRaisesRegex(redmine_client.RedmineError, "unsafe") as error:
                client.dmsf_file_metadata(7)
        self.assertEqual(error.exception.code, "REDMINE_DMSF_UNSAFE_REDIRECT")

        class HeaderResponse(Response):
            headers = {"Content-Type": "text/html"}

        metadata = {"id": 7, "name": "note.txt", "size": 4, "revisions": [], "revision": None}
        opener = FakeOpener([b"<html>login</html>"])
        opener.responses[0] = HeaderResponse(b"<html>login</html>")
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(redmine_client.RedmineError) as error:
                    client.download_dmsf_file(metadata, Path(directory))
                self.assertFalse(list(Path(directory).iterdir()))
        self.assertEqual(error.exception.code, "REDMINE_DMSF_HTML_RESPONSE")

    def test_dmsf_size_mismatch_removes_partial_file(self) -> None:
        metadata = {"id": 11, "name": "note.txt", "size": 5, "revisions": [], "revision": None}
        opener = FakeOpener([b"four"])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(redmine_client.RedmineError) as error:
                    client.download_dmsf_file(metadata, Path(directory))
                self.assertFalse(list(Path(directory).iterdir()))
        self.assertEqual(error.exception.code, "REDMINE_DMSF_DOWNLOAD_INTERRUPTED")

    def test_dmsf_content_length_mismatch_is_rejected_before_writing(self) -> None:
        metadata = {"id": 14, "name": "note.txt", "size": 5, "revisions": [], "revision": None}
        opener = FakeOpener([HeaderResponse(b"five", {"Content-Length": "4"})])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(redmine_client.RedmineError) as error:
                client.download_dmsf_file(metadata, Path(directory))
                self.assertFalse(list(Path(directory).iterdir()))
        self.assertEqual(error.exception.code, "REDMINE_DMSF_INVALID_METADATA")

    def test_dmsf_oversize_is_rejected_before_request(self) -> None:
        metadata = {"id": 12, "name": "large.docx", "size": redmine_client.ATTACHMENT_LIMIT + 1,
                    "revision": {"id": 15, "version": "1"}, "revisions": [{"id": 15, "version": "1"}]}
        opener = FakeOpener([])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(redmine_client.RedmineError) as error:
                client.download_dmsf_file(metadata, Path(directory))
        self.assertEqual(error.exception.code, "REDMINE_DMSF_SIZE_LIMIT")
        self.assertEqual(opener.requests, [])

    def test_interrupted_dmsf_stream_removes_partial_file(self) -> None:
        class InterruptedResponse(Response):
            def __init__(self):
                super().__init__(b"")
                self.read_count = 0

            def read(self, size=-1):
                self.read_count += 1
                if self.read_count == 1:
                    return b"part"
                raise OSError("connection reset")

        metadata = {"id": 13, "name": "note.txt", "size": 8,
                    "revision": {"id": 16, "version": "1"}, "revisions": [{"id": 16, "version": "1"}]}
        opener = FakeOpener([InterruptedResponse()])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(redmine_client.RedmineError) as error:
                    client.download_dmsf_file(metadata, Path(directory))
                self.assertFalse(list(Path(directory).iterdir()))
        self.assertEqual(error.exception.code, "REDMINE_DMSF_DOWNLOAD_INTERRUPTED")

    def test_dmsf_cross_origin_redirect_is_blocked(self) -> None:
        handler = redmine_client._SameOriginRedirectHandler(redmine_policy.url_origin("https://redmine.example.org"))
        request = redmine_client.urllib.request.Request("https://redmine.example.org/dmsf/files/7/download")
        with self.assertRaises(redmine_client.RedmineError) as error:
            handler.redirect_request(request, Response(b""), 302, "Found", {}, "https://attacker.example/file")
        self.assertEqual(error.exception.code, "REDMINE_DMSF_UNSAFE_REDIRECT")

    def test_dmsf_issue_update_redirect_is_blocked_even_on_same_origin(self) -> None:
        handler = redmine_client._SameOriginRedirectHandler(redmine_policy.url_origin("https://redmine.example.org"))
        request = redmine_client.urllib.request.Request(
            "https://redmine.example.org/issues/11993.json", data=b"{}", method="PUT"
        )
        with self.assertRaises(redmine_client.RedmineError) as error:
            handler.redirect_request(
                request, Response(b""), 302, "Found", {}, "https://redmine.example.org/login"
            )
        self.assertEqual(error.exception.code, "REDMINE_DMSF_UNSAFE_REDIRECT")

    def test_cross_origin_attachment_is_blocked_before_request(self) -> None:
        payload = {"issue": {"id": 7, "attachments": [{"id": 2, "filename": "note.txt", "content_url": "https://other.example/file"}]}}
        opener = FakeOpener([json.dumps(payload).encode()])
        with mock.patch.object(redmine_client.urllib.request, "build_opener", return_value=opener):
            client = redmine_client.RedmineClient("https://redmine.example.org", "key")
            issue = client.issue(7)
            with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(redmine_client.RedmineError, "another host"):
                client.download_attachments(issue, Path(directory))
        self.assertEqual(len(opener.requests), 1)

    def test_issue_id_and_filename_are_safe(self) -> None:
        for value in ("0", "-1", "1/2", "12.json", "99999999999"):
            with self.subTest(value=value), self.assertRaises(redmine_policy.RedminePolicyError):
                redmine_policy.issue_number(value)
        self.assertEqual(redmine_policy.safe_attachment_name("../../CON.txt", 3), "_CON-3.txt")
        self.assertEqual(redmine_credentials.target_name("https://redmine.example.org"), redmine_credentials.target_name("https://redmine.example.org/"))


class RedmineWorkflowTests(unittest.TestCase):
    def test_files_lists_standard_and_dmsf_without_downloading(self) -> None:
        class Client:
            recovery_warnings = []

            def issue(self, issue_id):
                return {"id": issue_id, "subject": "Issue", "description": "", "status": "New", "project": "ERP",
                        "attachments": [{"id": 3, "filename": "brief.docx", "filesize": 9, "content_type": "application/docx"}],
                        "journals": []}

            def current_dmsf_files(self, issue):
                return [{"id": 3947, "name": "policy.docx", "title": "Policy", "project_id": 21,
                         "size": 320905, "mime_type": "application/docx", "revision": {"id": 6841, "version": "1"}, "revisions": []}]

            def download_attachments(self, *args):
                raise AssertionError("files must not download standard attachments")

            def download_dmsf_file(self, *args, **kwargs):
                raise AssertionError("files must not download DMSF")

        with mock.patch.object(flow1c, "redmine_client_from_config", return_value=(Client(), "https://redmine.example.org", "credential-manager")) as client_factory, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(flow1c.cmd_redmine_files(argparse.Namespace(issue="17", json=True)), 0)
        client_factory.assert_called_once_with(recover=False)
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], "SELECTION_REQUIRED")
        self.assertEqual(result["standard_attachments"][0]["name"], "brief.docx")
        self.assertEqual(result["dms_files"][0]["id"], 3947)

    def test_fetch_without_dms_selection_never_downloads_dms(self) -> None:
        class Client:
            recovery_warnings = []

            def issue(self, issue_id):
                return {"id": issue_id, "subject": "Issue", "description": "", "status": "New", "project": "ERP",
                        "attachments": [], "journals": []}

            def current_dmsf_files(self, issue):
                return [{"id": 3947, "name": "policy.docx", "project_id": 21, "size": 4,
                         "mime_type": "application/docx", "revision": {"id": 6841, "version": "1"}, "revisions": [{"id": 6841, "version": "1"}]}]

            def download_dmsf_file(self, *args, **kwargs):
                raise AssertionError("DMSF must remain opt-in")

        with mock.patch.object(flow1c, "redmine_client_from_config", return_value=(Client(), "https://redmine.example.org", "credential-manager")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(flow1c.cmd_redmine_fetch(argparse.Namespace(issue="17", code=None, gate_id=None)), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], "SELECTION_REQUIRED")
        self.assertEqual(result["dms_files"][0]["id"], 3947)

    def test_selected_dms_file_rechecks_link_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "project-docs"
            docs.mkdir()
            (root / "config").mkdir()
            stages = Path(flow1c.__file__).resolve().parents[1] / "config" / "stages.json"
            (root / "config" / "stages.json").write_bytes(stages.read_bytes())
            (root / ".flow1c.local.json").write_text(json.dumps({"documentation_path": str(docs)}), encoding="utf-8")

            class Client:
                recovery_warnings = []
                rechecks = 0

                def issue(self, issue_id):
                    return {"id": issue_id, "subject": "Issue", "description": "", "status": "New", "project": "ERP",
                            "attachments": [{"id": 3}], "journals": []}

                def download_attachments(self, issue, destination):
                    path = destination / "standard.txt"
                    path.write_text("context", encoding="utf-8")
                    return [path], [{"id": 3, "name": path.name, "size": 7, "media_type": "text/plain"}]

                def current_dmsf_files(self, issue):
                    return [{"id": 3947, "name": "policy.txt", "title": "Policy", "project_id": 21,
                             "size": 12, "mime_type": "text/plain", "revision": {"id": 6841, "version": "1"},
                             "revisions": [{"id": 6841, "version": "1"}]}]

                def is_dmsf_attached(self, issue, file_id):
                    self.rechecks += 1
                    return file_id == 3947

                def download_dmsf_file(self, metadata, destination, **kwargs):
                    path = destination / "policy-3947.txt"
                    path.write_text("requirements", encoding="utf-8")
                    return path, {"id": 3947, "name": path.name, "size": 12, "mime_type": "text/plain",
                                  "revision": {"id": 6841, "version": "1"}}

            client = Client()
            with mock.patch.object(flow1c, "ROOT", root), mock.patch.object(
                flow1c, "redmine_client_from_config", return_value=(client, "https://redmine.example.org", "credential-manager")
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_fetch(argparse.Namespace(
                    issue="17", code=None, gate_id=None, dms_file=["3947"], dms_revision=None, all_dms=False,
                )), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(client.rechecks, 1)
            self.assertEqual(len(result["copied"]), 2)
            self.assertTrue(all(item["category"] == "redmine_attachments" for item in result["copied"]))
            dms_artifact = next(item for item in result["copied"] if item.get("source", {}).get("attachment_type") == "dmsf")
            provenance = dms_artifact["source"]
            self.assertEqual(provenance["attachment_type"], "dmsf")
            self.assertEqual(provenance["dmsf_file_id"], 3947)
            self.assertEqual(provenance["dmsf_revision_id"], 6841)
            self.assertEqual(provenance["dmsf_project_id"], 21)
            self.assertEqual(provenance["source_url"], "https://redmine.example.org/dmsf/files/3947/download")

    def test_unattached_dmsf_id_is_rejected_before_download(self) -> None:
        class Client:
            recovery_warnings = []

            def issue(self, issue_id):
                return {"id": issue_id, "subject": "Issue", "description": "", "status": "New", "project": "ERP",
                        "attachments": [], "journals": []}

            def current_dmsf_files(self, issue):
                return []

            def download_dmsf_file(self, *args, **kwargs):
                raise AssertionError("arbitrary DMSF IDs must not be downloaded")

        with mock.patch.object(flow1c, "redmine_client_from_config", return_value=(Client(), "https://redmine.example.org", "credential-manager")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(flow1c.cmd_redmine_fetch(argparse.Namespace(
                issue="17", code=None, gate_id=None, dms_file=["999"], dms_revision=None, all_dms=False,
            )), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["errors"][0]["code"], "REDMINE_DMSF_NOT_ATTACHED")

    def test_all_dms_preserves_successes_and_reports_per_file_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "project-docs"
            docs.mkdir()
            (root / "config").mkdir()
            stages = Path(flow1c.__file__).resolve().parents[1] / "config" / "stages.json"
            (root / "config" / "stages.json").write_bytes(stages.read_bytes())
            (root / ".flow1c.local.json").write_text(json.dumps({"documentation_path": str(docs)}), encoding="utf-8")

            class Client:
                recovery_warnings = []

                def issue(self, issue_id):
                    return {"id": issue_id, "subject": "Issue", "description": "", "status": "New", "project": "ERP",
                            "attachments": [], "journals": []}

                def dmsf_file_inventory(self, issue):
                    files = [
                        {"id": 3947, "name": "policy.txt", "project_id": 21, "size": 12, "mime_type": "text/plain",
                         "revision": {"id": 6841, "version": "1"}, "revisions": [{"id": 6841, "version": "1"}]},
                        {"id": 3919, "name": "bad.csv", "project_id": 21, "size": 3, "mime_type": "text/csv",
                         "revision": {"id": 6842, "version": "1"}, "revisions": [{"id": 6842, "version": "1"}]},
                    ]
                    return files, []

                def is_dmsf_attached(self, issue, file_id):
                    return True

                def download_dmsf_file(self, metadata, destination, **kwargs):
                    if metadata["id"] == 3919:
                        raise redmine_client.RedmineError("permission denied", code="REDMINE_DMSF_ACCESS_DENIED")
                    path = destination / "policy-3947.txt"
                    path.write_text("requirements", encoding="utf-8")
                    return path, {"id": 3947, "name": path.name, "size": 12, "mime_type": "text/plain",
                                  "revision": {"id": 6841, "version": "1"}}

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.object(
                flow1c, "redmine_client_from_config", return_value=(Client(), "https://redmine.example.org", "credential-manager")
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_fetch(argparse.Namespace(
                    issue="17", code=None, gate_id=None, dms_file=[], dms_revision=None, all_dms=True,
                )), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["state"], "IMPORTED_WITH_WARNINGS")
            self.assertEqual(len(result["copied"]), 1)
            self.assertEqual(result["warnings"][0]["code"], "REDMINE_DMSF_ACCESS_DENIED")
            self.assertEqual(result["warnings"][0]["dms_file_id"], 3919)

    def test_fetch_requires_gate_for_work_item_and_derives_target_from_it(self) -> None:
        class Client:
            recovery_warnings = []

            def issue(self, issue_id):
                return {"id": issue_id, "subject": "Issue", "description": "", "status": "New",
                        "project": "ERP", "attachments": []}

        with self.assertRaisesRegex(flow1c.WorkflowError, "requires an active gate_id"):
            flow1c.cmd_redmine_fetch(argparse.Namespace(issue="17", code="G-001", gate_id=None))

        gate = {"gate_id": "gate-1234", "work_reference": "G-001", "code": "G-001"}
        with mock.patch.object(flow1c, "load_gate", return_value=gate) as load_gate, \
                mock.patch.object(flow1c, "require_gate_tool") as require_tool, \
                mock.patch.object(flow1c, "load_manifest") as load_manifest, \
                mock.patch.object(flow1c, "redmine_client_from_config", return_value=(
                    Client(), "https://redmine.example.org", "environment"
                )), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(flow1c.cmd_redmine_fetch(
                argparse.Namespace(issue="17", code="G-001", gate_id="gate-1234")
            ), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "NO_ATTACHMENTS")
        load_gate.assert_called_once()
        require_tool.assert_called_once_with(gate, "flow1c_redmine_fetch")
        load_manifest.assert_called_once_with("G-001")

        with mock.patch.object(flow1c, "load_gate", return_value=gate), \
                mock.patch.object(flow1c, "require_gate_tool"):
            with self.assertRaisesRegex(flow1c.WorkflowError, "does not match"):
                flow1c.cmd_redmine_fetch(argparse.Namespace(
                    issue="17", code="G-002", gate_id="gate-1234"
                ))

    def test_issue_summary_has_bounded_untrusted_text_fields(self) -> None:
        issue = {"id": 3, "subject": "s" * 1000, "description": "d" * 20000, "status": "n" * 500, "project": "p" * 500}
        result = flow1c.redmine_issue_summary(issue, "https://redmine.example.org")
        self.assertEqual(len(result["subject"]), 500)
        self.assertTrue(result["subject_truncated"])
        self.assertEqual(len(result["description"]), 12000)
        self.assertTrue(result["description_truncated"])
        self.assertEqual(len(result["status"]), 200)
        self.assertEqual(result["url"], "https://redmine.example.org/issues/3")

    def test_configuration_never_writes_api_key_to_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".flow1c.local.json").write_text(json.dumps({"custom": True}), encoding="utf-8")
            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": "very-secret"}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), mock.patch.object(redmine_credentials, "write_api_key") as store, mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_configure(argparse.Namespace(url="https://redmine.example.org")), 0)
            config = json.loads((root / ".flow1c.local.json").read_text(encoding="utf-8"))
            self.assertEqual(config["custom"], True)
            self.assertEqual(config["redmine"], {"base_url": "https://redmine.example.org"})
            self.assertNotIn("very-secret", json.dumps(config))
            self.assertNotIn("very-secret", output.getvalue())
            store.assert_not_called()

    def test_first_and_same_url_configuration_are_atomic_and_preserve_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = write_local_config(root, {"custom": {"keep": True}})
            credentials: dict[str, str] = {}
            transaction_states = []
            real_write_json = flow1c.write_json

            def capture_transaction_state(path, value):
                if path.name == "redmine-configure-transaction.json":
                    transaction_states.append(value.copy())
                return real_write_json(path, value)

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), fake_credential_manager(credentials), mock.patch.object(
                flow1c, "write_json", side_effect=capture_transaction_state
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_configure(argparse.Namespace(url="https://redmine.example.org")), 0)
                first = json.loads(output.getvalue())
                self.assertEqual(first["state"], "CONNECTED")
                self.assertTrue(first["configured"] and first["verified"] and first["previous_connection_preserved"])
                self.assertEqual(first["warnings"], [])
                self.assertEqual(credentials, {"https://redmine.example.org": "credential-test-key"})
                # Updating the same URL replaces one entry and keeps unrelated config data.
                credentials["https://redmine.example.org"] = "older-key"
                output.seek(0)
                output.truncate()
                self.assertEqual(flow1c.cmd_redmine_configure(argparse.Namespace(url="https://redmine.example.org/")), 0)
            config = json.loads((root / ".flow1c.local.json").read_text(encoding="utf-8"))
            self.assertEqual(config["custom"], {"keep": True})
            self.assertEqual(config["redmine"], {"base_url": "https://redmine.example.org"})
            self.assertEqual(list(credentials), ["https://redmine.example.org"])
            self.assertEqual((root / ".flow1c.local.json").read_bytes() != raw, True)
            self.assertFalse((root / ".workspace" / "redmine-configure-transaction.json").exists())
            self.assertEqual([state["phase"] for state in transaction_states], ["prepared", "activated", "prepared", "activated"])
            self.assertTrue(all("credential-test-key" not in json.dumps(state) for state in transaction_states))
            self.assertNotIn("older-key", output.getvalue())

    def test_successful_url_change_activates_new_credential_then_removes_old(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"custom": 7, "redmine": {"base_url": "https://old.example.org"}})
            credentials = {"https://old.example.org": "old-secret"}
            events = []
            real_write_json = flow1c.write_json

            def track_delete(url, values):
                events.append(("delete", url))
                return values.pop(url, None) is not None

            def track_write(path, value):
                if path.name == ".flow1c.local.json":
                    events.append(("config", str(path)))
                return real_write_json(path, value)

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), fake_credential_manager(credentials, delete_hook=track_delete), mock.patch.object(
                flow1c, "write_json", side_effect=track_write
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_configure(argparse.Namespace(url="https://new.example.org")), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "CONNECTED")
            self.assertEqual(credentials, {"https://new.example.org": "credential-test-key"})
            self.assertLess(events.index(next(event for event in events if event[0] == "config")), events.index(("delete", "https://old.example.org")))
            config = json.loads((root / ".flow1c.local.json").read_text(encoding="utf-8"))
            self.assertEqual(config["custom"], 7)
            self.assertEqual(config["redmine"]["base_url"], "https://new.example.org")

    def test_validation_failure_preserves_old_config_credential_and_hides_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_local_config(root, {"redmine": {"base_url": "https://old.example.org"}, "other": [1]})
            credentials = {"https://old.example.org": "old-secret"}
            secret = "new-super-secret"
            output = io.StringIO()
            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": secret}), mock.patch.object(
                flow1c.RedmineClient, "current_user", side_effect=RuntimeError(secret)
            ), mock.patch("sys.stdout", output):
                self.assertEqual(flow1c.main(["redmine", "configure", "--url", "https://new.example.org"]), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "REDMINE_VALIDATION_FAILED")
            self.assertTrue(payload["previous_connection_preserved"])
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), original)
            self.assertEqual(credentials, {"https://old.example.org": "old-secret"})
            self.assertNotIn(secret, output.getvalue())

    def test_credential_write_failure_rolls_back_partial_new_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_local_config(root, {"redmine": {"base_url": "https://old.example.org"}})
            credentials = {"https://old.example.org": "old-secret"}

            def partial_write(url, key, values):
                values[url] = key
                raise RuntimeError(key)

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), fake_credential_manager(credentials, write_hook=partial_write), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.main(["redmine", "configure", "--url", "https://new.example.org"]), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "REDMINE_CREDENTIAL_WRITE_FAILED")
            self.assertTrue(payload["previous_connection_preserved"])
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), original)
            self.assertEqual(credentials, {"https://old.example.org": "old-secret"})
            self.assertNotIn("credential-test-key", output.getvalue())

    def test_credential_readback_mismatch_restores_previous_same_url_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_local_config(root, {"redmine": {"base_url": "https://same.example.org"}})
            credentials = {"https://same.example.org": "old-key"}

            def mismatch(url, key, values):
                return values.get(url) == "old-key"

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), fake_credential_manager(credentials, verify_hook=mismatch), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.main(["redmine", "configure", "--url", "https://same.example.org"]), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "REDMINE_CREDENTIAL_VERIFY_FAILED")
            self.assertEqual(credentials, {"https://same.example.org": "old-key"})
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), original)

    def test_config_write_failure_removes_new_key_and_keeps_old_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_local_config(root, {"redmine": {"base_url": "https://old.example.org"}, "opaque": "preserve"})
            credentials = {"https://old.example.org": "old-key"}
            real_write_json = flow1c.write_json

            def replace_then_fail(path, value):
                real_write_json(path, value)
                if path.name == ".flow1c.local.json":
                    raise OSError("disk failure after replacement")

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), fake_credential_manager(credentials), mock.patch.object(flow1c, "write_json", side_effect=replace_then_fail), mock.patch(
                "sys.stdout", new_callable=io.StringIO
            ) as output:
                self.assertEqual(flow1c.main(["redmine", "configure", "--url", "https://new.example.org"]), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "REDMINE_CONFIG_WRITE_FAILED")
            self.assertTrue(payload["previous_connection_preserved"])
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), original)
            self.assertEqual(credentials, {"https://old.example.org": "old-key"})

    def test_old_credential_cleanup_failure_keeps_new_connection_and_records_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"redmine": {"base_url": "https://old.example.org"}})
            credentials = {"https://old.example.org": "old-key"}
            attempts = 0

            def fail_old_delete(url, values):
                nonlocal attempts
                if url == "https://old.example.org":
                    attempts += 1
                    if attempts == 1:
                        raise RuntimeError("private detail")
                return values.pop(url, None) is not None

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), fake_credential_manager(credentials, delete_hook=fail_old_delete), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_configure(argparse.Namespace(url="https://new.example.org")), 0)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["state"], "CONNECTED_WITH_WARNING")
                self.assertTrue(payload["configured"] and payload["verified"])
                self.assertEqual(payload["warnings"][0]["code"], "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED")
                self.assertIn("redmine cleanup", payload["next_action"])
                pending_path = root / ".workspace" / "redmine-pending-cleanup.json"
                self.assertEqual(json.loads(pending_path.read_text(encoding="utf-8"))["urls"], ["https://old.example.org"])
                self.assertNotIn("private detail", output.getvalue())
                output.seek(0)
                output.truncate()
                self.assertEqual(flow1c.cmd_redmine_cleanup(argparse.Namespace(url=None)), 0)
                cleanup = json.loads(output.getvalue())
            self.assertEqual(cleanup["state"], "CLEANUP_COMPLETE")
            self.assertEqual(credentials, {"https://new.example.org": "credential-test-key"})
            self.assertFalse(pending_path.exists())

    def test_rollback_failure_reports_original_and_rollback_errors_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_local_config(root, {"redmine": {"base_url": "https://same.example.org"}})
            credentials = {"https://same.example.org": "old-private-key"}

            def fail_restore(url, key, values):
                if key == "old-private-key":
                    raise RuntimeError(key)
                values[url] = key

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), mock.patch.object(
                flow1c.RedmineClient, "current_user", return_value={"authenticated": True}
            ), fake_credential_manager(
                credentials, write_hook=fail_restore, verify_hook=lambda _url, _key, _values: False
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.main(["redmine", "configure", "--url", "https://same.example.org"]), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "REDMINE_ROLLBACK_FAILED")
            self.assertEqual([item["code"] for item in payload["errors"]], [
                "REDMINE_CREDENTIAL_VERIFY_FAILED", "REDMINE_ROLLBACK_FAILED"
            ])
            self.assertFalse(payload["previous_connection_preserved"])
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), original)
            self.assertNotIn("old-private-key", output.getvalue())

    def test_interrupted_unactivated_transaction_removes_partial_new_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"redmine": {"base_url": "https://old.example.org"}})
            credentials = {
                "https://old.example.org": "old-key",
                "https://new.example.org": "partial-new-key",
            }
            (root / ".workspace").mkdir()
            (root / ".workspace" / "redmine-configure-transaction.json").write_text(json.dumps({
                "schema_version": 1,
                "transaction_id": "a" * 32,
                "old_url": "https://old.example.org",
                "new_url": "https://new.example.org",
                "credential_managed": True,
                "credential_backup": False,
            }), encoding="utf-8")
            with mock.patch.object(flow1c, "ROOT", root), fake_credential_manager(credentials):
                warnings = flow1c.recover_redmine_transaction()
            self.assertEqual(warnings, [])
            self.assertEqual(credentials, {"https://old.example.org": "old-key"})
            self.assertFalse((root / ".workspace" / "redmine-configure-transaction.json").exists())

    def test_interrupted_activated_transaction_retries_old_key_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"redmine": {"base_url": "https://new.example.org"}})
            credentials = {
                "https://old.example.org": "old-key",
                "https://new.example.org": "new-key",
            }
            (root / ".workspace").mkdir()
            (root / ".workspace" / "redmine-configure-transaction.json").write_text(json.dumps({
                "schema_version": 1,
                "transaction_id": "b" * 32,
                "old_url": "https://old.example.org",
                "new_url": "https://new.example.org",
                "credential_managed": True,
                "credential_backup": False,
            }), encoding="utf-8")
            with mock.patch.object(flow1c, "ROOT", root), fake_credential_manager(credentials):
                warnings = flow1c.recover_redmine_transaction()
            self.assertEqual(warnings, [])
            self.assertEqual(credentials, {"https://new.example.org": "new-key"})
            self.assertFalse((root / ".workspace" / "redmine-configure-transaction.json").exists())

    def test_interrupted_unactivated_transaction_restores_preexisting_target_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"redmine": {"base_url": "https://old.example.org"}})
            credentials = {
                "https://old.example.org": "active-old-key",
                "https://new.example.org": "partially-replaced-key",
            }
            transaction_id = "c" * 32
            (root / ".workspace").mkdir()
            transaction_path = root / ".workspace" / "redmine-configure-transaction.json"
            transaction_path.write_text(json.dumps({
                "schema_version": 1,
                "transaction_id": transaction_id,
                "old_url": "https://old.example.org",
                "new_url": "https://new.example.org",
                "credential_managed": True,
                "credential_backup": True,
            }), encoding="utf-8")
            with mock.patch.object(flow1c, "ROOT", root), fake_credential_manager(credentials) as state:
                state[1][transaction_id] = "prior-new-target-key"
                self.assertEqual(flow1c.recover_redmine_transaction(), [])
            self.assertEqual(credentials, {
                "https://old.example.org": "active-old-key",
                "https://new.example.org": "prior-new-target-key",
            })
            self.assertFalse(transaction_path.exists())

    def test_interrupted_same_url_before_activation_restores_previous_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"redmine": {"base_url": "https://same.example.org"}})
            credentials = {"https://same.example.org": "partially-updated-key"}
            transaction_id = "d" * 32
            (root / ".workspace").mkdir()
            transaction_path = root / ".workspace" / "redmine-configure-transaction.json"
            transaction_path.write_text(json.dumps({
                "schema_version": 1,
                "transaction_id": transaction_id,
                "phase": "prepared",
                "old_url": "https://same.example.org",
                "new_url": "https://same.example.org",
                "credential_managed": True,
                "credential_backup": True,
            }), encoding="utf-8")
            with mock.patch.object(flow1c, "ROOT", root), fake_credential_manager(credentials) as state:
                state[1][transaction_id] = "previous-key"
                self.assertEqual(flow1c.recover_redmine_transaction(), [])
            self.assertEqual(credentials, {"https://same.example.org": "previous-key"})
            self.assertFalse(transaction_path.exists())

    def test_configure_does_not_overwrite_unresolved_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_local_config(root, {"redmine": {"base_url": "https://new.example.org"}})
            credentials = {
                "https://old.example.org": "old-key",
                "https://new.example.org": "new-key",
            }
            (root / ".workspace").mkdir()
            transaction_path = root / ".workspace" / "redmine-configure-transaction.json"
            transaction_path.write_text(json.dumps({
                "schema_version": 1,
                "transaction_id": "e" * 32,
                "phase": "activated",
                "old_url": "https://old.example.org",
                "new_url": "https://new.example.org",
                "credential_managed": True,
                "credential_backup": False,
            }), encoding="utf-8")

            def fail_old_delete(url, values):
                if url == "https://old.example.org":
                    raise RuntimeError("unavailable")
                return values.pop(url, None) is not None

            output = io.StringIO()
            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": "newer-key"}), fake_credential_manager(
                credentials, delete_hook=fail_old_delete
            ), mock.patch.object(flow1c, "save_redmine_pending_cleanup_urls", side_effect=OSError("read-only")), mock.patch(
                "sys.stdout", output
            ):
                self.assertEqual(flow1c.main(["redmine", "configure", "--url", "https://third.example.org"]), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "REDMINE_ROLLBACK_FAILED")
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), original)
            self.assertTrue(transaction_path.exists())
            self.assertEqual(credentials, {
                "https://old.example.org": "old-key",
                "https://new.example.org": "new-key",
            })

    def test_disconnect_is_idempotent_and_environment_key_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"redmine": {"base_url": "https://redmine.example.org"}, "other": 1})
            credentials = {"https://redmine.example.org": "saved-key"}
            events = []
            real_write_json = flow1c.write_json

            def track_delete(url, values):
                events.append(("delete", url))
                return values.pop(url, None) is not None

            def track_write(path, value):
                if path.name == ".flow1c.local.json":
                    events.append(("config", str(path)))
                return real_write_json(path, value)

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict(
                "os.environ", {"FLOW1C_REDMINE_API_KEY": "environment-secret"}
            ), fake_credential_manager(credentials, delete_hook=track_delete), mock.patch.object(
                flow1c, "write_json", side_effect=track_write
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_disconnect(argparse.Namespace()), 0)
                first = json.loads(output.getvalue())
                self.assertEqual(first["state"], "DISCONNECTED_WITH_WARNING")
                self.assertFalse(first["environment_key_cleared"])
                self.assertIn("environment-secret", __import__("os").environ["FLOW1C_REDMINE_API_KEY"])
                self.assertEqual(credentials, {})
                output.seek(0)
                output.truncate()
                self.assertEqual(flow1c.cmd_redmine_disconnect(argparse.Namespace()), 0)
            self.assertLess(events.index(next(event for event in events if event[0] == "config")), events.index(("delete", "https://redmine.example.org")))
            self.assertEqual(json.loads(output.getvalue())["state"], "DISCONNECTED_WITH_WARNING")
            config = json.loads((root / ".flow1c.local.json").read_text(encoding="utf-8"))
            self.assertEqual(config, {"other": 1})

    def test_disconnect_cleanup_failure_is_retryable_and_config_is_already_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_local_config(root, {"redmine": {"base_url": "https://redmine.example.org"}})
            credentials = {"https://redmine.example.org": "saved-key"}
            attempts = 0

            def fail_once(url, values):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("private detail")
                return values.pop(url, None) is not None

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.dict("os.environ", {"FLOW1C_REDMINE_API_KEY": ""}), fake_credential_manager(
                credentials, delete_hook=fail_once
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_disconnect(argparse.Namespace()), 0)
                first = json.loads(output.getvalue())
                self.assertEqual(first["state"], "DISCONNECTED_WITH_WARNING")
                self.assertFalse(first["configured"])
                self.assertFalse(flow1c.redmine_settings())
                output.seek(0)
                output.truncate()
                self.assertEqual(flow1c.cmd_redmine_disconnect(argparse.Namespace()), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "DISCONNECTED")
            self.assertEqual(credentials, {})
            self.assertFalse((root / ".workspace" / "redmine-pending-cleanup.json").exists())

    def test_disconnect_config_write_failure_keeps_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = write_local_config(root, {"redmine": {"base_url": "https://redmine.example.org"}, "other": True})
            credentials = {"https://redmine.example.org": "saved-key"}
            real_write_json = flow1c.write_json

            def replace_then_fail(path, value):
                real_write_json(path, value)
                if path.name == ".flow1c.local.json":
                    raise OSError("disk failure after replacement")

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.object(flow1c, "write_json", side_effect=replace_then_fail), fake_credential_manager(
                credentials
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.main(["redmine", "disconnect"]), 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["code"], "REDMINE_CONFIG_WRITE_FAILED")
            self.assertEqual((root / ".flow1c.local.json").read_bytes(), original)
            self.assertEqual(credentials, {"https://redmine.example.org": "saved-key"})

    def test_legacy_intake_keeps_folder_single_duplicate_unsupported_and_absence_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            docs = root / "project-docs"
            source_folder = Path(directory) / "source-folder"
            root.mkdir()
            docs.mkdir()
            source_folder.mkdir()
            (root / "config").mkdir()
            stages = Path(flow1c.__file__).resolve().parents[1] / "config" / "stages.json"
            (root / "config" / "stages.json").write_bytes(stages.read_bytes())
            (source_folder / "accepted.txt").write_text("same content", encoding="utf-8")
            (source_folder / "blocked.exe").write_bytes(b"blocked")
            duplicate = Path(directory) / "duplicate.txt"
            duplicate.write_text("same content", encoding="utf-8")
            files, skipped = flow1c.enumerate_intake_files([str(source_folder), str(duplicate)])
            index = {"artifacts": [], "confirmed_absent": ["correspondence"]}
            with mock.patch.object(flow1c, "ROOT", root), mock.patch.object(flow1c, "project_root", return_value=docs):
                result = flow1c.persist_intake_files(
                    files, skipped, code=None, category="meeting_materials", received_via="folder", index=index
                )
            self.assertEqual(result["state"], "ACCEPTED")
            self.assertEqual(result["code"], None)
            self.assertEqual(len(result["copied"]), 1)
            self.assertEqual(result["confirmed_absent"], ["correspondence"])
            self.assertIn(str((source_folder / "blocked.exe").resolve()), result["skipped"])
            self.assertIn("duplicate.txt", result["skipped"])
            self.assertEqual(set(result), {"state", "intake_id", "code", "destination", "copied", "skipped", "confirmed_absent"})
            manifest = json.loads((Path(result["destination"]).parent / "intake.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["confirmed_absent"], ["correspondence"])
            self.assertTrue((Path(result["destination"]) / "accepted.txt").is_file())
            self.assertFalse((Path(result["destination"]) / "blocked.exe").exists())

    def test_legacy_intake_imports_single_file_into_existing_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            docs = root / "project-docs"
            item = docs / "work-items" / "G-123"
            item.mkdir(parents=True)
            (root / "config").mkdir(parents=True)
            stages = Path(flow1c.__file__).resolve().parents[1] / "config" / "stages.json"
            (root / "config" / "stages.json").write_bytes(stages.read_bytes())
            source = Path(directory) / "single.csv"
            source.write_text("id;value\n1;ok\n", encoding="utf-8")
            files, skipped = flow1c.enumerate_intake_files([str(source)])
            index = {"artifacts": [], "confirmed_absent": []}
            index_path = item / "input" / "artifacts.json"
            with mock.patch.object(flow1c, "ROOT", root), mock.patch.object(flow1c, "project_root", return_value=docs), mock.patch.object(
                flow1c, "work_item_root", return_value=item
            ), mock.patch.object(flow1c, "artifact_index_path", return_value=index_path), mock.patch.object(
                flow1c, "load_artifact_index", return_value=index
            ):
                result = flow1c.persist_intake_files(
                    files, skipped, code="G-123", category="correspondence", received_via="file", index=index
                )
            self.assertEqual(result["code"], "G-123")
            self.assertEqual(result["skipped"], [])
            self.assertEqual(len(result["copied"]), 1)
            saved_index = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_index["artifacts"][0]["sha256"], flow1c.sha256(source))
            stored_path = docs / result["copied"][0]["relative_path"]
            self.assertEqual(stored_path.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))

    def test_windows_credential_adapter_roundtrips_without_exposing_the_secret(self) -> None:
        class Function:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        class Api:
            pass

        api = Api()
        class FileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class Credential(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                ("LastWritten", FileTime), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.c_void_p), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        def write(pointer, _prompt):
            credential = ctypes.cast(pointer, ctypes.POINTER(Credential)).contents
            api.target = credential.TargetName
            api.secret = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-16-le")
            api.blob = ctypes.create_string_buffer(api.secret.encode("utf-16-le"))
            saved = Credential()
            saved.TargetName = credential.TargetName
            saved.CredentialBlobSize = len(api.secret.encode("utf-16-le"))
            saved.CredentialBlob = ctypes.cast(api.blob, ctypes.c_void_p)
            api.saved_pointer = ctypes.pointer(saved)
            return 1

        def read(_target, _type, _flags, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(Credential)))[0] = api.saved_pointer
            return 1

        api.CredWriteW = Function(write)
        api.CredReadW = Function(read)
        api.CredFree = Function(lambda _pointer: None)

        def credential_api():
            return api, Credential

        with mock.patch.object(redmine_credentials, "_api", side_effect=credential_api), mock.patch.object(redmine_credentials.sys, "platform", "win32"):
            redmine_credentials.write_api_key("https://redmine.example.org", "secret-value")
            self.assertEqual(redmine_credentials.read_api_key("https://redmine.example.org"), "secret-value")
        self.assertNotIn("secret-value", api.target)
        self.assertEqual(api.secret, "secret-value")

    def test_fetch_imports_supported_file_into_inbox_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "project-docs"
            docs.mkdir()
            (root / "config").mkdir()
            (root / "config/stages.json").write_bytes((Path(flow1c.__file__).resolve().parents[1] / "config" / "stages.json").read_bytes())
            (root / ".flow1c.local.json").write_text(json.dumps({"documentation_path": str(docs)}), encoding="utf-8")

            class Client:
                def issue(self, issue_id):
                    return {"id": issue_id, "subject": "Sample issue", "description": "Issue details", "status": "New", "project": "ERP", "attachments": [{"id": 3}]}

                def download_attachments(self, issue, destination):
                    (destination / "brief.txt").write_text("requirements", encoding="utf-8")
                    (destination / "tool.exe").write_bytes(b"blocked")
                    return [], [{"id": 3, "name": "brief.txt", "size": 12}]

            with mock.patch.object(flow1c, "ROOT", root), mock.patch.object(
                flow1c, "redmine_client_from_config", return_value=(Client(), "https://redmine.example.org", "environment")
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(flow1c.cmd_redmine_fetch(argparse.Namespace(issue="17", code=None)), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["issue"]["id"], 17)
            self.assertEqual(result["issue"]["description"], "Issue details")
            self.assertEqual(result["state"], "PARTIAL")
            self.assertEqual(result["skipped_unsupported"], ["tool.exe"])
            manifest_path = Path(result["destination"]).parent / "intake.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"]["system"], "redmine")
            self.assertEqual(manifest["source"]["issue_id"], 17)
            self.assertEqual(manifest["artifacts"][0]["received_via"], "redmine")
            self.assertEqual((Path(result["destination"]) / "brief.txt").read_text(encoding="utf-8"), "requirements")
            self.assertFalse((Path(result["destination"]) / "tool.exe").exists())


if __name__ == "__main__":
    unittest.main()
