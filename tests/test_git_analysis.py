from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from scripts import flow1c_git
from scripts.flow1c_git_policy import select_issue_branch


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True,
                            text=True, encoding="utf-8")
    return result.stdout.strip()


class GitFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "user.email", "fixture@example.invalid")
        (root / "Module.bsl").write_text("base\n", encoding="utf-8")
        git(root, "add", "Module.bsl")
        git(root, "commit", "-m", "base")

    def commit(self, branch: str, text: str, subject: str) -> str:
        git(self.root, "switch", branch)
        (self.root / "Module.bsl").write_text(text, encoding="utf-8")
        git(self.root, "commit", "-am", subject)
        return git(self.root, "rev-parse", "HEAD")


class BranchDiscoveryTests(unittest.TestCase):
    def test_issue_branch_selection_uses_exact_configured_or_unique_suffix(self) -> None:
        branches = ["iss/11963", "feature/11963", "iss/20000"]
        self.assertEqual(select_issue_branch("11963", branches)["state"], "AMBIGUOUS")
        self.assertEqual(select_issue_branch("11963", branches, "iss")["selected"], "iss/11963")
        self.assertEqual(select_issue_branch("11963", branches + ["11963"], "iss")["selected"], "11963")
        self.assertEqual(select_issue_branch("11963", ["feature/11963"])["selected"], "feature/11963")
        self.assertEqual(select_issue_branch("11963", branches, "bug")["state"], "AMBIGUOUS")
        self.assertEqual(select_issue_branch("11963", [], "iss")["state"], "NOT_FOUND")
        self.assertEqual(select_issue_branch("iss/11963", branches)["selected"], "iss/11963")

    def test_remote_branch_names_resolve_issue_without_a_checkout(self) -> None:
        output = b"a\trefs/heads/main\nb\trefs/heads/iss/11963\n"
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr=b"")
        with patch.object(flow1c_git, "run_git", return_value=completed) as mocked:
            result = flow1c_git.discover_issue_refs(Path("unused"), ["11963"], prefix="iss")
        self.assertEqual(result["resolved"], {"11963": "iss/11963"})
        mocked.assert_called_once_with(Path("unused"), ["ls-remote", "--heads", "origin"], timeout=45)

    def test_remote_discovery_distinguishes_ambiguity_and_network_error(self) -> None:
        branches = b"a\trefs/heads/iss/11963\nb\trefs/heads/feature/11963\n"
        with patch.object(flow1c_git, "run_git", return_value=subprocess.CompletedProcess(
                [], 0, stdout=branches, stderr=b"")):
            ambiguous = flow1c_git.discover_issue_refs(Path("unused"), ["11963"])
        self.assertEqual(ambiguous["code"], "GIT_BRANCH_AMBIGUOUS")
        self.assertEqual(ambiguous["matches"]["11963"]["candidates"], ["feature/11963", "iss/11963"])
        with patch.object(flow1c_git, "run_git", return_value=subprocess.CompletedProcess(
                [], 128, stdout=b"", stderr=b"Could not resolve host")):
            unavailable = flow1c_git.discover_issue_refs(Path("unused"), ["11963"])
        self.assertEqual(unavailable["code"], "GIT_BRANCH_NETWORK_ERROR")


class GitAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name)
        self.fixture = GitFixture(self.repository)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_remote_issue_discovery_finds_full_ref_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as bare_directory:
            bare = Path(bare_directory)
            git(bare, "init", "--bare")
            git(self.repository, "remote", "add", "origin", bare.as_uri())
            git(self.repository, "branch", "iss/11963")
            git(self.repository, "push", "origin", "iss/11963", "main")
            result = flow1c_git.discover_issue_refs(self.repository, ["11963"], prefix="iss")
            self.assertEqual(result["state"], "READ")
            self.assertEqual(result["resolved"]["11963"], "iss/11963")
            refreshed = flow1c_git.refresh_refs(self.repository, remote="origin", refs=[result["resolved"]["11963"]])
            self.assertIn(refreshed["state"], {"UPDATED", "UNCHANGED"})
            self.assertEqual(flow1c_git.resolve_ref(self.repository, "iss/11963")["selected"]["full_ref"],
                             "refs/remotes/origin/iss/11963")

    def test_no_ff_merge_returns_exact_topology_and_first_parent_range(self) -> None:
        git(self.repository, "switch", "-c", "iss/9154")
        source = self.fixture.commit("iss/9154", "feature\n", "ISS-9154 feature")
        git(self.repository, "switch", "main")
        first_parent = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "merge", "--no-ff", "iss/9154", "-m", "Merge branch 'iss/9154'")
        merge = git(self.repository, "rev-parse", "HEAD")

        result = flow1c_git.merge_search(self.repository, ["iss/9154"], "main")[0]

        self.assertEqual(result["integration_status"], "MERGE_FOUND")
        self.assertEqual(result["final_merge"]["commit"], merge)
        self.assertEqual(result["final_merge"]["evidence_level"], "EXACT_TOPOLOGY")
        self.assertEqual(result["integrated_source_commit"], source)
        self.assertEqual(result["ranges"]["merge_result_diff"], f"{first_parent}..{merge}")

    def test_post_merge_commits_return_partial_integration(self) -> None:
        git(self.repository, "switch", "-c", "iss/9728")
        integrated = self.fixture.commit("iss/9728", "integrated\n", "ISS-9728 feature")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--no-ff", "iss/9728", "-m", "Merge iss/9728")
        self.fixture.commit("iss/9728", "new tip\n", "post merge")

        result = flow1c_git.merge_search(self.repository, ["iss/9728"], "main")[0]

        self.assertEqual(result["integration_status"], "PARTIALLY_MERGED")
        self.assertEqual(result["integrated_source_commit"], integrated)
        self.assertEqual(len(result["post_merge_commits"]), 1)

    def test_multiple_merges_select_latest_merge_that_introduces_commits(self) -> None:
        git(self.repository, "switch", "-c", "repeat")
        self.fixture.commit("repeat", "one\n", "repeat one")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--no-ff", "repeat", "-m", "Merge repeat one")
        first_merge = git(self.repository, "rev-parse", "HEAD")
        self.fixture.commit("repeat", "two\n", "repeat two")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--no-ff", "repeat", "-m", "Merge repeat two")
        second_merge = git(self.repository, "rev-parse", "HEAD")
        original_run_git = flow1c_git.run_git
        calls: list[list[str]] = []

        def counted_run_git(repository: Path, arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append(arguments)
            return original_run_git(repository, arguments, **kwargs)

        with patch.object(flow1c_git, "run_git", side_effect=counted_run_git):
            result = flow1c_git.merge_search(self.repository, ["repeat"], "main")[0]
        self.assertEqual(result["integration_status"], "MULTIPLE_MERGES_FOUND")
        self.assertEqual([item["commit"] for item in result["merge_candidates"]], [first_merge, second_merge])
        self.assertEqual(result["final_merge"]["commit"], second_merge)
        self.assertTrue(any("--ancestry-path" in arguments for arguments in calls))
        self.assertFalse(any(arguments[:2] == ["merge-base", "--is-ancestor"] for arguments in calls))

    def test_fast_forward_and_missing_refs_are_typed(self) -> None:
        git(self.repository, "switch", "-c", "fast")
        source = self.fixture.commit("fast", "ff\n", "fast")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--ff-only", "fast")
        records = flow1c_git.merge_search(self.repository, ["fast", "missing"], "main")
        self.assertEqual(records[0]["integration_status"], "FAST_FORWARD")
        self.assertEqual(records[0]["review_ref"], source)
        self.assertEqual(records[1]["integration_status"], "SOURCE_NOT_FOUND")

    def test_target_resolution_prefers_origin_tracking_ref_over_stale_local(self) -> None:
        previous = git(self.repository, "rev-parse", "HEAD")
        (self.repository / "Module.bsl").write_text("remote fresh\n", encoding="utf-8")
        git(self.repository, "commit", "-am", "remote state")
        remote_commit = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "update-ref", "refs/remotes/origin/main", remote_commit)
        git(self.repository, "reset", "--hard", previous)
        resolved = flow1c_git.resolve_ref(self.repository, "main", role="target")
        self.assertEqual(resolved["selected"]["full_ref"], "refs/remotes/origin/main")
        self.assertEqual(resolved["selected"]["commit"], remote_commit)

    def test_branch_changes_excludes_common_main_history(self) -> None:
        git(self.repository, "switch", "-c", "feature")
        first = self.fixture.commit("feature", "one\n", "G-010 one")
        second = self.fixture.commit("feature", "two\n", "G-010 two")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--no-ff", "feature", "-m", "Merge feature")
        result = flow1c_git.branch_changes(self.repository, "feature", "main")
        self.assertEqual([item["commit"] for item in result["commits"]], [first, second])

    def test_bounded_diff_has_full_names_and_verified_cursor(self) -> None:
        git(self.repository, "switch", "-c", "large")
        for index in range(4):
            (self.repository / f"file{index}.md").write_text(str(index), encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "many files")
        commit = git(self.repository, "rev-parse", "HEAD")
        first = flow1c_git.diff(self.repository, commit, detail="patch", max_files=2, max_chars=10000)
        self.assertEqual(first["total_files"], 4)
        self.assertEqual(len(first["files"]), 4)
        self.assertIsNotNone(first["next_cursor"])
        second = flow1c_git.diff(self.repository, commit, detail="patch", max_files=2,
                                 max_chars=10000, cursor=first["next_cursor"])
        self.assertEqual(second["returned_files"], 2)
        with self.assertRaisesRegex(flow1c_git.GitAnalysisError, "different parameters"):
            flow1c_git.diff(self.repository, commit, detail="stat", cursor=first["next_cursor"])
        with self.assertRaisesRegex(flow1c_git.GitAnalysisError, "different page size"):
            flow1c_git.diff(self.repository, commit, detail="patch", max_files=1,
                            cursor=first["next_cursor"])

    def test_single_large_patch_can_be_read_completely_by_cursor(self) -> None:
        git(self.repository, "switch", "-c", "large-file")
        lines = [f"Строка {index:04d}: " + "данные " * 12 for index in range(800)]
        (self.repository / "Module.bsl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        git(self.repository, "commit", "-am", "large BSL update")
        commit = git(self.repository, "rev-parse", "HEAD")
        expected = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--find-copies",
             "--unified=3", "main", commit, "--", "Module.bsl"],
            cwd=self.repository, check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout

        cursor = ""
        chunks = []
        for _ in range(20):
            result = flow1c_git.diff(self.repository, commit, base_ref="main", detail="patch",
                                     paths=["Module.bsl"], cursor=cursor, max_files=1, max_chars=16000)
            chunks.append(result["content"])
            self.assertLessEqual(len(result["content"]), 16000)
            self.assertEqual(result["diff"], result["content"])
            cursor = result["next_cursor"] or ""
            if not cursor:
                break
        else:
            self.fail("Large patch pagination did not finish")
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), expected)

    def test_read_at_ref_does_not_require_file_in_checkout(self) -> None:
        git(self.repository, "switch", "-c", "history")
        (self.repository / "old.md").write_text("historical", encoding="utf-8")
        git(self.repository, "add", "old.md")
        git(self.repository, "commit", "-m", "add old")
        commit = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "rm", "old.md")
        git(self.repository, "commit", "-m", "remove old")
        result = flow1c_git.read_at_ref(self.repository, commit, "old.md")
        self.assertEqual(result["content"], "historical")
        with self.assertRaises(flow1c_git.GitAnalysisError):
            flow1c_git.read_at_ref(self.repository, commit, "../secret.md")

    def test_history_search_cursor_is_bound_to_filters(self) -> None:
        for index in range(4):
            self.fixture.commit("main", f"main {index}\n", f"main change {index}")

        first = flow1c_git.history_search(self.repository, "main", max_count=2)
        second = flow1c_git.history_search(
            self.repository, "main", max_count=2, cursor=first["next_cursor"]
        )

        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 2)
        self.assertTrue(
            set(item["commit"] for item in first["items"]).isdisjoint(
                item["commit"] for item in second["items"]
            )
        )
        with self.assertRaisesRegex(flow1c_git.GitAnalysisError, "different parameters"):
            flow1c_git.history_search(
                self.repository, "main", subject_query="other", max_count=2,
                cursor=first["next_cursor"],
            )

    def test_object_change_set_uses_bsl_structure_not_free_text(self) -> None:
        base = git(self.repository, "rev-parse", "HEAD")
        directory = self.repository / "Documents" / "Заказ" / "Ext"
        directory.mkdir(parents=True)
        module = directory / "Module.bsl"
        module.write_text("Процедура Новая()\nКонецПроцедуры\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "object")
        commit = git(self.repository, "rev-parse", "HEAD")
        changes = flow1c_git.object_change_set(self.repository, base, commit)
        self.assertEqual(changes[0]["object_type"], "Document")
        self.assertEqual(changes[0]["object_name"], "Заказ")
        self.assertEqual(changes[0]["code_changes"]["procedures_added"], ["Новая"])

    def test_shell_metacharacters_are_rejected_as_ref(self) -> None:
        with self.assertRaises(flow1c_git.GitAnalysisError):
            flow1c_git.resolve_ref(self.repository, "main; touch owned")

    def test_mutating_git_command_is_rejected_before_subprocess(self) -> None:
        with self.assertRaisesRegex(flow1c_git.GitAnalysisError, "allowlist"):
            flow1c_git.run_git(self.repository, ["reset", "--hard", "HEAD"])

    def test_squash_merge_is_detected_by_patch_equivalence(self) -> None:
        git(self.repository, "switch", "-c", "squashed")
        self.fixture.commit("squashed", "squashed\n", "squashed change")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--squash", "squashed")
        git(self.repository, "commit", "-m", "squash integration")

        result = flow1c_git.merge_search(self.repository, ["squashed"], "main")[0]

        self.assertEqual(result["integration_status"], "SQUASH_OR_REBASE_MATCH")
        self.assertEqual(result["final_merge"]["evidence_level"], "PATCH_EQUIVALENT")
        self.assertEqual(result["patch_coverage"], 1.0)

    def test_partial_cherry_pick_reports_partial_patch_coverage(self) -> None:
        git(self.repository, "switch", "-c", "picked")
        first = self.fixture.commit("picked", "picked one\n", "first picked change")
        (self.repository / "second.md").write_text("second", encoding="utf-8")
        git(self.repository, "add", "second.md")
        git(self.repository, "commit", "-m", "second picked change")
        git(self.repository, "switch", "main")
        # Test patch equivalence with a distinct commit even when Git's
        # one-second timestamps would otherwise reproduce the original SHA.
        git(self.repository, "cherry-pick", "-x", first)

        result = flow1c_git.merge_search(self.repository, ["picked"], "main")[0]

        self.assertEqual(result["integration_status"], "PARTIALLY_MERGED")
        self.assertGreater(result["patch_coverage"], 0.0)
        self.assertLess(result["patch_coverage"], 1.0)

    def test_octopus_merge_uses_non_first_parent_topology(self) -> None:
        git(self.repository, "switch", "-c", "octopus-a")
        (self.repository / "a.md").write_text("a", encoding="utf-8")
        git(self.repository, "add", "a.md")
        git(self.repository, "commit", "-m", "octopus a")
        git(self.repository, "switch", "main")
        git(self.repository, "switch", "-c", "octopus-b")
        (self.repository / "b.md").write_text("b", encoding="utf-8")
        git(self.repository, "add", "b.md")
        git(self.repository, "commit", "-m", "octopus b")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--no-ff", "octopus-a", "octopus-b", "-m", "octopus integration")

        result = flow1c_git.merge_search(self.repository, ["octopus-b"], "main")[0]

        self.assertEqual(result["integration_status"], "MERGE_FOUND")
        self.assertGreaterEqual(len(result["final_merge"]["parents"]), 3)

    def test_deleted_source_branch_yields_bounded_history_candidate(self) -> None:
        git(self.repository, "switch", "-c", "deleted-source")
        self.fixture.commit("deleted-source", "deleted source\n", "source change")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--no-ff", "deleted-source", "-m", "Merge branch 'deleted-source'")
        git(self.repository, "branch", "-D", "deleted-source")

        result = flow1c_git.merge_search(self.repository, ["deleted-source"], "main")[0]

        self.assertIsNone(result["source_commit"])
        self.assertEqual(len(result["merge_candidates"]), 1)
        self.assertEqual(result["merge_candidates"][0]["evidence_level"], "HEURISTIC_CANDIDATE")

    def test_unicode_slash_dot_and_dash_branch_resolves(self) -> None:
        branch = "feature/разработка.1-test"
        git(self.repository, "switch", "-c", branch)

        result = flow1c_git.resolve_ref(self.repository, branch)

        self.assertEqual(result["selected"]["full_ref"], f"refs/heads/{branch}")

    def test_shallow_history_prevents_conclusive_negative_result(self) -> None:
        with tempfile.TemporaryDirectory() as bare_directory, tempfile.TemporaryDirectory() as clone_parent:
            bare = Path(bare_directory)
            shallow = Path(clone_parent) / "shallow"
            git(bare, "init", "--bare")
            git(self.repository, "remote", "add", "publish", bare.as_uri())
            git(self.repository, "push", "publish", "main")
            subprocess.run(
                ["git", "clone", "--depth", "1", bare.as_uri(), str(shallow)],
                check=True, capture_output=True, text=True, encoding="utf-8",
            )

            result = flow1c_git.merge_search(shallow, ["missing-source"], "main")[0]

            self.assertEqual(result["integration_status"], "REFRESH_REQUIRED")
            self.assertIn("GIT_SHALLOW_HISTORY", {item["code"] for item in result["limitations"]})

    def test_binary_rename_copy_and_delete_are_visible_in_full_file_list(self) -> None:
        (self.repository / "old.md").write_text("old", encoding="utf-8")
        (self.repository / "source.bin").write_bytes(b"\x00\x01binary")
        (self.repository / "removed.md").write_text("remove", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "files before structural changes")
        base = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "mv", "old.md", "renamed.md")
        (self.repository / "copied.bin").write_bytes((self.repository / "source.bin").read_bytes())
        (self.repository / "source.bin").write_bytes(b"\x00\x01binary changed")
        git(self.repository, "rm", "removed.md")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "rename copy delete binary")
        commit = git(self.repository, "rev-parse", "HEAD")

        result = flow1c_git.diff(self.repository, commit, base_ref=base, detail="names")

        names = "\n".join(result["files"])
        self.assertIn("renamed.md", names)
        self.assertIn("copied.bin", names)
        self.assertIn("removed.md", names)

    def test_merge_search_reaches_past_one_hundred_target_commits(self) -> None:
        git(self.repository, "switch", "-c", "deep-source")
        self.fixture.commit("deep-source", "deep\n", "deep feature")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--no-ff", "deep-source", "-m", "deep merge")
        expected = git(self.repository, "rev-parse", "HEAD")
        for index in range(105):
            git(self.repository, "commit", "--allow-empty", "-m", f"later target {index}")

        result = flow1c_git.merge_search(self.repository, ["deep-source"], "main")[0]

        self.assertEqual(result["final_merge"]["commit"], expected)

    def test_targeted_refresh_updates_only_requested_remote_tracking_ref(self) -> None:
        with tempfile.TemporaryDirectory() as bare_directory, tempfile.TemporaryDirectory() as writer_parent:
            bare = Path(bare_directory)
            writer = Path(writer_parent) / "writer"
            git(bare, "init", "--bare")
            git(self.repository, "remote", "add", "origin", bare.as_uri())
            git(self.repository, "push", "-u", "origin", "main")
            subprocess.run(
                ["git", "clone", "-b", "main", bare.as_uri(), str(writer)],
                check=True, capture_output=True, text=True, encoding="utf-8",
            )
            git(writer, "config", "user.name", "Writer")
            git(writer, "config", "user.email", "writer@example.invalid")
            (writer / "remote.md").write_text("remote", encoding="utf-8")
            git(writer, "add", "remote.md")
            git(writer, "commit", "-m", "remote target update")
            expected = git(writer, "rev-parse", "HEAD")
            git(writer, "push", "origin", "main")

            refreshed = flow1c_git.refresh_refs(self.repository, remote="origin", refs=["main"])

            self.assertEqual(refreshed["state"], "UPDATED")
            self.assertEqual(refreshed["after"]["main"]["selected"]["commit"], expected)

    def test_unrelated_histories_do_not_produce_false_integration(self) -> None:
        git(self.repository, "switch", "--orphan", "unrelated")
        (self.repository / "unrelated.md").write_text("unrelated", encoding="utf-8")
        git(self.repository, "add", "unrelated.md")
        git(self.repository, "commit", "-m", "unrelated root")
        git(self.repository, "switch", "main")

        result = flow1c_git.merge_search(self.repository, ["unrelated"], "main")[0]

        self.assertEqual(result["integration_status"], "NO_INTEGRATION_EVIDENCE")
        self.assertFalse(result["merge_candidates"])

    def test_mixed_task_and_unnamed_commits_keep_individual_references(self) -> None:
        git(self.repository, "switch", "-c", "mixed-scope")
        first = self.fixture.commit("mixed-scope", "g010\n", "G-010 requested change")
        second = self.fixture.commit("mixed-scope", "fix\n", "fix")
        third = self.fixture.commit("mixed-scope", "g002\n", "G-002 neighbouring change")

        result = flow1c_git.branch_changes(self.repository, "mixed-scope", "main")
        by_commit = {item["commit"]: item for item in result["commits"]}

        self.assertEqual(by_commit[first]["references"], ["G-010"])
        self.assertEqual(by_commit[second]["references"], [])
        self.assertEqual(by_commit[third]["references"], ["G-002"])

    def test_rebased_original_source_is_detected_by_patch_equivalence(self) -> None:
        git(self.repository, "switch", "-c", "rebased")
        self.fixture.commit("rebased", "rebased\n", "rebased feature")
        git(self.repository, "tag", "original-source")
        git(self.repository, "switch", "main")
        (self.repository / "target.md").write_text("target", encoding="utf-8")
        git(self.repository, "add", "target.md")
        git(self.repository, "commit", "-m", "target advanced")
        git(self.repository, "switch", "rebased")
        git(self.repository, "rebase", "main")
        git(self.repository, "switch", "main")
        git(self.repository, "merge", "--ff-only", "rebased")

        result = flow1c_git.merge_search(self.repository, ["original-source"], "main")[0]

        self.assertEqual(result["integration_status"], "SQUASH_OR_REBASE_MATCH")
        self.assertEqual(result["patch_coverage"], 1.0)

    def test_analysis_preserves_dirty_worktree_and_index(self) -> None:
        (self.repository / "Module.bsl").write_text("local modification\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("local", encoding="utf-8")
        before = git(self.repository, "status", "--porcelain=v1")

        flow1c_git.history_search(self.repository, "main", max_count=10)
        flow1c_git.merge_search(self.repository, ["missing-source"], "main")

        after = git(self.repository, "status", "--porcelain=v1")
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
