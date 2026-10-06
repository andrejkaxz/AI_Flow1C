from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from scripts.gitea_client import GiteaClient, GiteaError


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


class GiteaClientTests(unittest.TestCase):
    def test_missing_token_is_structured_and_never_sent(self) -> None:
        client = GiteaClient("https://git.example.invalid", "o", "r", token_env="MISSING_TEST_TOKEN")
        with self.assertRaisesRegex(GiteaError, "MISSING_TEST_TOKEN"):
            client.merged_pulls("feature", "main")

    def test_merged_pull_is_normalized_and_response_is_hashed(self) -> None:
        payload = b'[{"number":7,"merged":true,"merge_commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","head":{"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"html_url":"https://git/p/7"}]'
        opener = mock.Mock(return_value=Response(payload))
        client = GiteaClient("https://git.example.invalid/", "owner", "repo", token_env="TEST_GITEA_TOKEN", opener=opener)
        with mock.patch.dict(os.environ, {"TEST_GITEA_TOKEN": "secret"}):
            result = client.merged_pulls("feature", "main")
        self.assertEqual(result["records"][0]["number"], 7)
        self.assertNotIn("secret", str(result))
        self.assertEqual(len(result["response_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
