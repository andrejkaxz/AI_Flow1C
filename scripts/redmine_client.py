"""Redmine issue, attachment, and explicitly confirmed DMSF API client."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

try:
    from redmine_policy import (RedminePolicyError, current_dmsf_file_ids, dmsf_identifier, issue_number, relation_label,
                                issue_html_dmsf_file_ids, issue_html_has_dmsf_section, normalize_base_url,
                                safe_attachment_name, url_origin)
except ModuleNotFoundError:
    from scripts.redmine_policy import (RedminePolicyError, current_dmsf_file_ids, dmsf_identifier, issue_number, relation_label,
                                        issue_html_dmsf_file_ids, issue_html_has_dmsf_section, normalize_base_url,
                                        safe_attachment_name, url_origin)


ISSUE_RESPONSE_LIMIT = 5 * 1024 * 1024
ATTACHMENT_LIMIT = 500 * 1024 * 1024
ATTACHMENT_COUNT_LIMIT = 200
RELATION_COUNT_LIMIT = 200
DMSF_ALLOWED_EXTENSIONS = {".docx", ".xlsx", ".pdf", ".txt", ".md", ".csv", ".xml", ".png", ".jpg", ".jpeg"}
DMSF_UPLOAD_RESPONSE_LIMIT = 1024 * 1024


def _remote_text(value: Any, limit: int = 500) -> str:
    raw = str(value or "")
    return "".join(char if char.isprintable() else "�" for char in raw)[:limit]


class RedmineError(RuntimeError):
    """Actionable Redmine configuration, API, or response error."""

    def __init__(self, message: str, *, code: str = "REDMINE_ERROR"):
        super().__init__(message)
        self.code = code


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: tuple[str, str, int]):
        super().__init__()
        self.origin = origin

    def redirect_request(self, req: urllib.request.Request, fp: BinaryIO, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        if req.get_method().upper() != "GET":
            raise RedmineError(
                "Redmine redirected a write request; the upload was blocked.",
                code="REDMINE_DMSF_UNSAFE_REDIRECT",
            )
        try:
            destination_origin = url_origin(newurl)
        except RedminePolicyError as exc:
            raise RedmineError("Redmine returned a malformed redirect; download was blocked.") from exc
        parsed = urllib.parse.urlsplit(newurl)
        if destination_origin != self.origin or parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
            raise RedmineError("Redmine redirected a request to an unsafe URL; no data was sent there.", code="REDMINE_DMSF_UNSAFE_REDIRECT")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RedmineClient:
    """Read Redmine data and perform only explicitly confirmed DMSF uploads."""

    def __init__(self, base_url: str, api_key: str, *, timeout: int = 30):
        try:
            self.base_url = normalize_base_url(base_url)
        except RedminePolicyError as exc:
            raise RedmineError(str(exc)) from exc
        self.api_key = str(api_key or "").strip()
        if not self.api_key or "\r" in self.api_key or "\n" in self.api_key:
            raise RedmineError("Redmine API key is missing or invalid.")
        self.timeout = timeout
        self._origin = url_origin(self.base_url)
        self._opener = urllib.request.build_opener(_SameOriginRedirectHandler(self._origin))

    def _open(self, url: str, *, accept: str = "application/json") -> Any:
        try:
            parsed = urllib.parse.urlsplit(url)
            origin = url_origin(url)
        except (RedminePolicyError, ValueError) as exc:
            raise RedmineError("Redmine returned a malformed URL; request was blocked.", code="REDMINE_DMSF_UNSAFE_REDIRECT") from exc
        if origin != self._origin or parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
            raise RedmineError("Redmine URL is not same-origin HTTPS or contains embedded credentials; request was blocked.", code="REDMINE_DMSF_UNSAFE_REDIRECT")
        request = urllib.request.Request(url, headers={
            "Accept": accept,
            "X-Redmine-API-Key": self.api_key,
            "User-Agent": "FLOW1C-Workflow/1.0 (read-only Redmine integration)",
        })
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if "/dmsf/" in urllib.parse.urlsplit(url).path:
                if exc.code in {401, 403}:
                    raise RedmineError("Redmine denied access to this DMSF document.", code="REDMINE_DMSF_ACCESS_DENIED") from None
                if exc.code == 404:
                    raise RedmineError("DMSF endpoint or document was not found; the plugin may be unavailable.", code="REDMINE_DMSF_NOT_FOUND") from None
                if exc.code in {400, 405, 501}:
                    raise RedmineError("The Redmine DMSF API is unavailable or does not support this request.", code="REDMINE_DMSF_UNAVAILABLE") from None
            if exc.code in {401, 403}:
                raise RedmineError("Redmine rejected the API key or this user cannot access the requested resource.") from None
            if exc.code == 404:
                raise RedmineError("Redmine issue was not found or is not visible to this API user.") from None
            if exc.code == 429:
                raise RedmineError("Redmine rate limit reached; wait briefly and retry.") from None
            raise RedmineError(f"Redmine returned HTTP {exc.code}; check server availability and permissions.") from None
        except RedmineError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RedmineError(f"Could not reach Redmine: {getattr(exc, 'reason', exc)}") from None

    def current_user(self) -> dict[str, Any]:
        url = self.base_url + "/users/current.json"
        response = self._open(url)
        try:
            try:
                raw = response.read(ISSUE_RESPONSE_LIMIT + 1)
            except (TimeoutError, OSError) as exc:
                raise RedmineError(f"Redmine response read failed: {exc}") from None
        finally:
            response.close()
        if len(raw) > ISSUE_RESPONSE_LIMIT:
            raise RedmineError("Redmine current-user response exceeded the safety limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RedmineError("Redmine returned invalid JSON for the current user.") from exc
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            raise RedmineError("Redmine current-user response is missing the user identity.")
        return {"authenticated": True}

    def issue(self, number: str | int) -> dict[str, Any]:
        try:
            issue_id = issue_number(number)
        except RedminePolicyError as exc:
            raise RedmineError(str(exc)) from exc
        query = urllib.parse.urlencode({"include": "attachments,journals"})
        url = f"{self.base_url}/issues/{issue_id}.json?{query}"
        response = self._open(url)
        try:
            try:
                raw = response.read(ISSUE_RESPONSE_LIMIT + 1)
            except (TimeoutError, OSError) as exc:
                raise RedmineError(f"Redmine response read failed: {exc}") from None
        finally:
            response.close()
        if len(raw) > ISSUE_RESPONSE_LIMIT:
            raise RedmineError("Redmine issue response exceeded the safety limit.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RedmineError("Redmine returned invalid JSON for the issue.") from exc
        issue = payload.get("issue") if isinstance(payload, dict) else None
        if not isinstance(issue, dict) or issue.get("id") != issue_id:
            raise RedmineError("Redmine issue response did not match the requested issue number.")
        attachments = issue.get("attachments", [])
        if not isinstance(attachments, list):
            raise RedmineError("Redmine issue has an invalid attachment list.")
        if len(attachments) > ATTACHMENT_COUNT_LIMIT:
            raise RedmineError(f"Issue has more than {ATTACHMENT_COUNT_LIMIT} attachments; split the intake manually.")
        journals = issue.get("journals", [])
        if journals is not None and not isinstance(journals, list):
            raise RedmineError("Redmine issue has an invalid journal list.")
        status = issue.get("status") if isinstance(issue.get("status"), dict) else {}
        project = issue.get("project") if isinstance(issue.get("project"), dict) else {}
        project_id: int | None = None
        try:
            if project.get("id") is not None:
                project_id = dmsf_identifier(project.get("id"), label="Redmine project ID")
        except RedminePolicyError as exc:
            raise RedmineError(str(exc), code="REDMINE_DMSF_INVALID_METADATA") from exc
        return {
            "id": issue_id,
            "subject": str(issue.get("subject", "")),
            "description": str(issue.get("description", "")),
            "status": str(status.get("name", "")),
            "project": str(project.get("name", "")),
            "project_id": project_id,
            "attachments": attachments,
            "journals": journals or [],
        }

    def _write(self, url: str, data: Any, headers: dict[str, str], *, operation: str,
               method: str = "POST") -> bytes:
        """Write to a validated same-origin URL without exposing response bodies."""
        try:
            parsed = urllib.parse.urlsplit(url)
            origin = url_origin(url)
        except (RedminePolicyError, ValueError) as exc:
            raise RedmineError(
                "Redmine write URL is malformed; the request was blocked.",
                code="REDMINE_DMSF_UNSAFE_REDIRECT",
            ) from exc
        if origin != self._origin or parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
            raise RedmineError(
                "Redmine write URL is not same-origin HTTPS or contains embedded credentials; the request was blocked.",
                code="REDMINE_DMSF_UNSAFE_REDIRECT",
            )
        request_headers = {
            "Accept": "application/json",
            "X-Redmine-API-Key": self.api_key,
            "User-Agent": "FLOW1C-Workflow/1.0 (confirmed Redmine DMSF upload)",
            **headers,
        }
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        uncertain_code = "REDMINE_DMSF_COMMIT_UNCERTAIN" if operation == "attach" else "REDMINE_DMSF_UPLOAD_FAILED"
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                code = "REDMINE_DMSF_UPLOAD_ACCESS_DENIED"
                message = "Redmine denied the DMSF upload or issue attachment; check API permissions."
            elif exc.code == 413:
                code = "REDMINE_DMSF_UPLOAD_SIZE_LIMIT"
                message = "Redmine rejected the file because it exceeds the server upload limit."
            elif exc.code in {404, 405, 501}:
                code = "REDMINE_DMSF_UNAVAILABLE"
                message = "The installed Redmine DMSF API does not provide this operation."
            elif operation == "attach" and exc.code >= 500:
                code = uncertain_code
                message = "The DMSF issue-attachment response was inconclusive; do not retry automatically."
            else:
                code = "REDMINE_DMSF_COMMIT_FAILED" if operation == "attach" else "REDMINE_DMSF_UPLOAD_FAILED"
                message = f"Redmine rejected the DMSF {operation} request with HTTP {exc.code}."
            raise RedmineError(message, code=code) from None
        except RedmineError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise RedmineError(
                f"The DMSF {operation} request did not complete: {_remote_text(reason, 200)}",
                code=uncertain_code,
            ) from None
        try:
            raw = response.read(DMSF_UPLOAD_RESPONSE_LIMIT + 1)
        except (TimeoutError, OSError) as exc:
            code = uncertain_code
            raise RedmineError(
                f"The DMSF {operation} response could not be read: {_remote_text(exc, 200)}",
                code=code,
            ) from None
        finally:
            response.close()
        if len(raw) > DMSF_UPLOAD_RESPONSE_LIMIT:
            raise RedmineError("The DMSF response exceeded the safety limit.", code=uncertain_code)
        return raw

    def _post(self, url: str, data: Any, headers: dict[str, str], *, operation: str) -> bytes:
        """Backward-compatible POST wrapper for DMSF upload operations."""
        return self._write(url, data, headers, operation=operation)

    def _dmsf_available(self, project_id: int) -> None:
        """Probe the project DMSF endpoint before any mutating request."""
        url = f"{self.base_url}/projects/{project_id}/dmsf.json"
        try:
            response = self._open(url)
            try:
                response.read(DMSF_UPLOAD_RESPONSE_LIMIT + 1)
            finally:
                response.close()
        except RedmineError as exc:
            if exc.code in {"REDMINE_DMSF_ACCESS_DENIED", "REDMINE_ACCESS_DENIED"}:
                raise RedmineError(str(exc), code="REDMINE_DMSF_UPLOAD_ACCESS_DENIED") from None
            raise RedmineError("DMSF is not available for the issue project.", code="REDMINE_DMSF_UNAVAILABLE") from None

    def upload_dmsf_file(self, issue: dict[str, Any], local_path: str | Path, *, confirmed: bool) -> dict[str, Any]:
        """Upload one local file and attach it to the supplied issue after explicit confirmation."""
        try:
            issue_id = dmsf_identifier(issue.get("id"), label="Redmine issue ID")
        except RedminePolicyError as exc:
            raise RedmineError(str(exc), code="REDMINE_DMSF_UPLOAD_FAILED") from exc
        try:
            project_id = dmsf_identifier(issue.get("project_id"), label="Redmine project ID")
        except RedminePolicyError as exc:
            raise RedmineError(str(exc), code="REDMINE_DMSF_UNAVAILABLE") from exc
        requested = Path(local_path)
        try:
            absolute = requested.resolve(strict=True)
            if requested.is_symlink() or not absolute.is_file():
                raise RedmineError("The upload path must identify an ordinary local file.", code="REDMINE_DMSF_UPLOAD_FILE_NOT_FOUND")
            stat = absolute.stat()
        except FileNotFoundError:
            raise RedmineError("The selected local file does not exist.", code="REDMINE_DMSF_UPLOAD_FILE_NOT_FOUND") from None
        except PermissionError:
            raise RedmineError("The selected local file cannot be read.", code="REDMINE_DMSF_UPLOAD_ACCESS_DENIED") from None
        except OSError as exc:
            raise RedmineError(f"The selected local file cannot be inspected: {_remote_text(exc, 200)}", code="REDMINE_DMSF_UPLOAD_FILE_NOT_FOUND") from None
        filename = absolute.name
        extension = absolute.suffix.casefold()
        if extension not in DMSF_ALLOWED_EXTENSIONS:
            raise RedmineError("This file type is not allowed by the DMSF upload policy.", code="REDMINE_DMSF_UPLOAD_UNSUPPORTED_TYPE")
        if stat.st_size > ATTACHMENT_LIMIT:
            raise RedmineError("The selected file exceeds the 500 MiB DMSF upload limit.", code="REDMINE_DMSF_UPLOAD_SIZE_LIMIT")
        if any(ord(char) < 32 for char in filename) or not filename.strip(" ."):
            raise RedmineError("The selected file name is invalid for DMSF.", code="REDMINE_DMSF_UPLOAD_UNSUPPORTED_TYPE")

        self._dmsf_available(project_id)
        # An issue with no DMSF documents may not render a DMS container at all.
        # The inventory read below still verifies that the issue page is genuine
        # and accessible, while project availability proves that DMSF is enabled.
        try:
            current_files, warnings = self.dmsf_file_inventory(issue)
        except RedmineError:
            raise
        if warnings:
            first = warnings[0]
            raise RedmineError(
                "DMSF file inventory could not be verified before upload.",
                code=str(first.get("code", "REDMINE_DMSF_UNAVAILABLE")),
            )
        if any(str(item.get("name", "")).casefold() == filename.casefold() for item in current_files):
            raise RedmineError(
                "A DMSF document with this name is already attached; explicit revision handling is required.",
                code="REDMINE_DMSF_UPLOAD_NAME_CONFLICT",
            )
        preflight = {"issue_id": issue_id, "project_id": project_id, "path": str(absolute), "name": filename, "size": stat.st_size}
        if not confirmed:
            return {"state": "NEEDS_CONFIRMATION", "preflight": preflight}

        upload_url = f"{self.base_url}/projects/{project_id}/dmsf/upload.json?{urllib.parse.urlencode({'filename': filename})}"
        try:
            with absolute.open("rb") as stream:
                opened_stat = Path(stream.name).stat()
                if opened_stat.st_size != stat.st_size:
                    raise RedmineError(
                        "The selected local file changed during preflight; no upload was attempted.",
                        code="REDMINE_DMSF_UPLOAD_FAILED",
                    )
                upload_raw = self._post(
                    upload_url,
                    stream,
                    {"Content-Type": "application/octet-stream", "Content-Length": str(stat.st_size)},
                    operation="upload",
                )
        except PermissionError:
            raise RedmineError("The selected local file cannot be opened for upload.", code="REDMINE_DMSF_UPLOAD_ACCESS_DENIED") from None
        try:
            upload_payload = json.loads(upload_raw.decode("utf-8"))
            token = upload_payload.get("upload", {}).get("token") if isinstance(upload_payload, dict) else None
        except (UnicodeError, json.JSONDecodeError, AttributeError):
            token = None
        if not isinstance(token, str) or not token or len(token) > 256 or any(ord(char) < 32 for char in token):
            raise RedmineError("Redmine returned an invalid DMSF upload token.", code="REDMINE_DMSF_UPLOAD_FAILED")

        # DMSF's /commit endpoint creates a project document only; it does not
        # consume issue_id. Issue attachments use the plugin's IssuesController
        # hook, which consumes the temporary Attachment token together with
        # dmsf_attachments and committed_files on an issue update.
        issue_update_url = f"{self.base_url}/issues/{issue_id}.json"
        attachment_key = "1"
        issue_update_payload = {
            "issue": {},
            "dmsf_attachments_upload_choice": "DMSF",
            "dmsf_attachments": {attachment_key: {"filename": filename, "token": token}},
            "committed_files": {attachment_key: {
                "name": filename,
                "title": Path(filename).stem,
                "description": "",
                "comment": "",
                "version_major": "0",
                "version_minor": "1",
                "version_patch": None,
                "custom_field_values": {},
            }},
        }
        try:
            self._write(
                issue_update_url,
                json.dumps(issue_update_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                {"Content-Type": "application/json"},
                operation="attach",
                method="PUT",
            )
        except RedmineError as exc:
            if exc.code == "REDMINE_DMSF_COMMIT_UNCERTAIN":
                return {
                    "state": "COMMITTED_UNVERIFIED", "preflight": preflight,
                    "errors": [{"code": exc.code, "message": str(exc)}],
                    "next_action": "Inspect Redmine and DMSF manually; do not retry automatically.",
                }
            raise
        verification = {"issue_reloaded": False, "dmsf_attached": False}
        try:
            fresh_issue = self.issue(issue_id)
            verification["issue_reloaded"] = True
            journal_links = current_dmsf_file_ids(fresh_issue.get("journals", []))
            page_links = self._issue_page_dmsf_links(issue_id)
            attached = list({item["id"]: item for item in [*journal_links, *page_links]}.values())
            previous_ids = {int(item["id"]) for item in current_files if isinstance(item.get("id"), int)}
            new_links = [item for item in attached if item["id"] not in previous_ids]
            expected_names = {filename.casefold(), Path(filename).stem.casefold()}
            named = [item for item in new_links if str(item.get("name", "")).casefold() in expected_names]
            committed = named[0] if len(named) == 1 else (new_links[0] if len(new_links) == 1 else None)
        except (RedmineError, RedminePolicyError) as exc:
            return {"state": "COMMITTED_UNVERIFIED", "preflight": preflight,
                    "errors": [{"code": "REDMINE_DMSF_UPLOAD_VERIFICATION_FAILED", "message": str(exc)}],
                    "next_action": "Inspect the issue and DMSF manually; do not retry automatically."}
        if committed is None:
            return {"state": "COMMITTED_UNVERIFIED", "preflight": preflight,
                    "errors": [{"code": "REDMINE_DMSF_UPLOAD_VERIFICATION_FAILED", "message": "The issue reload did not show the committed DMSF file."}],
                    "next_action": "Inspect the issue and DMSF manually; do not retry automatically."}
        file_id = committed["id"]
        verification["dmsf_attached"] = True
        return {"state": "UPLOADED", "preflight": preflight,
                "dms_file": {"id": file_id, "name": filename}, "verification": verification}

    def revise_dmsf_file(
        self, issue: dict[str, Any], file_id: int | str, local_path: str | Path, *,
        expected_revision_id: int | str, confirmed: bool,
    ) -> dict[str, Any]:
        """Add a revision to one document already attached to the exact issue."""
        try:
            issue_id = dmsf_identifier(issue.get("id"), label="Redmine issue ID")
            target_id = dmsf_identifier(file_id, label="DMSF file ID")
            expected_id = dmsf_identifier(expected_revision_id, label="DMSF revision ID")
            project_id = dmsf_identifier(issue.get("project_id"), label="Redmine project ID")
        except RedminePolicyError as exc:
            raise RedmineError(str(exc), code="REDMINE_DMSF_INVALID_ID") from exc
        source = Path(local_path)
        try:
            absolute = source.resolve(strict=True)
            if source.is_symlink() or not absolute.is_file():
                raise RedmineError("The revision path must identify an ordinary local file.", code="REDMINE_DMSF_UPLOAD_FILE_NOT_FOUND")
            size = absolute.stat().st_size
        except FileNotFoundError:
            raise RedmineError("The selected local file does not exist.", code="REDMINE_DMSF_UPLOAD_FILE_NOT_FOUND") from None
        except PermissionError:
            raise RedmineError("The selected local file cannot be read.", code="REDMINE_DMSF_UPLOAD_ACCESS_DENIED") from None
        if absolute.suffix.casefold() not in DMSF_ALLOWED_EXTENSIONS or size > ATTACHMENT_LIMIT:
            raise RedmineError("The revision file type or size is not allowed.", code="REDMINE_DMSF_UPLOAD_FAILED")
        current_files, warnings = self.dmsf_file_inventory(issue)
        if warnings:
            raise RedmineError("The issue's DMSF inventory could not be verified.", code="REDMINE_DMSF_UPLOAD_VERIFICATION_FAILED")
        matches = [item for item in current_files if item.get("id") == target_id]
        if len(matches) != 1:
            raise RedmineError("The selected DMSF document is not attached to this issue.", code="REDMINE_DMSF_NOT_ATTACHED")
        metadata = matches[0]
        if metadata.get("project_id") != project_id or metadata.get("name") != absolute.name:
            raise RedmineError("The attached DMSF document does not match the selected file and project.", code="REDMINE_DMSF_UPLOAD_NAME_CONFLICT")
        folder_id = metadata.get("dmsf_folder_id")
        if not isinstance(folder_id, int) or folder_id <= 0:
            raise RedmineError("The attached DMSF document has no verifiable folder ID.", code="REDMINE_DMSF_INVALID_METADATA")
        previous = metadata.get("revision")
        if not isinstance(previous, dict) or previous.get("id") != expected_id:
            raise RedmineError("The DMSF document changed since it was selected; inspect its latest revision.", code="REDMINE_DMSF_REVISION_CONFLICT")
        preflight = {"issue_id": issue_id, "project_id": project_id, "file_id": target_id,
                     "folder_id": folder_id, "previous_revision_id": expected_id,
                     "path": str(absolute), "name": absolute.name, "size": size}
        if not confirmed:
            return {"state": "NEEDS_CONFIRMATION", "preflight": preflight}

        self._dmsf_available(project_id)
        upload_url = f"{self.base_url}/projects/{project_id}/dmsf/upload.json?{urllib.parse.urlencode({'filename': absolute.name})}"
        try:
            with absolute.open("rb") as stream:
                if absolute.stat().st_size != size:
                    raise RedmineError("The revision file changed during preflight.", code="REDMINE_DMSF_UPLOAD_FAILED")
                upload_raw = self._post(upload_url, stream,
                                        {"Content-Type": "application/octet-stream", "Content-Length": str(size)},
                                        operation="upload")
        except PermissionError:
            raise RedmineError("The revision file cannot be opened.", code="REDMINE_DMSF_UPLOAD_ACCESS_DENIED") from None
        try:
            upload_payload = json.loads(upload_raw.decode("utf-8"))
            token = upload_payload.get("upload", {}).get("token") if isinstance(upload_payload, dict) else None
        except (UnicodeError, json.JSONDecodeError, AttributeError):
            token = None
        if not isinstance(token, str) or not token or len(token) > 256 or any(ord(char) < 32 for char in token):
            raise RedmineError("Redmine returned an invalid DMSF upload token.", code="REDMINE_DMSF_UPLOAD_FAILED")
        commit_url = f"{self.base_url}/projects/{project_id}/dmsf/commit.json"
        payload = {"attachments": {"folder_id": folder_id, "uploaded_file": {
            "name": absolute.name, "title": metadata.get("title") or absolute.stem, "token": token,
        }}}
        try:
            commit_raw = self._post(
                commit_url, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                {"Content-Type": "application/json"}, operation="attach",
            )
        except RedmineError as exc:
            if exc.code == "REDMINE_DMSF_COMMIT_UNCERTAIN":
                return {"state": "COMMITTED_UNVERIFIED", "preflight": preflight,
                        "errors": [{"code": exc.code, "message": str(exc)}]}
            raise
        try:
            committed = json.loads(commit_raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            committed = None
        records = committed.get("dmsf_files") if isinstance(committed, dict) else None
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict) or records[0].get("id") != target_id:
            return {"state": "COMMITTED_UNVERIFIED", "preflight": preflight,
                    "errors": [{"code": "REDMINE_DMSF_REVISION_UNVERIFIED",
                                "message": "Commit did not identify the selected DMSF document."}]}
        try:
            fresh_issue = self.issue(issue_id)
            fresh = self.dmsf_file_metadata(target_id)
            attached = self.is_dmsf_attached(fresh_issue, target_id)
        except RedmineError as exc:
            return {"state": "COMMITTED_UNVERIFIED", "preflight": preflight,
                    "errors": [{"code": "REDMINE_DMSF_REVISION_UNVERIFIED", "message": str(exc)}]}
        revision = fresh.get("revision")
        new_id = revision.get("id") if isinstance(revision, dict) else None
        if not attached or fresh.get("dmsf_folder_id") != folder_id or new_id == expected_id or fresh.get("size") != size:
            return {"state": "COMMITTED_UNVERIFIED", "preflight": preflight,
                    "errors": [{"code": "REDMINE_DMSF_REVISION_UNVERIFIED",
                                "message": "The new revision or its issue attachment could not be verified."}]}
        return {"state": "REVISED", "preflight": preflight,
                "dms_file": {"id": target_id, "name": fresh.get("name"), "revision_id": new_id},
                "verification": {"issue_reloaded": True, "dmsf_attached": True}}

    def related_issues(self, number: str | int) -> dict[str, Any]:
        """Read direct issue relations and the visible endpoint issue summaries."""
        try:
            issue_id = issue_number(number)
        except RedminePolicyError as exc:
            raise RedmineError(str(exc), code="REDMINE_INVALID_ISSUE_ID") from exc

        def read_issue(related_id: int, *, include_relations: bool = False) -> dict[str, Any]:
            suffix = "?include=relations" if include_relations else ""
            url = f"{self.base_url}/issues/{related_id}.json{suffix}"
            response = self._open(url)
            try:
                raw = response.read(ISSUE_RESPONSE_LIMIT + 1)
            except (TimeoutError, OSError) as exc:
                raise RedmineError(f"Redmine issue {related_id} response read failed: {exc}", code="REDMINE_ISSUE_READ_FAILED") from None
            finally:
                response.close()
            if len(raw) > ISSUE_RESPONSE_LIMIT:
                raise RedmineError(f"Redmine issue {related_id} response exceeded the safety limit.", code="REDMINE_ISSUE_TOO_LARGE")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RedmineError(f"Redmine issue {related_id} returned invalid JSON.", code="REDMINE_INVALID_ISSUE_RESPONSE") from exc
            issue = payload.get("issue") if isinstance(payload, dict) else None
            if not isinstance(issue, dict) or type(issue.get("id")) is not int or issue["id"] != related_id:
                raise RedmineError(f"Redmine issue {related_id} response did not match the requested ID.", code="REDMINE_INVALID_ISSUE_RESPONSE")
            return issue

        source = read_issue(issue_id, include_relations=True)
        relations = source.get("relations")
        if not isinstance(relations, list):
            raise RedmineError("Redmine did not provide an issue relation list; check API support and permissions.", code="REDMINE_RELATIONS_UNAVAILABLE")
        if len(relations) > RELATION_COUNT_LIMIT:
            raise RedmineError(f"Issue has more than {RELATION_COUNT_LIMIT} direct relations.", code="REDMINE_RELATIONS_TOO_MANY")
        records: list[dict[str, Any]] = []
        endpoint_cache: dict[int, dict[str, Any] | RedmineError] = {}
        for relation in relations:
            if not isinstance(relation, dict):
                raise RedmineError("Redmine returned an invalid relation record.", code="REDMINE_INVALID_RELATION")
            try:
                from_id = issue_number(relation.get("issue_id"))
                to_id = issue_number(relation.get("issue_to_id"))
                relation_id = issue_number(relation.get("id"))
            except RedminePolicyError as exc:
                raise RedmineError("Redmine relation contains an invalid ID.", code="REDMINE_INVALID_RELATION") from exc
            if from_id == issue_id and to_id != issue_id:
                related_id, direction = to_id, "outgoing"
            elif to_id == issue_id and from_id != issue_id:
                related_id, direction = from_id, "incoming"
            else:
                raise RedmineError("Redmine relation does not connect the requested issue.", code="REDMINE_INVALID_RELATION")
            relation_type = relation.get("relation_type")
            if not isinstance(relation_type, str) or not relation_type.strip():
                raise RedmineError("Redmine relation has no type.", code="REDMINE_INVALID_RELATION")
            if related_id not in endpoint_cache:
                try:
                    endpoint_cache[related_id] = read_issue(related_id)
                except RedmineError as exc:
                    endpoint_cache[related_id] = exc
            endpoint = endpoint_cache[related_id]
            record: dict[str, Any] = {
                "relation_id": relation_id,
                "relation_type": _remote_text(relation_type, 80),
                "relation_label": relation_label(_remote_text(relation_type, 80), direction),
                "direction": direction,
                "id": related_id,
                "url": f"{self.base_url}/issues/{related_id}",
            }
            if isinstance(endpoint, RedmineError):
                record.update(subject=None, tracker=None, status=None, project=None,
                              error={"code": endpoint.code, "message": _remote_text(str(endpoint), 1000)})
            else:
                def name(field: str) -> str | None:
                    value = endpoint.get(field)
                    return _remote_text(value.get("name"), 200) if isinstance(value, dict) and isinstance(value.get("name"), str) else None
                subject = endpoint.get("subject")
                tracker, status = name("tracker"), name("status")
                metadata_error = None if isinstance(subject, str) and tracker and status else {
                    "code": "REDMINE_INCOMPLETE_ISSUE",
                    "message": "Related issue has no subject, tracker or status; check Redmine API response.",
                }
                record.update(subject=_remote_text(subject, 500) if isinstance(subject, str) else None,
                              tracker=tracker, status=status, project=name("project"), error=metadata_error)
            records.append(record)
        def source_name(field: str) -> str | None:
            value = source.get(field)
            return _remote_text(value.get("name"), 200) if isinstance(value, dict) and isinstance(value.get("name"), str) else None
        return {
            "issue": {"id": issue_id, "url": f"{self.base_url}/issues/{issue_id}",
                      "subject": _remote_text(source.get("subject"), 500),
                      "description": "".join(char if char.isprintable() or char in "\n\r\t" else "�"
                                             for char in str(source.get("description") or ""))[:12000],
                      "tracker": source_name("tracker"), "status": source_name("status"),
                      "project": source_name("project")},
            "relations": records,
        }

    def current_dmsf_files(self, issue: dict[str, Any]) -> list[dict[str, Any]]:
        """Read metadata for every DMSF document still attached to this issue."""
        files, warnings = self.dmsf_file_inventory(issue)
        if warnings:
            first = warnings[0]
            raise RedmineError(str(first["message"]), code=str(first["code"]))
        return files

    def _issue_page_dmsf_links(self, issue_id: int, *, require_section: bool = False) -> list[dict[str, Any]]:
        """Read DMS links rendered by Redmine when the REST journals omit them."""
        url = f"{self.base_url}/issues/{issue_id}"
        response = self._open(url, accept="text/html")
        try:
            if hasattr(response, "geturl") and response.geturl() != url:
                raise RedmineError("Redmine redirected the issue page; DMSF links cannot be verified.", code="REDMINE_DMSF_UNAVAILABLE")
            raw = response.read(ISSUE_RESPONSE_LIMIT + 1)
            content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
        except (TimeoutError, OSError) as exc:
            raise RedmineError(f"Redmine issue page read failed: {exc}", code="REDMINE_DMSF_UNAVAILABLE") from None
        finally:
            response.close()
        if len(raw) > ISSUE_RESPONSE_LIMIT:
            raise RedmineError("Redmine issue page exceeded the response safety limit.", code="REDMINE_DMSF_UNAVAILABLE")
        if content_type and "text/html" not in content_type.casefold():
            raise RedmineError("Redmine issue page is not HTML.", code="REDMINE_DMSF_UNAVAILABLE")
        try:
            html = raw.decode("utf-8")
        except UnicodeError as exc:
            raise RedmineError("Redmine issue page has invalid UTF-8.", code="REDMINE_DMSF_UNAVAILABLE") from exc
        if not re.search(r'<div\b[^>]*\bclass=["\'][^"\']*\bissue\b[^"\']*\bdetails\b', html):
            raise RedmineError("Redmine did not return the requested issue page; DMSF links cannot be verified.", code="REDMINE_DMSF_UNAVAILABLE")
        if require_section and not issue_html_has_dmsf_section(html):
            raise RedmineError(
                "The selected Redmine issue does not expose a DMSF attachment target; no upload was attempted.",
                code="REDMINE_DMSF_ISSUE_ATTACH_UNAVAILABLE",
            )
        return issue_html_dmsf_file_ids(html)

    def dmsf_file_inventory(self, issue: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return accessible DMSF metadata while retaining per-file API failures."""
        try:
            linked = current_dmsf_file_ids(issue.get("journals", []))
        except RedminePolicyError as exc:
            code = "REDMINE_DMSF_INVALID_ID" if "REDMINE_DMSF_INVALID_ID" in str(exc) else "REDMINE_DMSF_INVALID_METADATA"
            raise RedmineError(str(exc), code=code) from exc
        warnings: list[dict[str, Any]] = []
        try:
            page_links = self._issue_page_dmsf_links(dmsf_identifier(issue.get("id"), label="Redmine issue ID"))
        except RedmineError as exc:
            page_links = []
            warnings.append({"code": exc.code, "message": str(exc)[:1000]})
        linked = list({item["id"]: item for item in [*linked, *page_links]}.values())
        if len(linked) > ATTACHMENT_COUNT_LIMIT:
            raise RedmineError(
                f"Issue has more than {ATTACHMENT_COUNT_LIMIT} current DMSF files; split the intake manually.",
                code="REDMINE_DMSF_INVALID_METADATA",
            )
        result: list[dict[str, Any]] = []
        for link in linked:
            try:
                metadata = self.dmsf_file_metadata(link["id"])
            except RedmineError as exc:
                warnings.append({"code": exc.code, "message": str(exc)[:1000], "dms_file_id": link["id"]})
                continue
            metadata["issue_name"] = _remote_text(link.get("name") or metadata.get("title") or metadata.get("name"))
            result.append(metadata)
        return result, warnings

    def is_dmsf_attached(self, issue: dict[str, Any], file_id: int | str) -> bool:
        try:
            safe_id = dmsf_identifier(file_id)
            issue_id = dmsf_identifier(issue.get("id"), label="Redmine issue ID")
        except RedminePolicyError as exc:
            code = "REDMINE_DMSF_INVALID_ID" if "REDMINE_DMSF_INVALID_ID" in str(exc) else "REDMINE_DMSF_INVALID_METADATA"
            raise RedmineError(str(exc), code=code) from exc
        try:
            return any(item["id"] == safe_id for item in self._issue_page_dmsf_links(issue_id))
        except RedmineError:
            return any(item["id"] == safe_id for item in current_dmsf_file_ids(issue.get("journals", [])))

    def _normalize_dmsf_url(self, value: Any, fallback: str | None = None, *, field: str = "URL") -> str:
        """Resolve and validate a DMSF URL before it can be used for a request."""
        if value is None:
            if fallback is None:
                raise RedmineError("DMSF metadata is missing its download URL.", code="REDMINE_DMSF_UNSUPPORTED_SCHEMA")
            value = fallback
        if not isinstance(value, str):
            raise RedmineError("DMSF metadata contains an invalid download URL.", code="REDMINE_DMSF_INVALID_METADATA")
        absolute = urllib.parse.urljoin(self.base_url + "/", value)
        try:
            parsed = urllib.parse.urlsplit(absolute)
            if (url_origin(absolute) != self._origin or parsed.scheme.casefold() != "https"
                    or parsed.username or parsed.password):
                raise RedminePolicyError("DMSF URL is not same-origin HTTPS.")
        except (RedminePolicyError, ValueError) as exc:
            raise RedmineError("DMSF metadata contains an unsafe URL.", code="REDMINE_DMSF_UNSAFE_REDIRECT") from exc
        return absolute

    def dmsf_file_metadata(self, file_id: int | str) -> dict[str, Any]:
        try:
            safe_id = dmsf_identifier(file_id)
        except RedminePolicyError as exc:
            raise RedmineError(str(exc), code="REDMINE_DMSF_INVALID_ID") from exc
        url = f"{self.base_url}/dmsf/files/{safe_id}.json"
        response = self._open(url)
        try:
            raw = response.read(ISSUE_RESPONSE_LIMIT + 1)
        except (TimeoutError, OSError) as exc:
            raise RedmineError(f"DMSF metadata response read failed: {exc}", code="REDMINE_DMSF_INVALID_METADATA") from None
        finally:
            response.close()
        if len(raw) > ISSUE_RESPONSE_LIMIT:
            raise RedmineError("DMSF metadata exceeded the response safety limit.", code="REDMINE_DMSF_INVALID_METADATA")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RedmineError("Redmine returned invalid DMSF metadata JSON.", code="REDMINE_DMSF_INVALID_METADATA") from exc
        record = payload.get("dmsf_file", payload.get("file", payload)) if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            raise RedmineError(
                "DMSF metadata uses an unsupported schema; missing logical fields: dmsf_file.",
                code="REDMINE_DMSF_UNSUPPORTED_SCHEMA",
            )

        def unsupported(*fields: str) -> None:
            missing = ", ".join(dict.fromkeys(fields))
            raise RedmineError(
                f"DMSF metadata uses an unsupported schema; missing logical fields: {missing}.",
                code="REDMINE_DMSF_UNSUPPORTED_SCHEMA",
            )

        def invalid(message: str) -> None:
            raise RedmineError(message, code="REDMINE_DMSF_INVALID_METADATA")

        if "id" not in record or record.get("id") is None:
            unsupported("id")
        if "project_id" not in record and not (isinstance(record.get("project"), dict) and "id" in record["project"]):
            unsupported("project_id")
        try:
            returned_id = dmsf_identifier(record.get("id"))
            project = record.get("project") if isinstance(record.get("project"), dict) else {}
            project_id = dmsf_identifier(record.get("project_id", project.get("id")), label="DMSF project ID")
        except RedminePolicyError as exc:
            invalid(f"DMSF metadata contains an invalid identifier: {exc}")
        if returned_id != safe_id:
            invalid("DMSF metadata ID does not match the issue journal.")

        def size_from(value: Any, field: str, *, required: bool = False) -> int | None:
            if value is None:
                if required:
                    unsupported(field)
                return None
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                invalid(f"DMSF metadata field {field} has an invalid value.")
            return value

        def mime_from(value: Any, field: str, *, required: bool = False) -> str:
            if value is None:
                if required:
                    unsupported(field)
                return "application/octet-stream"
            if not isinstance(value, str):
                invalid(f"DMSF metadata field {field} has an invalid value.")
            result = value.split(";", 1)[0].strip().lower()
            if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", result):
                if required:
                    invalid(f"DMSF metadata field {field} has an invalid value.")
                return "application/octet-stream"
            return result[:128]

        def name_from(value: Any, field: str, *, fallback: Any = None, required: bool = False) -> str:
            candidate = value if value is not None else fallback
            if candidate is None:
                if required:
                    unsupported(field)
                return ""
            if not isinstance(candidate, str):
                invalid(f"DMSF metadata field {field} has an invalid value.")
            result = candidate.strip()
            if not result or len(result) > 1024:
                invalid(f"DMSF metadata field {field} has an invalid value.")
            return safe_attachment_name(result)

        def checked_url(value: Any, fallback: str | None = None, *, field: str = "URL") -> str:
            if value is None:
                if fallback is None:
                    unsupported(field)
                value = fallback
            return self._normalize_dmsf_url(value, field=field)

        top_name = next((record[name] for name in ("filename", "file_name", "name") if record.get(name) is not None), None)
        top_mime = next((record[name] for name in ("mime_type", "content_type") if record.get(name) is not None), None)
        nested = "dmsf_file_revisions" in record
        safe_revisions: list[dict[str, Any]] = []

        if nested:
            raw_revisions = record.get("dmsf_file_revisions")
            if not isinstance(raw_revisions, list):
                invalid("DMSF metadata field dmsf_file_revisions has an invalid value.")
            if not raw_revisions:
                unsupported("revision.id", "revision.size", "revision.content_url")
            for item in raw_revisions:
                if not isinstance(item, dict):
                    invalid("DMSF revision metadata has an invalid value.")
                missing = [field for key, field in (("id", "revision.id"), ("size", "revision.size"),
                                                     ("content_url", "revision.content_url")) if key not in item]
                if missing:
                    unsupported(*missing)
                try:
                    revision_id = dmsf_identifier(item.get("id"), label="DMSF revision ID")
                except RedminePolicyError as exc:
                    invalid(f"DMSF revision metadata contains an invalid identifier: {exc}")
                revision_size = size_from(item.get("size"), "revision.size", required=True)
                revision_name = name_from(item.get("name"), "revision.name", fallback=top_name, required=True)
                revision_mime = mime_from(item.get("mime_type", top_mime), "revision.mime_type")
                revision_url = checked_url(item.get("content_url"), field="revision.content_url")
                safe_revisions.append({
                    "id": revision_id,
                    "version": _remote_text(item.get("version", ""), 100),
                    "size": revision_size,
                    "name": revision_name,
                    "mime_type": revision_mime,
                    "download_url": revision_url,
                })
            revision_result = safe_revisions[0]
            filename = revision_result["name"]
            mime_type = revision_result["mime_type"]
            size_value = revision_result["size"]
            if "content_url" not in record:
                unsupported("content_url")
            absolute_url = checked_url(record.get("content_url"), field="content_url")
        else:
            filename = name_from(top_name, "filename", required=True)
            mime_type = mime_from(top_mime, "mime_type")
            raw_size = next((record[name] for name in ("filesize", "size", "file_size") if record.get(name) is not None), None)
            size_value = size_from(raw_size, "size", required=True)
            revision_field = next((name for name in ("revisions", "available_revisions", "file_revisions") if name in record), None)
            if revision_field is None:
                raw_revisions = []
            else:
                raw_revisions = record[revision_field]
                if not isinstance(raw_revisions, list):
                    invalid(f"DMSF metadata field {revision_field} has an invalid value.")
            for item in raw_revisions:
                if not isinstance(item, dict):
                    invalid("DMSF revision metadata has an invalid value.")
                if item.get("id", item.get("revision_id")) is None:
                    continue
                try:
                    revision_id = dmsf_identifier(item.get("id", item.get("revision_id")), label="DMSF revision ID")
                except RedminePolicyError as exc:
                    invalid(f"DMSF revision metadata contains an invalid identifier: {exc}")
                revision_size = size_from(item.get("filesize", item.get("size")), "revision.size")
                revision_url = checked_url(
                    item.get("content_url", item.get("download_url")),
                    f"{self.base_url}/dmsf/files/{safe_id}/view?{urllib.parse.urlencode({'download': revision_id})}",
                    field="revision.content_url",
                )
                safe_revisions.append({
                    "id": revision_id,
                    "version": _remote_text(item.get("version", ""), 100),
                    "size": revision_size,
                    "download_url": revision_url,
                })
            latest = None
            for latest_name in ("revision", "latest_revision", "last_revision"):
                if latest_name not in record:
                    continue
                if record[latest_name] is not None and not isinstance(record[latest_name], dict):
                    invalid(f"DMSF metadata field {latest_name} has an invalid value.")
                if isinstance(record[latest_name], dict):
                    latest = record[latest_name]
                    break
            if latest is None and safe_revisions:
                latest = safe_revisions[-1]
            if latest is None:
                unsupported("revisions", "revision.id")
            try:
                latest_id = dmsf_identifier(latest.get("id", latest.get("revision_id")), label="DMSF revision ID")
            except RedminePolicyError as exc:
                invalid(f"DMSF latest revision metadata contains an invalid identifier: {exc}")
            latest_size = size_from(latest.get("filesize", latest.get("size")), "revision.size")
            if latest_size is None:
                latest_size = size_value
            latest_url = checked_url(
                latest.get("content_url", latest.get("download_url")),
                f"{self.base_url}/dmsf/files/{safe_id}/view?{urllib.parse.urlencode({'download': latest_id})}",
                field="revision.content_url",
            )
            revision_result = next((item for item in safe_revisions if item["id"] == latest_id), None)
            if revision_result is None:
                revision_result = {"id": latest_id, "version": _remote_text(latest.get("version", ""), 100),
                                   "size": latest_size, "download_url": latest_url}
                safe_revisions.append(revision_result)
            else:
                revision_result = {**revision_result, "size": latest_size, "download_url": latest_url,
                                   "version": _remote_text(latest.get("version", revision_result.get("version", "")), 100)}
                safe_revisions = [revision_result if item["id"] == latest_id else item for item in safe_revisions]
            absolute_url = checked_url(
                record.get("content_url", record.get("download_url")),
                f"{self.base_url}/dmsf/files/{safe_id}/download",
                field="content_url",
            )
        title = _remote_text(record.get("title") or Path(filename).stem)
        folder_value = record.get("dmsf_folder_id")
        try:
            folder_id = dmsf_identifier(folder_value, label="DMSF folder ID") if folder_value is not None else None
        except RedminePolicyError as exc:
            invalid(f"DMSF metadata contains an invalid folder identifier: {exc}")
        return {
            "id": safe_id, "name": filename, "title": title[:500], "project_id": project_id,
            "dmsf_folder_id": folder_id,
            "size": size_value, "mime_type": mime_type[:128], "download_url": absolute_url,
            "revision": revision_result, "revisions": safe_revisions,
        }

    def download_dmsf_file(
        self, metadata: dict[str, Any], destination: Path, *, revision_id: int | str | None = None,
        max_bytes: int = ATTACHMENT_LIMIT,
    ) -> tuple[Path, dict[str, Any]]:
        """Download one DMSF revision to a new file, deleting partial data on failure."""
        try:
            file_id = dmsf_identifier(metadata.get("id"))
            chosen_revision = dmsf_identifier(revision_id, label="DMSF revision ID") if revision_id is not None else None
        except RedminePolicyError as exc:
            raise RedmineError(str(exc), code="REDMINE_DMSF_INVALID_ID") from exc
        revisions = metadata.get("revisions", [])
        if not isinstance(revisions, list):
            raise RedmineError("DMSF metadata has an invalid revision list.", code="REDMINE_DMSF_INVALID_METADATA")
        if chosen_revision is not None and chosen_revision not in {item.get("id") for item in revisions if isinstance(item, dict)}:
            raise RedmineError("Requested DMSF revision is not available for this file.", code="REDMINE_DMSF_INVALID_METADATA")
        extension = Path(str(metadata.get("name", ""))).suffix.casefold()
        if extension not in DMSF_ALLOWED_EXTENSIONS:
            raise RedmineError("DMSF document format is not supported for intake.", code="REDMINE_DMSF_INVALID_METADATA")
        chosen = metadata.get("revision") if chosen_revision is None else next(
            item for item in revisions if isinstance(item, dict) and item.get("id") == chosen_revision
        )
        latest_expected = chosen.get("size") if isinstance(chosen, dict) else metadata.get("size")
        if latest_expected is None:
            latest_expected = metadata.get("size")
        if latest_expected is not None and (not isinstance(latest_expected, int) or isinstance(latest_expected, bool) or latest_expected < 0):
            raise RedmineError("DMSF file size is invalid.", code="REDMINE_DMSF_INVALID_METADATA")
        if chosen_revision is None:
            url = self._normalize_dmsf_url(
                metadata.get("download_url"), f"{self.base_url}/dmsf/files/{file_id}/download"
            )
        else:
            url = self._normalize_dmsf_url(
                chosen.get("download_url") if isinstance(chosen, dict) else None,
                f"{self.base_url}/dmsf/files/{file_id}/view?{urllib.parse.urlencode({'download': chosen_revision})}",
            )
        expected = latest_expected if chosen_revision is None else chosen.get("size") if isinstance(chosen, dict) else None
        if expected is not None and expected > min(ATTACHMENT_LIMIT, max_bytes):
            raise RedmineError("DMSF document exceeds the remaining 500 MiB safety limit.", code="REDMINE_DMSF_SIZE_LIMIT")
        response = self._open(url, accept="*/*")
        path = destination / safe_attachment_name(str(metadata.get("name", "attachment")), file_id)
        if path.exists():
            response.close()
            raise RedmineError("DMSF download would overwrite an existing file.", code="REDMINE_DMSF_INVALID_METADATA")
        headers = getattr(response, "headers", None)
        if headers is None and hasattr(response, "info"):
            headers = response.info()
        content_type = str(headers.get("Content-Type", "") if headers is not None else "").split(";", 1)[0].strip().lower()
        content_length = headers.get("Content-Length") if headers is not None else None
        try:
            declared_length = int(content_length) if content_length is not None else None
        except (TypeError, ValueError):
            response.close()
            raise RedmineError("DMSF response has an invalid Content-Length.", code="REDMINE_DMSF_INVALID_METADATA") from None
        if declared_length is not None and ((expected is not None and declared_length != expected) or declared_length > min(ATTACHMENT_LIMIT, max_bytes)):
            response.close()
            code = "REDMINE_DMSF_SIZE_LIMIT" if declared_length > min(ATTACHMENT_LIMIT, max_bytes) else "REDMINE_DMSF_INVALID_METADATA"
            raise RedmineError("DMSF response size does not match its metadata or exceeds the safety limit.", code=code)
        if expected is None:
            if declared_length is None:
                response.close()
                raise RedmineError("Selected revision has no verifiable size in metadata or Content-Length.", code="REDMINE_DMSF_INVALID_METADATA")
            expected = declared_length
        if content_type in {"text/html", "application/xhtml+xml"}:
            response.close()
            raise RedmineError("Redmine returned an HTML page instead of a DMSF document.", code="REDMINE_DMSF_HTML_RESPONSE")
        written = 0
        try:
            with path.open("xb") as stream:
                while True:
                    try:
                        chunk = response.read(min(1024 * 1024, min(ATTACHMENT_LIMIT, max_bytes) - written + 1))
                    except RedmineError:
                        raise
                    except Exception as exc:
                        raise RedmineError(f"DMSF download was interrupted: {exc}", code="REDMINE_DMSF_DOWNLOAD_INTERRUPTED") from None
                    if not chunk:
                        break
                    if written == 0 and chunk[:512].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                        raise RedmineError("Redmine returned an HTML page instead of a DMSF document.", code="REDMINE_DMSF_HTML_RESPONSE")
                    written += len(chunk)
                    if written > min(ATTACHMENT_LIMIT, max_bytes):
                        raise RedmineError("DMSF document exceeds the remaining 500 MiB safety limit.", code="REDMINE_DMSF_SIZE_LIMIT")
                    stream.write(chunk)
            if written != expected:
                raise RedmineError("Downloaded DMSF size does not match the metadata size.", code="REDMINE_DMSF_DOWNLOAD_INTERRUPTED")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        result = {"id": file_id, "name": path.name, "size": written, "mime_type": metadata.get("mime_type", "application/octet-stream"),
                  "download_url": url}
        if isinstance(chosen, dict):
            result["revision"] = {"id": chosen.get("id"), "version": chosen.get("version", "")}
        return path, result

    def download_attachments(self, issue: dict[str, Any], destination: Path) -> tuple[list[Path], list[dict[str, Any]]]:
        destination.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        metadata: list[dict[str, Any]] = []
        total = 0
        for attachment in issue["attachments"]:
            if not isinstance(attachment, dict):
                raise RedmineError("Redmine issue returned an invalid attachment record.")
            content_url = str(attachment.get("content_url", "")).strip()
            if not content_url:
                raise RedmineError("Redmine attachment is missing its content URL.")
            absolute_url = urllib.parse.urljoin(self.base_url + "/", content_url)
            try:
                attachment_origin = url_origin(absolute_url)
            except RedminePolicyError as exc:
                raise RedmineError("Redmine returned a malformed attachment URL; download was blocked.") from exc
            if attachment_origin != self._origin:
                raise RedmineError("Redmine attachment URL points to another host; download was blocked.")
            attachment_id = attachment.get("id")
            filename = safe_attachment_name(str(attachment.get("filename", "attachment")), attachment_id)
            path = destination / filename
            if path.exists():
                raise RedmineError("Redmine returned duplicate attachment names; refusing to overwrite a file.")
            expected = attachment.get("filesize")
            if isinstance(expected, int) and expected > ATTACHMENT_LIMIT:
                raise RedmineError(f"Attachment {filename} exceeds the 500 MiB safety limit.")
            response = self._open(absolute_url, accept="*/*")
            written = 0
            try:
                with path.open("xb") as stream:
                    while True:
                        try:
                            chunk = response.read(min(1024 * 1024, ATTACHMENT_LIMIT - total - written + 1))
                        except (TimeoutError, OSError) as exc:
                            raise RedmineError(f"Download interrupted for attachment {filename}: {exc}") from None
                        if not chunk:
                            break
                        written += len(chunk)
                        if written + total > ATTACHMENT_LIMIT:
                            raise RedmineError("Redmine attachments exceed the 500 MiB total safety limit.")
                        stream.write(chunk)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            finally:
                response.close()
            total += written
            paths.append(path)
            metadata.append({
                "id": str(attachment_id)[:100] if attachment_id is not None else None,
                "name": path.name,
                "size": written,
                "media_type": str(attachment.get("content_type", "application/octet-stream"))[:128],
            })
        return paths, metadata
