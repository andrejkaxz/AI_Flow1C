"""Isolated, multi-turn OpenCode server evals. No live project mutations.

HTTP contract: https://opencode.ai/docs/server/ and the official v2 SDK question types.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"read", "glob", "list", "edit", "write", "apply_patch", "task", "webfetch", "websearch"}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=FLOW1C Eval", "-c", "user.email=flow1c-eval@example.invalid", *args],
                          cwd=root, check=True, capture_output=True, timeout=30).stdout


def tree_hash(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


def build_fixture(destination: Path, case: dict, *, source=ROOT, baseline_ref=None):
    destination.mkdir(parents=True, exist_ok=False)
    workflow, docs, extension = (destination / name for name in ("workflow", "documentation", "extension"))
    workflow.mkdir()
    if baseline_ref:
        archive = git(source, "archive", "--format=zip", baseline_ref)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            for member in bundle.infolist():
                target = (workflow / member.filename).resolve()
                if not target.is_relative_to(workflow.resolve()):
                    raise ValueError("Unsafe baseline archive path")
            bundle.extractall(workflow)
    else:
        for name in (".agents", ".claude", ".opencode", "flow1c", "scripts", "config", "schemas", "standards", "templates", "docs", "README.md", "AGENTS.md", "CLAUDE.md", "opencode.json", ".gitignore"):
            src = source / name
            if src.is_dir():
                shutil.copytree(src, workflow / name, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
            elif src.is_file():
                shutil.copy2(src, workflow / name)
    docs.mkdir()
    extension.mkdir()
    (extension / "Module.bsl").write_text("Процедура Тест()\nКонецПроцедуры\n", encoding="utf-8")
    # Never copy local settings, tokens, external source paths or live repository URLs.
    write_json(workflow / ".flow1c.json", {"version": 1, "project": {"default_branch": "main"},
        "quality": {"rlm_endpoint": "http://127.0.0.1:1/mcp"}, "gitea": {"base_url": "http://127.0.0.1:1", "owner": "fixture", "repository": "fixture"}})
    write_json(workflow / ".flow1c.local.json", {"documentation_path": str(docs), "extension_path": str(extension), "extension_mode": "local-export"})
    if case.get("fixture") == "mismatch":
        item = docs / "work-items/G-035"
        write_json(item / "manifest.yaml", {"schema_version": 1, "code": "G-035", "title": "Приёмка товара",
            "status": "clarification", "requirements": ["REQ-001"], "approvals": {}})
        write_json(item / "input/requirements.snapshot.yaml", {"REQ-001": {"text": "Приёмка товара на склад"}})
    if case.get("fixture") == "formal-existing-work-item":
        item = docs / "work-items/USER-FORMAL-1"
        write_json(item / "manifest.yaml", {"schema_version": 1, "code": "USER-FORMAL-1", "title": "Формальная спецификация",
            "status": "clarification", "requirements": ["REQ-001"], "approvals": {"functional_architect": "pending"},
            "traceability_mode": "registry", "requirement_basis": {"type": "registry_snapshot", "sha256": ""}})
        write_json(item / "input/requirements.snapshot.yaml", {"REQ-001": {"text": "Приёмка товара на склад"}})
        write_json(item / "input/artifacts.json", {"artifacts": [], "confirmed_absent": []})
    materials = destination / "arbitrary materials"
    materials.mkdir()
    (materials / "process.md").write_text("Кладовщик сверяет количество по накладной, фиксирует расхождения и передаёт их руководителю склада.", encoding="utf-8")
    template = materials / "Template.xml"
    template.write_text("<?xml version=\"1.0\" encoding=\"UTF-8\"?><DataCompositionSchema><dataSets/></DataCompositionSchema>", encoding="utf-8")
    for repo in (workflow, docs, extension):
        git(repo, "init", "-b", "main")
        git(repo, "add", ".")
        git(repo, "commit", "--allow-empty", "-m", "eval fixture")
    fixture_expected = {}
    if str(case.get("fixture", "")).startswith("git-"):
        git(extension, "switch", "-c", "iss/9154")
        (extension / "Module.bsl").write_text("Процедура Тест9154()\nКонецПроцедуры\n", encoding="utf-8")
        git(extension, "add", "Module.bsl")
        git(extension, "commit", "-m", "refs #9154 feature")
        git(extension, "switch", "main")
        git(extension, "merge", "--no-ff", "iss/9154", "-m", "merge iss/9154")
        fixture_expected["iss/9154"] = git(extension, "rev-parse", "HEAD").decode().strip()
        if case.get("fixture") == "git-post-merge":
            git(extension, "switch", "iss/9154")
            (extension / "post-merge.md").write_text("later", encoding="utf-8")
            git(extension, "add", "post-merge.md")
            git(extension, "commit", "-m", "later source work")
            git(extension, "switch", "main")
        git(extension, "switch", "-c", "iss/9728")
        (extension / "Module.bsl").write_text("Процедура Тест9728()\nКонецПроцедуры\n", encoding="utf-8")
        git(extension, "add", "Module.bsl")
        git(extension, "commit", "-m", "refs #9728 feature")
        git(extension, "switch", "main")
        git(extension, "merge", "--no-ff", "iss/9728", "-m", "merge iss/9728")
        fixture_expected["iss/9728"] = git(extension, "rev-parse", "HEAD").decode().strip()
        if case.get("fixture") == "git-large-diff":
            git(extension, "switch", "-c", "iss/large")
            for index in range(40):
                target = extension / "Documents" / f"Object{index}" / "Ext" / "Module.bsl"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"Процедура Изменение{index}()\nКонецПроцедуры\n", encoding="utf-8")
            git(extension, "add", ".")
            git(extension, "commit", "-m", "large historical change")
            git(extension, "switch", "main")
            git(extension, "merge", "--no-ff", "iss/large", "-m", "merge iss/large")
            fixture_expected["iss/large"] = git(extension, "rev-parse", "HEAD").decode().strip()
        if case.get("fixture") == "git-single-large-patch":
            git(extension, "switch", "-c", "iss/large-file")
            (extension / "Module.bsl").write_text(
                "".join(f"Процедура Проверка{index:04d}() // " + "контекст " * 12 +
                        "\nКонецПроцедуры\n" for index in range(500)), encoding="utf-8")
            git(extension, "commit", "-am", "large single-file review")
            git(extension, "switch", "main")
            git(extension, "merge", "--no-ff", "iss/large-file", "-m", "merge iss/large-file")
            fixture_expected["iss/large-file"] = git(extension, "rev-parse", "HEAD").decode().strip()
    config = json.loads((workflow / "opencode.json").read_text(encoding="utf-8"))
    config.pop("mcp", None) # The source query uses the intentionally unavailable local fixture endpoint.
    write_json(workflow / "opencode.json", config)
    write_json(destination / "fixture.json", {"generated_by": "Flow1C", "case": case["id"], "fixture": case.get("fixture", "chat"),
        "workflow": str(workflow), "documentation": str(docs), "extension": str(extension), "baseline_ref": baseline_ref,
        "expected_git": fixture_expected})
    return workflow, docs, extension, materials


def tool_calls(messages):
    calls = {}
    for message in messages:
        for part in message.get("parts", []):
            if part.get("type") == "tool":
                calls[part.get("callID", part.get("id"))] = part
    return list(calls.values())


def assess_run(case, messages, questions, *, timed_out=False, server_exit=None, extension_unchanged=True):
    """Inspect structured records, never search serialized strings for a lucky state."""
    failures = []
    calls = tool_calls(messages)
    def call_label(call):
        tool = str(call.get("tool", ""))
        input_value = call.get("state", {}).get("input", {})
        if tool == "flow1c_dialogue" and isinstance(input_value, dict) and input_value.get("action"):
            return f"{tool}:{input_value['action']}"
        if tool == "flow1c_action" and isinstance(input_value, dict) and input_value.get("action"):
            return f"{tool}:{input_value['action']}"
        if tool == "flow1c_write" and isinstance(input_value, dict) and input_value.get("target"):
            return f"{tool}:{input_value['target']}"
        if tool == "flow1c_git_inspect" and isinstance(input_value, dict) and input_value.get("action"):
            return f"{tool}:{input_value['action']}"
        return tool
    labels = [call_label(call) for call in calls]
    if timed_out:
        failures.append("run timed out")
    if server_exit is not None:
        failures.append(f"server exited unexpectedly: {server_exit}")
    if not extension_unchanged:
        failures.append("extension changed during a dialogue eval")
    errors = [m.get("info", {}).get("error") for m in messages if m.get("info", {}).get("error")]
    if errors:
        failures.append("assistant/provider returned an error")
    if any(c.get("tool") in FORBIDDEN for c in calls):
        failures.append("forbidden direct tool was called")
    pre_gate_tools = {"flow1c_route_catalog", "flow1c_route_check", "flow1c_redmine_files",
                      "flow1c_redmine_relations", "flow1c_redmine_upload"}
    begun = False
    for call in calls:
        tool_name = str(call.get("tool", ""))
        if tool_name == "flow1c_begin":
            begun = True
        elif tool_name.startswith("flow1c_") and not begun and tool_name not in pre_gate_tools:
            arguments = call.get("state", {}).get("input", {})
            if tool_name != "flow1c_redmine_fetch" or arguments.get("gate_id") or arguments.get("code"):
                failures.append("gate operation was called before flow1c_begin")
                break
    if len(calls) > int(case.get("max_tool_calls", 80)):
        failures.append("tool-call budget exceeded")
    git_calls = [call for call in calls if call.get("tool") in {"flow1c_git_refresh", "flow1c_git_inspect", "flow1c_git_snapshot"}]
    if len(git_calls) > int(case.get("max_git_calls", 999)):
        failures.append("Git tool-call budget exceeded")
    if not calls and not questions:
        failures.append("no observable tool or question activity")
    states = []
    completed_query_text = None
    for call in calls:
        state = call.get("state", {})
        if state.get("status") == "error":
            failures.append(f"tool failed: {call.get('tool')}")
        if call.get("tool") == "flow1c_complete" and state.get("status") == "completed":
            try:
                completion = json.loads(state["output"])
                states.append(completion["state"])
                completed_query_text = completion.get("query_text")
            except (KeyError, TypeError, ValueError):
                failures.append("malformed completion output")
    expected = set(case["expected_states"])
    if states:
        valid_terminal = states[-1] in expected
    else:
        valid_terminal = any(call.get("state", {}).get("output") and
                             any(value in str(call.get("state", {}).get("output")) for value in expected)
                             for call in calls)
    if case.get("requires_completion", True) and (not states or not valid_terminal):
        failures.append("expected terminal result was not produced by flow1c_complete")
    if case.get("required_query_fragments"):
        if not isinstance(completed_query_text, str):
            failures.append("completed query text is unavailable")
        else:
            query = completed_query_text.casefold()
            for fragment in case["required_query_fragments"]:
                if fragment.casefold() not in query:
                    failures.append(f"completed query omits required fragment: {fragment}")
    if checks := case.get("section_content_checks"):
        # Check the latest successfully saved section, not final prose or a
        # correct draft that was later replaced with a defective version.
        section_saves = [call for call in calls if call.get("tool") == "flow1c_section" and
                         call.get("state", {}).get("status") == "completed" and
                         call.get("state", {}).get("input", {}).get("action") == "save" and
                         call.get("state", {}).get("input", {}).get("section_id") == checks["section_id"]]
        body = None
        if section_saves:
            saved = section_saves[-1]["state"]
            try:
                output = json.loads(saved["output"])
                if output.get("state") == "DRAFT_READY":
                    body = saved["input"].get("content")
            except (KeyError, TypeError, ValueError, AttributeError):
                failures.append("malformed section-save output")
        if not isinstance(body, str) or not body.strip():
            failures.append("saved section text is unavailable")
        else:
            for pattern in checks.get("required_patterns", []):
                if not re.search(pattern, body, re.IGNORECASE | re.MULTILINE):
                    failures.append(f"saved section omits required pattern: {pattern}")
            for pattern in checks.get("forbidden_patterns", []):
                if re.search(pattern, body, re.IGNORECASE | re.MULTILINE):
                    failures.append(f"saved section contains forbidden pattern: {pattern}")
    if case.get("required_tool_sequence"):
        position = 0
        for required in case["required_tool_sequence"]:
            try:
                position = labels.index(required, position) + 1
            except ValueError:
                failures.append(f"required tool sequence item was not observed: {required}")
                break
    for forbidden in case.get("forbidden_tool_sequence", []):
        if forbidden in labels:
            failures.append(f"forbidden scenario tool was called: {forbidden}")
    if case.get("requires_question") and not questions:
        failures.append("no clarification question was asked")
    if case.get("question_before_begin") and questions:
        first_begin = next((c for c in calls if c.get("tool") == "flow1c_begin"), None)
        if first_begin and first_begin.get("state", {}).get("time", {}).get("start", float("inf")) < questions[0]["observed_epoch_ms"]:
            failures.append("gate was opened before the intent was clarified")
    if questions:
        first = questions[0]
        wording = " ".join(q.get("question", "") for q in first.get("questions", [])).casefold()
        if case.get("question_keywords") and not any(word in wording for word in case["question_keywords"]):
            failures.append("first question did not address the scenario's missing decision")
        if any(c.get("tool") in {"flow1c_source_query", "flow1c_analyze_bsl", "flow1c_context", "flow1c_action"}
               and c.get("state", {}).get("time", {}).get("start", float("inf")) < first["observed_epoch_ms"] for c in calls):
            failures.append("infrastructure was called before the missing decision was clarified")
        wordings = [json.dumps(q.get("questions", []), sort_keys=True, ensure_ascii=False) for q in questions]
        if len(wordings) != len(set(wordings)):
            failures.append("an identical question was repeated")
    failed_inputs = [(c.get("tool"), json.dumps(c.get("state", {}).get("input", {}), sort_keys=True))
                     for c in calls if c.get("state", {}).get("status") == "error"]
    if len(failed_inputs) != len(set(failed_inputs)):
        failures.append("an unchanged failed call was repeated")
    semantic_calls = []
    for call in git_calls:
        input_value = dict(call.get("state", {}).get("input", {}) or {})
        input_value.pop("max_chars", None)
        input_value.pop("max_count", None)
        semantic_calls.append((call.get("tool"), json.dumps(input_value, sort_keys=True, ensure_ascii=False)))
    if len(semantic_calls) != len(set(semantic_calls)):
        failures.append("a semantic duplicate Git call was observed")
    details = [call.get("state", {}).get("input", {}).get("detail") for call in calls
               if call.get("tool") == "flow1c_git_inspect" and call.get("state", {}).get("input", {}).get("action") == "diff"]
    if "patch" in details and not ({"names", "stat"} & set(details[:details.index("patch")])):
        failures.append("patch was requested before names/stat evidence")
    if case.get("requires_complete_diff_pagination"):
        diff_calls = [call for call in calls if call.get("tool") == "flow1c_git_inspect" and
                      call.get("state", {}).get("input", {}).get("action") == "diff" and
                      call.get("state", {}).get("input", {}).get("detail") == "patch"]
        pages = []
        for call in diff_calls:
            try:
                pages.append((call["state"]["input"], json.loads(call["state"]["output"])))
            except (KeyError, TypeError, ValueError):
                failures.append("patch page output is unavailable")
                break
        if len(pages) < 2 or not any(page.get("truncated_reason") == "max_chars" for _, page in pages):
            failures.append("large single-file patch was not paginated")
        for (_, page), (following_input, _) in zip(pages, pages[1:]):
            if page.get("next_cursor") and following_input.get("cursor") != page["next_cursor"]:
                failures.append("patch pagination cursor was not followed")
                break
        if pages and pages[-1][1].get("next_cursor"):
            failures.append("patch review ended before the final page")
    visible = "\n".join(str(part.get("text", "")) for message in messages
                         if message.get("info", {}).get("role") == "assistant"
                         for part in message.get("parts", []) if part.get("type") == "text")
    if any(marker.casefold() in visible.casefold() for marker in ("Actually", "Hmm", "Let me reconsider")):
        failures.append("visible assistant text contains internal reconsideration")
    tokens = {key: sum(m.get("info", {}).get("tokens", {}).get(key, 0) for m in messages) for key in ("input", "output", "reasoning")}
    return {"passed": not failures, "failures": failures, "terminal_states": states, "tool_calls": len(calls),
            "questions": len(questions), "time_to_question_seconds": questions[0]["elapsed_seconds"] if questions else None, "tokens": tokens}


def run_case(executable, model, case, destination, *, timeout, baseline_ref=None):
    workflow, docs, extension, materials = build_fixture(destination, case, baseline_ref=baseline_ref)
    original_extension = tree_hash(extension)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = uuid.uuid4().hex
    env = {**os.environ, "OPENCODE_SERVER_PASSWORD": password, "OPENCODE_SERVER_USERNAME": "flow1c-eval",
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    env.pop("FLOW1C_GITEA_TOKEN", None)
    base = f"http://127.0.0.1:{port}"
    authorization = "Basic " + base64.b64encode(f"flow1c-eval:{password}".encode()).decode()
    phase = "initialization"
    def request(method, endpoint, body=None, *, request_timeout=30):
        suffix = "?" + urllib.parse.urlencode({"directory": str(workflow)})
        req = urllib.request.Request(base + endpoint + suffix, data=json.dumps(body).encode() if body is not None else None,
              method=method, headers={"Content-Type": "application/json", "Authorization": authorization})
        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else None
    start = time.monotonic()
    messages, questions, snapshots = [], [], []
    timeout_hit = False
    with (destination / "server.log").open("wb") as log:
        server = subprocess.Popen([str(executable), "serve", "--hostname", "127.0.0.1", "--port", str(port)], cwd=workflow,
            env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        session = None
        try:
            deadline = time.monotonic() + min(timeout, 45)
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"OpenCode failed to start: exit {server.returncode}; see server.log")
                try:
                    phase = "health check"
                    health = request("GET", "/global/health", request_timeout=5)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("OpenCode startup timed out")
                    time.sleep(0.25)
            phase = "OpenAPI contract"
            contract = request("GET", "/doc", request_timeout=15)
            required = {"/question", "/question/{requestID}/reply"}
            if not required.issubset(contract.get("paths", {})):
                raise RuntimeError("Installed OpenCode does not expose the required question API")
            phase = "session creation"
            # The first session after an OpenCode restart may compile project plugins.
            # Keep one idempotency-safe POST open long enough instead of retrying and
            # potentially creating duplicate sessions after a client-side timeout.
            session = request("POST", "/session", {"title": "FLOW1C disposable eval"},
                              request_timeout=min(timeout, 90))["id"]
            provider, model_id = model.split("/", 1)
            prompt = case["prompt"].replace("{materials}", str(materials)).replace("{template}", str(materials / "Template.xml"))
            start = time.monotonic()
            phase = "prompt submission"
            request("POST", f"/session/{session}/prompt_async", {"agent": "flow1c-controller", "model": {"providerID": provider, "modelID": model_id}, "parts": [{"type": "text", "text": prompt}]})
            seen_questions, last_snapshot = set(), None
            answers = iter(case.get("answers", []))
            while time.monotonic() - start < timeout:
                if server.poll() is not None:
                    break
                phase = "question polling"
                pending = request("GET", "/question", request_timeout=10)
                for question in pending:
                    if question.get("sessionID") != session or question["id"] in seen_questions:
                        continue
                    seen_questions.add(question["id"])
                    record = {**question, "elapsed_seconds": time.monotonic() - start, "observed_epoch_ms": time.time() * 1000}
                    questions.append(record)
                    answer = next(answers, None)
                    if answer is None:
                        raise RuntimeError("Scenario has no answer for an additional question")
                    if len(question.get("questions", [])) != 1:
                        raise RuntimeError("Scenario expects one focused question at a time")
                    phase = "question reply"
                    request("POST", f"/question/{question['id']}/reply", {"answers": [[answer]]})
                phase = "permission polling"
                permissions = request("GET", "/permission", request_timeout=10)
                if any(p.get("sessionID") == session for p in permissions):
                    raise RuntimeError("Unexpected permission request; eval does not authorize external actions")
                phase = "message polling"
                messages = request("GET", f"/session/{session}/message", request_timeout=10)
                encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                if digest != last_snapshot:
                    snapshots.append({"elapsed_seconds": time.monotonic() - start, "messages": messages})
                    last_snapshot = digest
                phase = "status polling"
                statuses = request("GET", "/session/status", request_timeout=10)
                completed = any(m.get("info", {}).get("role") == "assistant" and m.get("info", {}).get("time", {}).get("completed") for m in messages)
                if completed and statuses.get(session, {}).get("type", "idle") == "idle" and not any(q.get("sessionID") == session for q in pending):
                    break
                time.sleep(0.25)
            else:
                timeout_hit = True
            result = assess_run(case, messages, questions, timed_out=timeout_hit, server_exit=server.poll(), extension_unchanged=tree_hash(extension) == original_extension)
            result.update(opencode_version=health.get("version"), elapsed_seconds=time.monotonic() - start)
        except (OSError, ValueError, RuntimeError) as exc:
            result = {"passed": False, "failures": [f"{phase}: {exc}"], "elapsed_seconds": time.monotonic() - start, "server_exit": server.poll()}
        finally:
            if session and server.poll() is None:
                try:
                    request("POST", f"/session/{session}/abort")
                except OSError:
                    pass
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
    write_json(destination / "messages.json", messages)
    write_json(destination / "questions.json", questions)
    with (destination / "events.ndjson").open("w", encoding="utf-8") as log:
        for snapshot in snapshots:
            log.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    write_json(destination / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--opencode", default=shutil.which("opencode"))
    parser.add_argument("--model", required=True, help="Exact provider/model-id selected by the user")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--scenario")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--baseline-ref", help="Compare with a local Git revision on the same model and machine")
    parser.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, help="New output/fixture directory; use outside the author workspace for client isolation")
    args = parser.parse_args()
    if args.runs < 1 or args.timeout < 1 or "/" not in args.model:
        parser.error("positive runs/timeout and provider/model-id are required")
    if not args.fixture_only and (not args.opencode or not Path(args.opencode).is_file()):
        parser.error("OpenCode CLI executable is required; a desktop GUI is not a CLI")
    cases = json.loads((ROOT / "evals/opencode-natural-language.json").read_text(encoding="utf-8"))
    if args.scenario:
        cases = [c for c in cases if c["id"] == args.scenario]
    if not cases:
        parser.error("no scenarios selected")
    output = args.output_dir or ROOT / ".workspace/opencode-evals" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    summary = {"schema_version": 2, "model": args.model, "machine": platform.platform(), "runs_per_scenario": args.runs,
               "baseline_ref": args.baseline_ref, "fixture_only": args.fixture_only, "results": [],
               "expected_results": len(cases) * args.runs * (2 if args.baseline_ref else 1),
               "completed": False, "passed": True}
    variants = [("baseline", args.baseline_ref), ("candidate", None)] if args.baseline_ref else [("candidate", None)]
    for case in cases:
        for run in range(args.runs):
            for variant, revision in variants:
                destination = output / f"{case['id']}-{variant}-{run + 1:02}"
                if args.fixture_only:
                    build_fixture(destination, case, baseline_ref=revision)
                    result = {"fixture_created": True, "passed": None}
                else:
                    result = run_case(args.opencode, args.model, case, destination, timeout=args.timeout, baseline_ref=revision)
                summary["results"].append({"case": case["id"], "variant": variant, "run": run + 1, **result})
                if variant == "candidate" and result["passed"] is False:
                    summary["passed"] = False
                write_json(output / "summary.json", summary)
                print(json.dumps({"case": case["id"], "variant": variant, "run": run + 1, "result": result}, ensure_ascii=False), flush=True)
    summary["latency"] = {variant: {"median_seconds": statistics.median(values), "samples": len(values)}
        for variant, _ in variants if (values := [r["time_to_question_seconds"] for r in summary["results"]
        if r["variant"] == variant and r.get("time_to_question_seconds") is not None])}
    if args.fixture_only:
        summary["passed"] = None
    summary["completed"] = len(summary["results"]) == summary["expected_results"]
    if not summary["completed"]:
        summary["passed"] = False
    write_json(output / "summary.json", summary)
    print(f"Summary: {output / 'summary.json'}")
    return 0 if summary["passed"] is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())
