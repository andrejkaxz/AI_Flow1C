"""Argument parsing, result presentation and exit codes."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from flow1c import context as runtime
from flow1c import documents as documents
from flow1c import intake as intake_service
from flow1c import publication as publication
from flow1c import readiness as readiness
from flow1c import redmine as redmine
from flow1c import registry as registry_service
from flow1c import routing
from flow1c import handoff
from flow1c import context_manifest
from flow1c.context_policy import ContextError
from flow1c.routing_policy import MAX_PROPOSAL_BYTES, RoutingError
from flow1c import storage as storage
from flow1c import templates as templates_service
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import actions as actions_service
from flow1c.workflow import begin as begin_service
from flow1c.workflow import complete as completion
from flow1c.workflow import dialogue as dialogue_service
from flow1c.workflow import git_actions as git_actions
from flow1c.workflow import source_actions as source_actions
from flow1c.workflow import knowledge_actions
from scripts import flow1c_docx as docx
from scripts import flow1c_interview_policy as interview_policy
from scripts import flow1c_policy as policy
from scripts import flow1c_sections_policy as sections_policy
from scripts import flow1c_templates as template_library
from scripts import flow1c_templates_policy as template_policy

ROOT = Path(__file__).resolve().parents[1]


def configure_stdio_utf8() -> None:
    """Make the CLI JSON protocol UTF-8 even on Windows with an ANSI locale."""
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (io.UnsupportedOperation, ValueError):
                continue


def read_json_stdin() -> dict[str, Any]:
    try:
        request = json.load(sys.stdin)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Invalid JSON stdin: {exc}") from exc
    if not isinstance(request, dict):
        raise WorkflowError("JSON stdin must contain an object.")
    return storage.sanitize_json_value(request)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flow1C CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    catalog = subparsers.add_parser("route-catalog", help="Read the product route catalog")
    catalog.add_argument("--json", action="store_true")
    catalog.set_defaults(handler=cmd_route_catalog)
    route_check = subparsers.add_parser("route-check", help="Check one bounded RouteProposal without a gate")
    route_check.add_argument("--json-stdin", action="store_true", required=True)
    route_check.set_defaults(handler=cmd_route_check)
    doctor = subparsers.add_parser("doctor", help="Check local prerequisites and paths")
    doctor.add_argument(
        "--json", action="store_true", help="Emit a machine-readable readiness gate"
    )
    doctor.add_argument(
        "--format",
        choices=("docx", "markdown"),
        help="Selected template format for targeted readiness",
    )
    doctor.add_argument(
        "--gate-id", help="Infer the selected template format from a saved request pin"
    )
    doctor_scope = doctor.add_mutually_exclusive_group()
    doctor_scope.add_argument(
        "--profile", choices=runtime.PROFILE_NAMES + ("template-markdown", "template-docx")
    )
    doctor_scope.add_argument(
        "--operation", choices=tuple(runtime.load_stages(product_root=ROOT)["operations"])
    )
    doctor.set_defaults(handler=cmd_doctor)
    registry_import = subparsers.add_parser(
        "registry-import", help="Validate and normalize an Excel registry"
    )
    registry_import.add_argument("--file", required=True)
    registry_import.add_argument(
        "--allow-errors",
        action="store_true",
        help="Deprecated compatibility flag; partial indexes are written by default",
    )
    registry_import.set_defaults(handler=cmd_registry_import)
    registry_reconcile = subparsers.add_parser(
        "registry-reconcile", help="Reconcile a provisional work item with a corrected registry"
    )
    registry_reconcile.add_argument("--file", required=True)
    registry_reconcile.add_argument("--code", required=True, help="User-supplied work reference")
    registry_reconcile.add_argument(
        "--mappings-json", default="{}", help="Confirmed user-brief to registry ID mapping"
    )
    registry_reconcile.set_defaults(handler=cmd_registry_reconcile)
    fs_start = subparsers.add_parser(
        "fs-start", help="Create a work item for a user-supplied reference"
    )
    fs_start.add_argument("--task-reference")
    fs_start.add_argument("--project-reference")
    fs_start.add_argument("--code", help="Deprecated alias for --task-reference")
    fs_start.add_argument("--g-number", help=argparse.SUPPRESS)
    fs_start.add_argument("--reference-kind", choices=runtime.REFERENCE_KINDS, default="auto")
    fs_start.add_argument("--title")
    fs_start.add_argument(
        "--requirements", help="Comma-separated IDs when the FS is absent from the registry"
    )
    fs_start.add_argument("--create-branch", action="store_true")
    fs_start.set_defaults(handler=cmd_fs_start)
    context = subparsers.add_parser("context-build", help="Build a compact role-specific context")
    context.add_argument("--code", required=True)
    context.add_argument("--role", required=True, choices=runtime.VALID_ROLES)
    context.add_argument("--view", choices=("full", "compact"), default="full")
    context.add_argument("--gate-id")
    context.set_defaults(handler=cmd_context_build)
    agent_begin = subparsers.add_parser(
        "agent-begin", help="Create a machine-readable gate for a natural-language request"
    )
    agent_begin.add_argument("--json-stdin", action="store_true")
    agent_begin.add_argument(
        "--operation",
        choices=("ambiguous", *tuple(runtime.load_stages(product_root=ROOT)["operations"])),
    )
    agent_begin.add_argument("--code")
    agent_begin.add_argument("--task-reference")
    agent_begin.add_argument("--project-reference")
    agent_begin.add_argument("--reference-kind", choices=runtime.REFERENCE_KINDS, default="auto")
    agent_begin.add_argument("--git-ref")
    agent_begin.add_argument("--git-refs", nargs="*", default=[])
    agent_begin.add_argument("--target-ref", default="")
    agent_begin.add_argument("--refresh-git-refs", action="store_true")
    agent_begin.add_argument("--g-number", help=argparse.SUPPRESS)
    agent_begin.add_argument("--summary", default="")
    agent_begin.add_argument("--query-intent", choices=("create", "review", "optimize"))
    agent_begin.add_argument("--path", action="append", default=[])
    agent_begin.add_argument("--allow-incomplete-draft", action="store_true")
    agent_begin.add_argument("--mode", choices=policy.MODES, default=None)
    agent_begin.add_argument("--requirements", nargs="*", default=[])
    agent_begin.add_argument(
        "--mismatch",
        default="",
        help="Observed semantic mismatch; ask instead of silently remapping",
    )
    agent_begin.add_argument("--profile", choices=runtime.PROFILE_NAMES)
    agent_begin.set_defaults(handler=cmd_agent_begin, route_proposal=None)
    dialogue = subparsers.add_parser(
        "agent-dialogue", help="Persist a question, user answer or decision on the same request"
    )
    dialogue.add_argument("--json-stdin", action="store_true")
    dialogue.add_argument("--gate-id")
    dialogue.add_argument("--action", choices=("ask", "answer", "record", "deviate"))
    dialogue.add_argument("--question", default="")
    dialogue.add_argument("--answer", default="")
    dialogue.add_argument(
        "--kind",
        choices=("user_answer", "assumption", "open_question", "decision"),
        default="user_answer",
    )
    dialogue.add_argument("--deviation-type", choices=("process", "registry_bypass"))
    dialogue.add_argument("--actor", default="user", help=argparse.SUPPRESS)
    dialogue.add_argument("--scope", choices=("gate", "work-item"))
    dialogue.add_argument("--condition-ids", nargs="*", default=None)
    dialogue.add_argument("--user-statement", default="")
    dialogue.add_argument("--resolution", choices=("independent-draft", "correct-code"))
    dialogue.add_argument("--reference-kind", choices=runtime.REFERENCE_KINDS)
    dialogue.add_argument("--code")
    dialogue.add_argument("--mode", choices=("explore", "draft"))
    dialogue.add_argument("--profile", choices=runtime.PROFILE_NAMES)
    dialogue.set_defaults(handler=cmd_agent_dialogue)
    inspect = subparsers.add_parser(
        "agent-inspect", help="Bounded read/search of workflow or accepted request text"
    )
    inspect.add_argument("--json-stdin", action="store_true")
    inspect.add_argument("--gate-id")
    inspect.add_argument("--scope", choices=("workflow", "request"), default="request")
    inspect.add_argument("--path", default="")
    inspect.add_argument("--query", default="")
    inspect.add_argument("--start-line", type=int, default=1)
    inspect.add_argument("--max-chars", type=int, default=12000)
    inspect.set_defaults(handler=cmd_agent_inspect)
    knowledge = subparsers.add_parser("knowledge", help="Bounded project results and wiki operations")
    knowledge.add_argument("--json-stdin", action="store_true", required=True)
    knowledge.add_argument("--gate-id")
    knowledge.add_argument("--action", choices=tuple(knowledge_actions.FIELDS), default="navigation")
    knowledge.set_defaults(request={}, handler=cmd_knowledge)
    git_refresh = subparsers.add_parser(
        "agent-git-refresh", help="Refresh explicitly allowed remote-tracking refs"
    )
    git_refresh.add_argument("--json-stdin", action="store_true")
    git_refresh.add_argument("--gate-id")
    git_refresh.add_argument("--repository", choices=("workflow", "extension"), default="extension")
    git_refresh.add_argument("--remote", default="origin")
    git_refresh.add_argument("--refs", nargs="*", default=[])
    git_refresh.set_defaults(handler=cmd_agent_git_refresh)
    git_inspect = subparsers.add_parser(
        "agent-git-inspect", help="Read Git refs, history and diffs for a free review"
    )
    git_inspect.add_argument("--json-stdin", action="store_true")
    git_inspect.add_argument("--gate-id")
    git_inspect.add_argument("--repository", choices=("workflow", "extension"), default="extension")
    git_inspect.add_argument(
        "--action",
        choices=(
            "resolve",
            "log",
            "latest-merge",
            "integration",
            "merge-search",
            "branch-changes",
            "history-search",
            "diff",
            "read-at-ref",
        ),
        default="resolve",
    )
    git_inspect.add_argument("--git-ref", default="")
    git_inspect.add_argument("--git-refs", nargs="*", default=[])
    git_inspect.add_argument("--base-ref", default="")
    git_inspect.add_argument("--target-ref", default="")
    git_inspect.add_argument(
        "--detail",
        choices=("names", "stat", "patch", "first-parent-patch", "remerge-diff", "combined"),
        default="patch",
    )
    git_inspect.add_argument("--paths", nargs="*", default=[])
    git_inspect.add_argument("--path", default="")
    git_inspect.add_argument("--cursor", default="")
    git_inspect.add_argument("--max-files", type=int, default=100)
    git_inspect.add_argument("--start-line", type=int, default=1)
    git_inspect.add_argument("--subject-query", default="")
    git_inspect.add_argument("--regex", action="store_true")
    git_inspect.add_argument("--merges-only", action="store_true")
    git_inspect.add_argument("--first-parent", action="store_true")
    git_inspect.add_argument("--min-parents", type=int)
    git_inspect.add_argument("--max-parents", type=int)
    git_inspect.add_argument("--since", default="")
    git_inspect.add_argument("--until", default="")
    git_inspect.add_argument(
        "--include-pr-evidence", action=argparse.BooleanOptionalAction, default=True
    )
    git_inspect.add_argument(
        "--include-patch-evidence", action=argparse.BooleanOptionalAction, default=True
    )
    git_inspect.add_argument("--max-count", type=int, default=20)
    git_inspect.add_argument("--max-chars", type=int, default=40000)
    git_inspect.set_defaults(handler=cmd_agent_git_inspect_v2)
    git_snapshot = subparsers.add_parser(
        "agent-git-snapshot", help="Create an isolated historical Git snapshot"
    )
    git_snapshot.add_argument("--json-stdin", action="store_true")
    git_snapshot.add_argument("--gate-id")
    git_snapshot.add_argument(
        "--repository", choices=("workflow", "extension"), default="extension"
    )
    git_snapshot.add_argument("--action", choices=("create", "cleanup"), default="create")
    git_snapshot.add_argument("--git-ref", required=False, default="")
    git_snapshot.add_argument("--paths", nargs="*", default=[])
    git_snapshot.add_argument("--ttl-hours", type=int, default=168)
    git_snapshot.set_defaults(handler=cmd_agent_git_snapshot)
    promote = subparsers.add_parser(
        "draft-promote", help="Attach a completed free draft without changing status or approvals"
    )
    promote.add_argument("--json-stdin", action="store_true")
    promote.add_argument("--gate-id")
    promote.add_argument("--code")
    promote.add_argument("--task-reference")
    promote.add_argument("--project-reference")
    promote.add_argument("--g-number", help=argparse.SUPPRESS)
    promote.add_argument("--requirements", nargs="*", default=[])
    promote.add_argument("--confirmed", action="store_true")
    promote.set_defaults(handler=cmd_draft_promote)
    intake = subparsers.add_parser(
        "artifact-intake", help="Validate and copy user artifacts into a controlled intake"
    )
    intake.add_argument("--json-stdin", action="store_true")
    intake.add_argument("--gate-id")
    intake.add_argument("--source", action="append", default=[])
    intake.add_argument("--code")
    intake.add_argument(
        "--category",
        choices=tuple(runtime.load_stages(product_root=ROOT).get("artifact_categories", {})),
    )
    intake.add_argument("--confirm-absence", action="append", default=[])
    intake.add_argument("--confirm-large", action="store_true")
    intake.add_argument("--received-via", choices=("chat-attachment", "file", "folder"), default="")
    intake.add_argument("--promote-intake-id")
    intake.set_defaults(handler=cmd_artifact_intake)
    agent_context = subparsers.add_parser(
        "agent-context", help="Build and return a gated role context"
    )
    agent_context.add_argument("--json-stdin", action="store_true")
    agent_context.add_argument("--gate-id")
    agent_context.add_argument("--view", choices=("full", "compact"), default="full")
    agent_context.add_argument("--action", choices=("build", "read"), default="build")
    agent_context.add_argument("--entry-id", default="")
    agent_context.add_argument("--section-id", default="")
    agent_context.add_argument("--cursor", default="")
    agent_context.add_argument("--max-chars", type=int)
    agent_context.set_defaults(handler=cmd_agent_context)

    context_read = subparsers.add_parser("context-read", help="Read one gate-owned context manifest entry or part")
    context_read.add_argument("--json-stdin", action="store_true")
    context_read.add_argument("--gate-id")
    context_read.add_argument("--entry-id", default="")
    context_read.add_argument("--section-id", default="")
    context_read.add_argument("--cursor", default="")
    context_read.add_argument("--max-chars", type=int)
    context_read.set_defaults(action="read", handler=cmd_context_read)
    agent_diff = subparsers.add_parser(
        "agent-diff", help="Return and record a bounded extension diff"
    )
    agent_diff.add_argument("--json-stdin", action="store_true")
    agent_diff.add_argument("--gate-id")
    agent_diff.add_argument("--max-chars", type=int, default=120000)
    agent_diff.set_defaults(handler=cmd_agent_diff)
    source_read = subparsers.add_parser(
        "agent-source-read", help="Read a gated extension source file"
    )
    source_read.add_argument("--json-stdin", action="store_true")
    source_read.add_argument("--gate-id")
    source_read.add_argument(
        "--source", choices=("extension", "configuration"), default="extension"
    )
    source_read.add_argument("--path")
    source_read.add_argument("--max-chars", type=int, default=80000)
    source_read.set_defaults(handler=cmd_agent_source_read)
    source_query = subparsers.add_parser(
        "source-query", help="Query indexed BSL sources and record evidence"
    )
    source_query.add_argument("--json-stdin", action="store_true")
    source_query.add_argument("--gate-id")
    source_query.add_argument("--source")
    source_query.add_argument("--query")
    source_query.add_argument("--code")
    source_query.add_argument("--reason")
    source_query.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
    source_query.add_argument("--max-chars", type=int, default=12000)
    source_query.set_defaults(handler=cmd_source_query)
    cc_inspect = subparsers.add_parser(
        "cc-inspect", help="Run a bounded read-only cc-1c-skills metadata inspector"
    )
    cc_inspect.add_argument("--json-stdin", action="store_true")
    cc_inspect.add_argument("--gate-id")
    cc_inspect.add_argument("--source", choices=("request", "configuration", "extension"))
    cc_inspect.add_argument(
        "--operation",
        choices=(
            "meta-overview",
            "meta-full",
            "meta-item",
            "skd-overview",
            "skd-query",
            "skd-fields",
            "skd-params",
            "skd-links",
            "skd-full",
        ),
    )
    cc_inspect.add_argument("--path")
    cc_inspect.add_argument("--name", default="")
    cc_inspect.add_argument("--max-chars", type=int, default=12000)
    cc_inspect.set_defaults(handler=cmd_cc_inspect)
    query_schema = subparsers.add_parser(
        "query-schema", help="Extract exact fields from one gated 1C XML export"
    )
    query_schema.add_argument("--json-stdin", action="store_true")
    query_schema.add_argument("--gate-id")
    query_schema.add_argument("--source", choices=("request", "configuration", "extension"))
    query_schema.add_argument("--path")
    query_schema.add_argument("--rlm-evidence-id", default="")
    query_schema.set_defaults(handler=cmd_query_schema)
    query_check = subparsers.add_parser(
        "query-check", help="Save and statically inspect the final 1C query text"
    )
    query_check.add_argument("--json-stdin", action="store_true")
    query_check.add_argument("--gate-id")
    query_check.add_argument("--text", default="")
    query_check.add_argument("--baseline-text", default="")
    query_check.add_argument("--schema-ids", nargs="*", default=[])
    query_check.add_argument("--expected-result", default="")
    query_check.add_argument("--assumptions", nargs="*", default=[])
    query_check.add_argument("--changes", nargs="*", default=[])
    query_check.set_defaults(handler=cmd_query_check)
    agent_write = subparsers.add_parser(
        "agent-write", help="Write only within the target permitted by a gate"
    )
    agent_write.add_argument("--json-stdin", action="store_true")
    agent_write.add_argument("--gate-id")
    agent_write.add_argument("--target", choices=("work-item", "extension", "draft"))
    agent_write.add_argument("--path")
    agent_write.add_argument("--content", default="")
    agent_write.add_argument("--content-stdin", action="store_true")
    agent_write.set_defaults(handler=cmd_agent_write)
    analyze = subparsers.add_parser(
        "agent-analyze-bsl", help="Run BSL Language Server and record evidence"
    )
    analyze.add_argument("--json-stdin", action="store_true")
    analyze.add_argument("--gate-id")
    analyze.add_argument("--source", type=json.loads, help="Typed BSL source selector")
    analyze.set_defaults(handler=cmd_agent_analyze_bsl)
    complete = subparsers.add_parser(
        "agent-complete", help="Validate evidence and close an agent gate"
    )
    complete.add_argument("--json-stdin", action="store_true")
    complete.add_argument("--gate-id")
    complete.add_argument("--output")
    complete.add_argument("--summary", default="")
    complete.set_defaults(handler=cmd_agent_complete)
    transfer = subparsers.add_parser("agent-handoff", help="Read or recover a completed gate handoff without replaying actions")
    transfer.add_argument("--json-stdin", action="store_true")
    transfer.add_argument("--gate-id")
    transfer.add_argument("--action", choices=("read", "recover"), default="read")
    transfer.set_defaults(handler=cmd_agent_handoff)
    action = subparsers.add_parser("agent-action", help="Run a gated workflow action")
    action.add_argument("--json-stdin", action="store_true")
    action.add_argument("--gate-id")
    action.add_argument(
        "--action",
        choices=(
            "setup-audit",
            "setup-plan",
            "setup-bootstrap",
            "setup-configure",
            "setup-status",
            "setup-resume",
            "doctor",
            "update",
            "update-diagnose",
            "registry-import",
            "fs-start",
            "provisional-start",
            "registry-reconcile",
            "status",
            "publish",
        ),
    )
    action.add_argument("--parameters-json", default="{}")
    action.set_defaults(handler=cmd_agent_action)
    redmine = subparsers.add_parser(
        "redmine", help="Configure Redmine, inspect issues, or explicitly upload a DMSF document"
    )
    redmine_commands = redmine.add_subparsers(dest="redmine_command", required=True)
    redmine_configure = redmine_commands.add_parser(
        "configure", help="Connect this workflow checkout to Redmine"
    )
    redmine_configure.add_argument(
        "--url", required=True, help="Redmine HTTPS base URL, optionally with a path prefix"
    )
    redmine_configure.set_defaults(handler=cmd_redmine_configure)
    redmine_status = redmine_commands.add_parser(
        "status", help="Show local Redmine connection readiness"
    )
    redmine_status.set_defaults(handler=cmd_redmine_status)
    redmine_test = redmine_commands.add_parser(
        "test", help="Test the configured API key with Redmine"
    )
    redmine_test.set_defaults(handler=cmd_redmine_test)
    redmine_disconnect = redmine_commands.add_parser(
        "disconnect", help="Remove this checkout's Redmine configuration"
    )
    redmine_disconnect.set_defaults(handler=cmd_redmine_disconnect)
    redmine_cleanup = redmine_commands.add_parser(
        "cleanup", help="Retry removal of saved Redmine credentials"
    )
    redmine_cleanup.add_argument(
        "--url", help="Optional HTTPS URL whose saved credential should be removed"
    )
    redmine_cleanup.set_defaults(handler=cmd_redmine_cleanup)
    redmine_files = redmine_commands.add_parser(
        "files", help="List standard and currently attached DMSF issue files without downloading"
    )
    redmine_files.add_argument("issue", help="Numeric Redmine issue number")
    redmine_files.add_argument(
        "--json", action="store_true", help="Emit a machine-readable file inventory"
    )
    redmine_files.set_defaults(handler=cmd_redmine_files)
    redmine_relations = redmine_commands.add_parser(
        "relations", help="Show direct issue relations with related issue tracker and status"
    )
    redmine_relations.add_argument("issue", help="Numeric Redmine issue number")
    redmine_relations.add_argument(
        "--json", action="store_true", help="Emit a machine-readable relation inventory"
    )
    redmine_relations.set_defaults(handler=cmd_redmine_relations)
    redmine_upload = redmine_commands.add_parser(
        "upload", help="Upload one local file to DMSF and attach it to an issue"
    )
    redmine_upload.add_argument("issue", help="Numeric Redmine issue number")
    redmine_upload.add_argument("--file", required=True, help="Explicit local file path to upload")
    redmine_upload.add_argument(
        "--confirmed",
        action="store_true",
        help="Confirm this exact issue and local file for the external write",
    )
    redmine_upload.add_argument(
        "--json", action="store_true", help="Emit a machine-readable upload result"
    )
    redmine_upload.set_defaults(handler=cmd_redmine_upload)
    redmine_revise = redmine_commands.add_parser(
        "revise", help="Add a revision to one DMSF document attached to an issue"
    )
    redmine_revise.add_argument("issue", help="Numeric Redmine issue number")
    redmine_revise.add_argument("--dms-file", required=True, help="Exact existing DMSF file ID")
    redmine_revise.add_argument(
        "--expected-revision", required=True, help="Current revision ID for race protection"
    )
    redmine_revise.add_argument(
        "--file", required=True, help="Local replacement file with the same name"
    )
    redmine_revise.add_argument(
        "--confirmed", action="store_true", help="Confirm revision of this exact document"
    )
    redmine_revise.add_argument(
        "--json", action="store_true", help="Emit a machine-readable result"
    )
    redmine_revise.set_defaults(handler=cmd_redmine_revise)
    redmine_fetch = redmine_commands.add_parser(
        "fetch", help="Find an issue by number and import its supported attachments"
    )
    redmine_fetch.add_argument("issue", help="Numeric Redmine issue number")
    redmine_fetch.add_argument(
        "--code", help="Existing Flow1C task reference; omit to place files in inbox"
    )
    redmine_fetch.add_argument(
        "--gate-id", help="Active Flow1C gate; derives and validates the target work item"
    )
    redmine_fetch.add_argument(
        "--dms-file",
        action="append",
        default=[],
        help="Explicit DMSF file ID to import; repeat for multiple files",
    )
    redmine_fetch.add_argument(
        "--dms-revision", help="DMSF revision ID; requires exactly one --dms-file"
    )
    redmine_fetch.add_argument(
        "--all-dms",
        action="store_true",
        help="Import every DMSF file currently attached to the issue",
    )
    redmine_fetch.set_defaults(handler=cmd_redmine_fetch)
    status = subparsers.add_parser("status", help="Show or write the project status")
    status.add_argument("--write", action="store_true")
    status.set_defaults(handler=cmd_status)
    commit = subparsers.add_parser("git-commit", help="Commit only files allowed for one work item")
    commit.add_argument("--code", required=True)
    commit.add_argument("--message", required=True)
    commit.add_argument("--include-registry", action="store_true")
    commit.set_defaults(handler=cmd_git_commit)
    pr = subparsers.add_parser("pr-create", help="Push the current branch and create a Gitea PR")
    pr.add_argument("--code", required=True)
    pr.add_argument("--phase", required=True, choices=("specification", "technical", "acceptance"))
    pr.add_argument("--title")
    pr.set_defaults(handler=cmd_pr_create)
    section_catalog = subparsers.add_parser(
        "section-catalog", help="List canonical functional-specification sections"
    )
    section_catalog.add_argument("--json-stdin", action="store_true")
    section_catalog.add_argument("--gate-id")
    section_catalog.set_defaults(handler=cmd_section_catalog)
    section_save = subparsers.add_parser(
        "section-save", help="Save a versioned functional-specification section draft"
    )
    section_save.add_argument("--json-stdin", action="store_true")
    section_save.add_argument("--gate-id")
    section_save.add_argument("--request-id")
    section_save.add_argument("--work-reference")
    section_save.add_argument("--section-id", action="append", default=[])
    section_save.add_argument("--sections", nargs="*", default=[])
    section_save.add_argument("--content", default="")
    section_save.add_argument("--content-stdin", action="store_true")
    section_save.add_argument("--checklist", default="[]")
    section_save.add_argument("--sources", nargs="*", default=[])
    section_save.set_defaults(handler=cmd_section_save)
    section_approve = subparsers.add_parser(
        "section-approve", help="Record explicit approval of the current section hash"
    )
    section_approve.add_argument("--json-stdin", action="store_true")
    section_approve.add_argument("--gate-id")
    section_approve.add_argument("--request-id")
    section_approve.add_argument("--work-reference")
    section_approve.add_argument("--section-id", action="append", default=[])
    section_approve.add_argument("--sections", nargs="*", default=[])
    section_approve.add_argument("--approved-by", default="user")
    section_approve.add_argument("--approval-statement", required=False, default="")
    section_approve.set_defaults(handler=cmd_section_approve)
    docx_inspect = subparsers.add_parser(
        "docx-inspect", help="Inspect a selected DOCX and locate section anchors"
    )
    docx_inspect.add_argument("--json-stdin", action="store_true")
    docx_inspect.add_argument("--gate-id")
    docx_inspect.add_argument("--source", default="")
    docx_inspect.add_argument("--path", default="")
    docx_inspect.add_argument("--section-id", action="append", default=[])
    docx_inspect.add_argument("--sections", nargs="*", default=[])
    docx_inspect.set_defaults(handler=cmd_docx_inspect)
    docx_plan = subparsers.add_parser(
        "docx-write-plan", help="Create a checked, explicit DOCX write plan"
    )
    docx_plan.add_argument("--json-stdin", action="store_true")
    docx_plan.add_argument("--gate-id")
    docx_plan.add_argument("--source", default="")
    docx_plan.add_argument("--path", default="")
    docx_plan.add_argument("--section-id", action="append", default=[])
    docx_plan.add_argument("--sections", nargs="*", default=[])
    docx_plan.add_argument("--request-id")
    docx_plan.add_argument("--work-reference")
    docx_plan.add_argument("--mode", choices=("replace", "append"), default="")
    docx_plan.add_argument("--modes", default="")
    docx_plan.add_argument("--output", default="")
    docx_plan.add_argument("--plan", default="")
    docx_plan.set_defaults(handler=cmd_docx_write_plan)
    docx_write = subparsers.add_parser(
        "docx-write", help="Write a previously checked DOCX plan to a new file"
    )
    docx_write.add_argument("--json-stdin", action="store_true")
    docx_write.add_argument("--gate-id")
    docx_write.add_argument("--plan", default="")
    docx_write.set_defaults(handler=cmd_docx_write)
    interview = subparsers.add_parser(
        "interview-register",
        help="Inspect, audit or write an interview XLSX draft without overwriting sources",
    )
    interview.add_argument("--json-stdin", action="store_true")
    interview.add_argument("--gate-id")
    interview.add_argument("--action", choices=("inspect", "audit", "write"), default="inspect")
    interview.set_defaults(request={}, handler=cmd_interview_register)
    register_template_parser(subparsers)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "route-check":
            return cmd_route_check(args)
        if getattr(args, "json_stdin", False):
            if args.command == "knowledge":
                raw = sys.stdin.read(280001)
                if len(raw.encode("utf-8")) > 280000:
                    raise WorkflowError("Knowledge input exceeds 280000 bytes")
                try:
                    request = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise WorkflowError("Invalid knowledge JSON stdin") from exc
                if not isinstance(request, dict) or set(request) - {"gate_id", "action", "request"}:
                    raise WorkflowError("Knowledge input accepts only gate_id, action and request")
                request = storage.sanitize_json_value(request)
            else:
                request = read_json_stdin()
            if args.command == "agent-handoff" and set(request) - {"gate_id", "action"}:
                raise WorkflowError("Handoff input accepts only gate_id and action")
            if args.command in {"agent-context", "context-read"}:
                context_fields = {"gate_id", "entry_id", "section_id", "cursor", "max_chars"}
                if args.command == "agent-context":
                    context_fields |= {"action", "view"}
                if set(request) - context_fields:
                    raise ContextError("CONTEXT_INVALID", "Unknown context input field")
            args._json_input_fields = set(request)
            for key, value in request.items():
                attribute = str(key).replace("-", "_")
                if not hasattr(args, attribute):
                    raise WorkflowError(f"Unknown JSON input field: {key}")
                setattr(args, attribute, value)
        if getattr(args, "g_number", None):
            print(
                "WARNING: --g-number is deprecated; use --task-reference. The value is treated as an opaque user reference.",
                file=sys.stderr,
            )
        return int(args.handler(args))
    except (RoutingError, ContextError) as exc:
        print(json.dumps({"schema_version": 1, "state": "BLOCKED", "ready": False,
                          "errors": [exc.as_dict()]}, ensure_ascii=False, indent=2))
        return 2
    except redmine.RedmineOperationError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, indent=2))
        return 2
    except (
        sections_policy.SectionPolicyError,
        docx.DocxError,
        template_policy.TemplateError,
        interview_policy.InterviewError,
    ) as exc:
        payload = {
            "schema_version": 1,
            "state": "BLOCKED",
            "ready": False,
            "errors": [exc.as_dict()],
        }
        if getattr(args, "json_stdin", False) or getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except WorkflowError as exc:
        if getattr(args, "json_stdin", False) or getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state": "BLOCKED",
                        "ready": False,
                        "errors": [
                            {
                                "code": "WORKFLOW_ERROR",
                                "message": str(exc),
                                "recoverable": True,
                                "next_action": "Correct the reported input or prerequisite and retry the same operation.",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        if getattr(args, "json_stdin", False) or getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state": "BLOCKED",
                        "ready": False,
                        "errors": [
                            {
                                "code": "COMMAND_FAILED",
                                "message": detail,
                                "recoverable": True,
                                "next_action": "Inspect the failed command output, correct the environment, and retry.",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"ERROR: command failed: {detail}", file=sys.stderr)
        return 2


def cmd_agent_action(args: argparse.Namespace) -> int:
    result = actions_service.agent_action(args, product_root=ROOT)
    return emit_result(result)


def cmd_knowledge(args: argparse.Namespace) -> int:
    result = knowledge_actions.command(args, product_root=ROOT)
    # The bound includes the serialized envelope, not only excerpts.
    print(json.dumps(result.value, ensure_ascii=False, separators=(",", ":")))
    return result.exit_code


def cmd_route_catalog(args: argparse.Namespace) -> int:
    return emit_result(OperationResult(routing.route_catalog(product_root=ROOT)))


def cmd_route_check(args: argparse.Namespace) -> int:
    # Read a bounded direct proposal rather than assigning its keys to CLI attributes.
    raw = sys.stdin.read(MAX_PROPOSAL_BYTES + 1)
    try:
        if len(raw.encode("utf-8")) > MAX_PROPOSAL_BYTES:
            raise ValueError("RouteProposal exceeds 32768 bytes")
        proposal = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowError("Invalid bounded RouteProposal JSON stdin") from exc
    decision = routing.check_proposal(proposal, product_root=ROOT)
    return emit_result(OperationResult(decision, 0 if decision["status"] == "VALID" else
                                      1 if decision["status"] == "CLARIFICATION_REQUIRED" else 2))


def cmd_agent_analyze_bsl(args: argparse.Namespace) -> int:
    result = source_actions.agent_analyze_bsl(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_begin(args: argparse.Namespace) -> int:
    result = begin_service.agent_begin(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_complete(args: argparse.Namespace) -> int:
    result = completion.agent_complete(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_handoff(args: argparse.Namespace) -> int:
    return emit_result(handoff.command(args.gate_id, args.action, product_root=ROOT))


def cmd_agent_context(args: argparse.Namespace) -> int:
    result = dialogue_service.agent_context(args, product_root=ROOT)
    return emit_result(result)


def cmd_context_read(args: argparse.Namespace) -> int:
    return emit_result(context_manifest.command(args, product_root=ROOT))


def cmd_agent_dialogue(args: argparse.Namespace) -> int:
    result = dialogue_service.agent_dialogue(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_diff(args: argparse.Namespace) -> int:
    result = actions_service.agent_diff(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_git_inspect_v2(args: argparse.Namespace) -> int:
    result = git_actions.agent_git_inspect_v2(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_git_refresh(args: argparse.Namespace) -> int:
    result = git_actions.agent_git_refresh(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_git_snapshot(args: argparse.Namespace) -> int:
    result = git_actions.agent_git_snapshot(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_inspect(args: argparse.Namespace) -> int:
    result = actions_service.agent_inspect(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_source_read(args: argparse.Namespace) -> int:
    result = source_actions.agent_source_read(args, product_root=ROOT)
    return emit_result(result)


def cmd_agent_write(args: argparse.Namespace) -> int:
    result = actions_service.agent_write(args, product_root=ROOT)
    return emit_result(result)


def cmd_artifact_intake(args: argparse.Namespace) -> int:
    result = intake_service.artifact_intake(args, product_root=ROOT)
    return emit_result(result)


def cmd_cc_inspect(args: argparse.Namespace) -> int:
    result = source_actions.cc_inspect(args, product_root=ROOT)
    return emit_result(result)


def cmd_context_build(args: argparse.Namespace) -> int:
    result = dialogue_service.context_build(args, product_root=ROOT)
    return emit_result(result)


def cmd_doctor(args: argparse.Namespace) -> int:
    result = readiness.doctor(args, product_root=ROOT)
    return render_doctor(
        result,
        json_output=bool(getattr(args, "json", False)),
        aligned=not (getattr(args, "profile", None) or getattr(args, "operation", None)),
    )


def cmd_docx_inspect(args: argparse.Namespace) -> int:
    result = documents.docx_inspect(args, product_root=ROOT)
    return emit_result(result)


def cmd_docx_write(args: argparse.Namespace) -> int:
    result = documents.docx_write(args, product_root=ROOT)
    return emit_result(result)


def cmd_docx_write_plan(args: argparse.Namespace) -> int:
    result = documents.docx_write_plan(args, product_root=ROOT)
    return emit_result(result)


def cmd_draft_promote(args: argparse.Namespace) -> int:
    result = actions_service.draft_promote(args, product_root=ROOT)
    return emit_result(result)


def cmd_fs_start(args: argparse.Namespace) -> int:
    result = work_items.fs_start(args, product_root=ROOT)
    print(work_items.describe_created_work_item(result.value))
    return result.exit_code


def cmd_git_commit(args: argparse.Namespace) -> int:
    result = publication.git_commit(args, product_root=ROOT)
    return emit_result(result)


def cmd_interview_register(args: argparse.Namespace) -> int:
    result = documents.interview_register(args, product_root=ROOT)
    return emit_result(result)


def cmd_pr_create(args: argparse.Namespace) -> int:
    result = publication.pr_create(args, product_root=ROOT)
    response = result.value
    print(
        response.get("html_url") or response.get("url") or json.dumps(response, ensure_ascii=False)
    )
    return result.exit_code


def cmd_profile_doctor(profile: str, *, json_output: bool) -> int:
    result = readiness.profile_doctor(profile, product_root=ROOT)
    return render_doctor(result, json_output=json_output, aligned=False)


def cmd_query_check(args: argparse.Namespace) -> int:
    result = source_actions.query_check(args, product_root=ROOT)
    return emit_result(result)


def cmd_query_schema(args: argparse.Namespace) -> int:
    result = source_actions.query_schema(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_cleanup(args: argparse.Namespace) -> int:
    result = redmine.redmine_cleanup(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_configure(args: argparse.Namespace) -> int:
    result = redmine.redmine_configure(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_disconnect(args: argparse.Namespace) -> int:
    result = redmine.redmine_disconnect(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_fetch(args: argparse.Namespace) -> int:
    result = redmine.redmine_fetch(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_files(args: argparse.Namespace) -> int:
    result = redmine.redmine_files(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_relations(args: argparse.Namespace) -> int:
    result = redmine.redmine_relations(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_revise(args: argparse.Namespace) -> int:
    result = redmine.redmine_revise(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_status(args: argparse.Namespace) -> int:
    result = redmine.redmine_status(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_test(args: argparse.Namespace) -> int:
    result = redmine.redmine_test(args, product_root=ROOT)
    return emit_result(result)


def cmd_redmine_upload(args: argparse.Namespace) -> int:
    result = redmine.redmine_upload(args, product_root=ROOT)
    return emit_result(result)


def cmd_registry_import(args: argparse.Namespace) -> int:
    result = registry_service.registry_import(args, product_root=ROOT)
    return emit_result(result)


def cmd_registry_reconcile(args: argparse.Namespace) -> int:
    result = work_items.registry_reconcile(args, product_root=ROOT)
    return emit_result(result)


def cmd_section_approve(args: argparse.Namespace) -> int:
    result = documents.section_approve(args, product_root=ROOT)
    return emit_result(result)


def cmd_section_catalog(args: argparse.Namespace) -> int:
    result = documents.section_catalog(args, product_root=ROOT)
    return emit_result(result)


def cmd_section_save(args: argparse.Namespace) -> int:
    result = documents.section_save(args, product_root=ROOT)
    return emit_result(result)


def cmd_source_query(args: argparse.Namespace) -> int:
    result = source_actions.source_query(args, product_root=ROOT)
    return emit_result(result)


def cmd_status(args: argparse.Namespace) -> int:
    result = publication.status(args, product_root=ROOT)
    print(result.value, end="\n" if args.write else "")
    return result.exit_code


def emit_result(result: OperationResult) -> int:
    if isinstance(result.value, (dict, list)):
        print(json.dumps(result.value, ensure_ascii=False, indent=2))
    elif result.value is not None:
        print(result.value)
    return result.exit_code


def render_doctor(result: OperationResult, *, json_output: bool, aligned: bool) -> int:
    value = result.value
    if json_output or value.get("operation") in {"template-management", "template-document"}:
        return emit_result(result)
    checks = value.get("checks", [])
    width = max((len(item["name"]) for item in checks), default=0)
    for item in checks:
        name, state, detail = (item["name"], item["state"], item["detail"])
        print(f"{name:<{width}}  {state:<8}  {detail}" if aligned else f"{name}  {state}  {detail}")
    return result.exit_code


def emit_doctor_report(rows: list[tuple[str, str, str]], *, json_output: bool = False) -> int:
    return render_doctor(readiness.doctor_report(rows), json_output=json_output, aligned=True)


def cmd_template(args: argparse.Namespace) -> int:
    return emit_result(templates_service.command(args, product_root=ROOT))


def register_template_parser(subparsers: Any) -> None:
    template = subparsers.add_parser("template", help="Manage immutable project template revisions")
    template.add_argument(
        "action", choices=template_library.ACTIONS + template_library.DOCUMENT_ACTIONS
    )
    template.add_argument("--json-stdin", action="store_true")
    template.add_argument("--gate-id")
    template.add_argument("--json", action="store_true")
    template.set_defaults(request=None, **{key: None for key in templates_service.INPUT_FIELDS})
    template.set_defaults(handler=cmd_template)
    for action in template_library.DOCUMENT_ACTIONS:
        document_parser = subparsers.add_parser(
            action, help="Write a document through a pinned template draft gate"
        )
        document_parser.add_argument("--json-stdin", action="store_true")
        document_parser.add_argument("--gate-id")
        document_parser.set_defaults(
            action=action, request=None, **{key: None for key in templates_service.INPUT_FIELDS}
        )
        document_parser.set_defaults(handler=cmd_template)
