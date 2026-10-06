"""Small read-only Gitea client used by Git merge evidence collection."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class GiteaError(RuntimeError):
    code: str
    message: str
    recoverable: bool = True
    next_action: str = "continue-with-git-evidence"

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "component": "gitea", "message": self.message,
                "recoverable": self.recoverable, "next_action": self.next_action}


def normalize_base_url(raw_url: str) -> str:
    parsed = urllib.parse.urlsplit(str(raw_url or "").strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise GiteaError("GITEA_URL_INVALID", "Gitea base URL must be an HTTP(S) origin")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class GiteaClient:
    def __init__(self, base_url: str, owner: str, repository: str, *, token_env: str,
                 timeout: float = 15.0, opener: Any = None) -> None:
        self.base_url = normalize_base_url(base_url)
        self.owner = owner
        self.repository = repository
        self.token_env = token_env
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[Any, str]:
        token = os.environ.get(self.token_env, "")
        if not token:
            raise GiteaError("GITEA_AUTH_REQUIRED", f"Environment variable {self.token_env} is unavailable")
        encoded = urllib.parse.urlencode(query)
        url = f"{self.base_url}/api/v1{path}?{encoded}"
        request = urllib.request.Request(url, method="GET", headers={"Authorization": f"token {token}", "Accept": "application/json"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            code = "GITEA_AUTH_REQUIRED" if exc.code in {401, 403} else "GITEA_API_ERROR"
            raise GiteaError(code, f"Gitea returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GiteaError("GITEA_NETWORK_ERROR", f"Gitea is unavailable: {exc}") from exc
        if len(payload) > 2 * 1024 * 1024:
            raise GiteaError("GITEA_RESPONSE_LIMIT", "Gitea response exceeded 2 MiB")
        try:
            return json.loads(payload), hashlib.sha256(payload).hexdigest()
        except json.JSONDecodeError as exc:
            raise GiteaError("GITEA_RESPONSE_INVALID", "Gitea returned invalid JSON") from exc

    def merged_pulls(self, head: str, base: str) -> dict[str, Any]:
        owner = urllib.parse.quote(self.owner, safe="")
        repository = urllib.parse.quote(self.repository, safe="")
        value, digest = self._get(f"/repos/{owner}/{repository}/pulls", {
            "state": "closed", "head": head, "base": base, "limit": "50",
        })
        if not isinstance(value, list):
            raise GiteaError("GITEA_RESPONSE_INVALID", "Gitea pull list must be an array")
        records = []
        for item in value:
            if not isinstance(item, dict) or not item.get("merged"):
                continue
            merge_sha = str(item.get("merge_commit_sha") or "").lower()
            head_sha = str((item.get("head") or {}).get("sha") or "").lower()
            records.append({
                "number": item.get("number"), "title": str(item.get("title") or ""),
                "merge_commit_sha": merge_sha, "head_sha": head_sha,
                "merged_at": item.get("merged_at"), "url": item.get("html_url"),
            })
        return {"records": records, "response_sha256": digest}
