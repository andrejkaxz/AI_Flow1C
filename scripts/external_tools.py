#!/usr/bin/env python3
"""Check and safely update Flow1C's machine-local external tools."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "external-tools.json"
LOCAL_CONFIG_PATH = ROOT / ".flow1c.local.json"
ALLOWED_STATES = {"CURRENT", "UPDATE_AVAILABLE", "MISSING", "INVALID", "BLOCKED", "REVIEW_REQUIRED", "UPDATED", "SKIPPED"}
REQUIRED_WORKFLOW_SKILLS = {
    "flow1c-interview-preparation",
    "flow1c-consultation",
    "flow1c-functional-review",
    "flow1c-functional-spec",
    "flow1c-git-gitea",
    "flow1c-project-setup",
    "flow1c-project-update",
    "flow1c-query-analysis",
    "flow1c-registry",
    "flow1c-technical-implementation",
    "flow1c-technical-review",
    "flow1c-testing",
    "flow1c-wiki",
}


class ExternalToolsError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalToolsError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExternalToolsError(f"Expected a JSON object in {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def validate_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != 1:
        raise ExternalToolsError(f"Unsupported external tools manifest schema: {value.get('schema_version')!r}")
    for section in ("policy", "rlm_tools_bsl", "cc_1c_skills", "bsl_language_server"):
        if not isinstance(value.get(section), dict):
            raise ExternalToolsError(f"Manifest section '{section}' is required")
    rlm = value["rlm_tools_bsl"]
    for key in ("package", "requirement", "index_package"):
        if not isinstance(rlm.get(key), str) or not rlm[key].strip():
            raise ExternalToolsError(f"rlm_tools_bsl.{key} must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", rlm["package"]):
        raise ExternalToolsError("rlm_tools_bsl.package contains unsafe characters")
    skills = value["cc_1c_skills"]
    for key in ("repository", "remote", "ref", "path"):
        if not isinstance(skills.get(key), str) or not skills[key].strip():
            raise ExternalToolsError(f"cc_1c_skills.{key} must be a non-empty string")
    if not isinstance(skills.get("switch_command"), list) or not all(isinstance(x, str) and x for x in skills["switch_command"]):
        raise ExternalToolsError("cc_1c_skills.switch_command must be a non-empty string array")
    skills_path = Path(skills["path"])
    if skills_path.is_absolute() or ".." in skills_path.parts or not skills_path.parts or skills_path.parts[0] != ".tools":
        raise ExternalToolsError("cc_1c_skills.path must be a relative path inside .tools")
    switch_path = Path(skills["switch_command"][0])
    if switch_path.is_absolute() or ".." in switch_path.parts:
        raise ExternalToolsError("cc_1c_skills.switch_command must stay inside the skills repository")
    bsl = value["bsl_language_server"]
    for key in ("platform", "version", "url", "sha256", "executable"):
        if not isinstance(bsl.get(key), str) or not bsl[key].strip():
            raise ExternalToolsError(f"bsl_language_server.{key} must be a non-empty string")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", bsl["sha256"]):
        raise ExternalToolsError("bsl_language_server.sha256 must contain 64 hexadecimal characters")
    executable_path = Path(bsl["executable"])
    if executable_path.is_absolute() or ".." in executable_path.parts:
        raise ExternalToolsError("bsl_language_server.executable must be a relative archive path")
    for name, url in (("skills repository", skills["repository"]), ("BSL archive", bsl["url"])):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise ExternalToolsError(f"Untrusted {name} URL: {url}")
    if urllib.parse.urlparse(skills["repository"]).path.rstrip("/") != "/Nikolay-Shirokov/cc-1c-skills.git":
        raise ExternalToolsError("cc-1c-skills repository must use the approved upstream")
    if not urllib.parse.urlparse(bsl["url"]).path.startswith("/1c-syntax/bsl-language-server/releases/download/"):
        raise ExternalToolsError("BSL archive must use the approved upstream release path")
    return value


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return validate_manifest(read_json(path))


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ExternalToolsError(f"Command failed ({' '.join(command)}): {detail}")
    return result


def parse_version(value: str) -> tuple[tuple[int, Any], ...]:
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r"[._+\-]", value) if part)


def version_satisfies(version: str, requirement: str) -> bool:
    current = parse_version(version)
    for clause in (item.strip() for item in requirement.split(",") if item.strip()):
        match = re.fullmatch(r"(<=|>=|==|!=|<|>)\s*([A-Za-z0-9._+\-]+)", clause)
        if not match:
            raise ExternalToolsError(f"Unsupported version constraint: {clause}")
        operator, expected_text = match.groups()
        expected = parse_version(expected_text)
        comparisons = {"<": current < expected, "<=": current <= expected, "==": current == expected,
                       "!=": current != expected, ">=": current >= expected, ">": current > expected}
        if not comparisons[operator]:
            return False
    return True


def package_version(package: str, python: Path | None = None) -> str | None:
    if python and python.exists() and python.resolve() != Path(sys.executable).resolve():
        result = run([str(python), "-c", "import importlib.metadata,sys; print(importlib.metadata.version(sys.argv[1]))", package])
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def available_package_version(python: Path, package: str, requirement: str) -> tuple[str | None, str]:
    result = run([str(python), "-m", "pip", "index", "versions", package, "--disable-pip-version-check"], timeout=45)
    if result.returncode:
        return None, (result.stderr or result.stdout).strip() or "package index is unavailable"
    versions: list[str] = []
    for line in result.stdout.splitlines():
        if line.strip().lower().startswith("available versions:"):
            versions = [item.strip() for item in line.split(":", 1)[1].split(",")]
    compatible = [item for item in versions if item and version_satisfies(item, requirement)]
    return (max(compatible, key=parse_version), "") if compatible else (None, "no compatible version was published")


def git_output(repository: Path, *arguments: str, check: bool = True) -> str:
    result = run(["git", "-c", "core.excludesFile=", "-C", str(repository), *arguments], check=check)
    return result.stdout.strip()


def skills_connected(root: Path = ROOT) -> bool:
    source_dir = root / ".tools" / "cc-1c-skills" / ".claude" / "skills"
    target_dir = root / ".agents" / "skills"
    if not source_dir.is_dir() or not target_dir.is_dir():
        return False
    expected = [item.name for item in source_dir.iterdir() if item.is_dir() and (item / "SKILL.md").is_file()]
    external_skills_ok = bool(expected) and all(
        (target_dir / name / "SKILL.md").is_file() and (target_dir / name / "SKILL.md").stat().st_size > 0
        for name in expected
    )
    workflow_skills_ok = all(
        (target_dir / name / "SKILL.md").is_file() and (target_dir / name / "SKILL.md").stat().st_size > 0
        for name in REQUIRED_WORKFLOW_SKILLS
    )
    return external_skills_ok and workflow_skills_ok


def connect_skills(manifest: dict[str, Any], *, root: Path = ROOT, python: Path | None = None) -> None:
    """Connect external skills without deleting project-owned workflow skills."""
    cfg = manifest["cc_1c_skills"]
    repository = root / Path(cfg["path"])
    source_dir = repository / ".claude" / "skills"
    switch = repository / Path(cfg["switch_command"][0])
    target_dir = root / ".agents" / "skills"
    if not source_dir.is_dir() or not switch.is_file():
        raise ExternalToolsError("cc-1c-skills checkout is incomplete")

    expected = {
        item.name for item in source_dir.iterdir()
        if item.is_dir() and (item / "SKILL.md").is_file()
    }
    if not expected:
        raise ExternalToolsError("cc-1c-skills does not contain any skills")
    missing_workflow = sorted(
        name for name in REQUIRED_WORKFLOW_SKILLS
        if not (target_dir / name / "SKILL.md").is_file()
    )
    if missing_workflow:
        raise ExternalToolsError(
            "Project workflow skills must be restored before connection: " + ", ".join(missing_workflow)
        )

    marker_path = root / ".workspace" / "cc-1c-skills-connected.json"
    previously_managed: set[str] = set()
    if marker_path.is_file():
        marker = read_json(marker_path)
        names = marker.get("skills", [])
        if isinstance(names, list):
            previously_managed = {str(name) for name in names}
    managed = expected | previously_managed

    workspace = root / ".workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    python = python or root / ".venv" / "Scripts" / "python.exe"
    with tempfile.TemporaryDirectory(dir=workspace) as temporary_text:
        backup = Path(temporary_text) / "skills"
        if target_dir.is_dir():
            shutil.copytree(target_dir, backup)
        try:
            command = [str(python), str(switch), *cfg["switch_command"][1:], "--project-dir", str(root)]
            result = run(command, timeout=120)
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise ExternalToolsError(f"Cannot connect cc-1c-skills: {detail}")

            if backup.is_dir():
                for item in backup.iterdir():
                    if item.name in managed:
                        continue
                    destination = target_dir / item.name
                    if item.is_dir():
                        shutil.copytree(item, destination, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, destination)

            missing_workflow = sorted(
                name for name in REQUIRED_WORKFLOW_SKILLS
                if not (target_dir / name / "SKILL.md").is_file()
            )
            if missing_workflow:
                raise ExternalToolsError(
                    "Project workflow skills are missing after connection: " + ", ".join(missing_workflow)
                )
        except Exception as exc:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            if backup.is_dir():
                shutil.copytree(backup, target_dir)
            if isinstance(exc, ExternalToolsError):
                raise
            raise ExternalToolsError(f"Cannot connect cc-1c-skills: {exc}") from exc
    write_json_atomic(marker_path, {"schema_version": 1, "skills": sorted(expected)})


def bsl_smoke(executable: Path) -> tuple[bool, str]:
    if not executable.is_file():
        return False, "executable is missing"
    try:
        result = run([str(executable), "--version"], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr).strip().splitlines()
    return result.returncode == 0, detail[0] if detail else f"exit code {result.returncode}"


def check_tools(manifest: dict[str, Any], *, root: Path = ROOT, network: bool = True) -> dict[str, Any]:
    python = root / ".venv" / "Scripts" / "python.exe"
    rlm_cfg = manifest["rlm_tools_bsl"]
    installed = package_version(rlm_cfg["package"], python)
    rlm: dict[str, Any] = {"before": installed, "after": installed, "candidate": None, "state": "MISSING" if not installed else "CURRENT"}
    if installed and not version_satisfies(installed, rlm_cfg["requirement"]):
        rlm["state"] = "INVALID"
        rlm["detail"] = f"installed version does not satisfy {rlm_cfg['requirement']}"
    elif network and manifest["policy"].get("check_updates", True):
        candidate, error = available_package_version(python, rlm_cfg["package"], rlm_cfg["requirement"])
        rlm["candidate"] = candidate
        if error:
            rlm["state"], rlm["detail"] = "BLOCKED", error
        elif candidate and (not installed or parse_version(candidate) > parse_version(installed)):
            if installed:
                rlm["state"] = "UPDATE_AVAILABLE"
            else:
                rlm["recoverable"] = True
    for executable in ("rlm-tools-bsl.exe", "rlm-bsl-index.exe"):
        if not (root / ".venv" / "Scripts" / executable).is_file():
            rlm["state"], rlm["detail"] = "MISSING", f"{executable} is missing"

    skills_cfg = manifest["cc_1c_skills"]
    repository = root / Path(skills_cfg["path"])
    skills: dict[str, Any] = {"before_commit": None, "after_commit": None, "candidate_commit": None, "state": "MISSING"}
    if (repository / ".git").exists():
        before = git_output(repository, "rev-parse", "HEAD")
        skills.update(before_commit=before, after_commit=before, state="CURRENT")
        dirty = git_output(repository, "status", "--porcelain")
        if dirty:
            skills.update(state="BLOCKED", detail="local changes are present")
        elif network and manifest["policy"].get("check_updates", True):
            remote = run(["git", "ls-remote", skills_cfg["repository"], f"refs/heads/{skills_cfg['ref']}"])
            if remote.returncode or not remote.stdout.strip():
                skills.update(state="BLOCKED", detail=(remote.stderr or "remote ref is unavailable").strip())
            else:
                candidate = remote.stdout.split()[0]
                skills["candidate_commit"] = candidate
                if candidate != before:
                    # A read-only check deliberately does not fetch objects into the
                    # working repository. Fast-forward ancestry is proved after the
                    # apply-mode fetch and before merge --ff-only.
                    skills["state"] = "UPDATE_AVAILABLE"
        if skills["state"] == "CURRENT" and not skills_connected(root):
            skills.update(state="INVALID", recoverable=True, detail="skills are not connected in .agents/skills")
    elif network and manifest["policy"].get("check_updates", True):
        remote = run(["git", "ls-remote", skills_cfg["repository"], f"refs/heads/{skills_cfg['ref']}"])
        if remote.returncode or not remote.stdout.strip():
            skills.update(state="BLOCKED", detail=(remote.stderr or "remote ref is unavailable").strip())
        else:
            skills.update(candidate_commit=remote.stdout.split()[0], recoverable=True, detail="repository can be installed")

    bsl_cfg = manifest["bsl_language_server"]
    local = read_json(root / LOCAL_CONFIG_PATH) if (root / LOCAL_CONFIG_PATH).is_file() else {}
    local_bsl = local.get("bsl_language_server", {}) if isinstance(local.get("bsl_language_server"), dict) else {}
    before_version = str(local_bsl.get("version", "")) or None
    before_command = Path(str(local_bsl.get("command", ""))) if local_bsl.get("command") else None
    expected = root / ".tools" / f"bsl-language-server-v{bsl_cfg['version']}" / Path(bsl_cfg["executable"])
    bsl: dict[str, Any] = {"before": before_version, "after": before_version, "candidate": bsl_cfg["version"], "checksum_verified": False}
    if before_version != bsl_cfg["version"]:
        bsl["state"] = "UPDATE_AVAILABLE" if before_command and before_command.is_file() else "MISSING"
        if bsl["state"] == "MISSING":
            bsl["recoverable"] = True
    elif not expected.is_file() or not before_command or before_command.resolve() != expected.resolve():
        bsl.update(state="INVALID", recoverable=True, detail="configured executable does not match the manifest installation path")
    else:
        ok, detail = bsl_smoke(expected)
        bsl.update(state="CURRENT" if ok else "INVALID", detail=detail)
    return {"rlm_tools_bsl": rlm, "cc_1c_skills": skills, "bsl_language_server": bsl}


def install_bsl(manifest: dict[str, Any], local: dict[str, Any], *, root: Path = ROOT) -> None:
    cfg = manifest["bsl_language_server"]
    tools = (root / ".tools").resolve()
    destination = (tools / f"bsl-language-server-v{cfg['version']}").resolve()
    if tools not in destination.parents:
        raise ExternalToolsError("BSL installation path escapes .tools")
    executable = destination / Path(cfg["executable"])
    if not executable.is_file():
        tools.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tools) as temporary_text:
            temporary = Path(temporary_text)
            archive = temporary / "bsl-language-server.zip"
            with urllib.request.urlopen(cfg["url"], timeout=60) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual.lower() != cfg["sha256"].lower():
                raise ExternalToolsError(f"BSL checksum mismatch: expected {cfg['sha256']}, got {actual}")
            staging = temporary / "unpacked"
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    target = (staging / member.filename).resolve()
                    if staging.resolve() not in target.parents and target != staging.resolve():
                        raise ExternalToolsError(f"Unsafe path in BSL archive: {member.filename}")
                package.extractall(staging)
            staged_executable = staging / Path(cfg["executable"])
            if not staged_executable.is_file():
                raise ExternalToolsError(f"BSL archive does not contain {cfg['executable']}")
            ok, detail = bsl_smoke(staged_executable)
            if not ok:
                raise ExternalToolsError(f"BSL smoke test failed: {detail}")
            if destination.exists():
                raise ExternalToolsError(f"Incomplete BSL destination already exists: {destination}")
            shutil.move(str(staging), destination)
    settings = dict(local.get("bsl_language_server", {})) if isinstance(local.get("bsl_language_server"), dict) else {}
    settings.update(command=str(executable), version=cfg["version"])
    settings.setdefault("max_heap", "4g")
    local["bsl_language_server"] = settings


def apply_updates(manifest: dict[str, Any], before: dict[str, Any], *, root: Path = ROOT, backup_dir: Path) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    local_path = root / LOCAL_CONFIG_PATH
    local = read_json(local_path)
    shutil.copy2(local_path, backup_dir / LOCAL_CONFIG_PATH.name)
    write_json_atomic(backup_dir / "external-tools-before.json", before)
    changed: list[str] = []
    python = root / ".venv" / "Scripts" / "python.exe"
    try:
        bsl = before["bsl_language_server"]
        if bsl["state"] in {"UPDATE_AVAILABLE", "MISSING", "INVALID"}:
            install_bsl(manifest, local, root=root)
            write_json_atomic(local_path, local)
            changed.append("bsl")
        rlm = before["rlm_tools_bsl"]
        if rlm["state"] in {"UPDATE_AVAILABLE", "MISSING"}:
            candidate = rlm.get("candidate")
            if not candidate:
                raise ExternalToolsError("RLM update has no compatible candidate")
            run([str(python), "-m", "pip", "install", f"{manifest['rlm_tools_bsl']['package']}=={candidate}"], timeout=300, check=True)
            changed.append("rlm")
        skills = before["cc_1c_skills"]
        if skills["state"] in {"UPDATE_AVAILABLE", "MISSING", "INVALID"}:
            repository = root / Path(manifest["cc_1c_skills"]["path"])
            cfg = manifest["cc_1c_skills"]
            changed.append("skills")
            if skills["state"] == "MISSING":
                repository.parent.mkdir(parents=True, exist_ok=True)
                run(["git", "clone", "--single-branch", "--branch", cfg["ref"], cfg["repository"], str(repository)], timeout=180, check=True)
            elif skills["state"] == "UPDATE_AVAILABLE":
                git_output(repository, "fetch", cfg["remote"], cfg["ref"])
                candidate = git_output(repository, "rev-parse", f"{cfg['remote']}/{cfg['ref']}")
                ancestor = run(["git", "-C", str(repository), "merge-base", "--is-ancestor", skills["before_commit"], candidate])
                if ancestor.returncode:
                    raise ExternalToolsError("cc-1c-skills has diverged; automatic update is blocked")
                git_output(repository, "merge", "--ff-only", f"{cfg['remote']}/{cfg['ref']}")
            connect_skills(manifest, root=root, python=python)
        after = check_tools(manifest, root=root, network=False)
        blocking = [name for name, value in after.items() if value["state"] not in {"CURRENT"}]
        if blocking:
            raise ExternalToolsError(f"Post-update smoke checks failed: {', '.join(blocking)}")
        after["rlm_tools_bsl"]["before"] = before["rlm_tools_bsl"].get("before")
        after["bsl_language_server"]["before"] = before["bsl_language_server"].get("before")
        after["cc_1c_skills"]["before_commit"] = before["cc_1c_skills"].get("before_commit")
        if "rlm" in changed:
            after["rlm_tools_bsl"]["state"] = "UPDATED"
        if "bsl" in changed:
            after["bsl_language_server"]["state"] = "UPDATED"
            after["bsl_language_server"]["checksum_verified"] = True
        if "skills" in changed:
            after["cc_1c_skills"]["state"] = "UPDATED"
        write_json_atomic(backup_dir / "external-tools-after.json", after)
        return after
    except Exception as exc:
        recovery: list[str] = []
        if "skills" in changed:
            repository = root / Path(manifest["cc_1c_skills"]["path"])
            old = before["cc_1c_skills"].get("before_commit")
            current = git_output(repository, "rev-parse", "HEAD", check=False) if repository.exists() else ""
            if old and current and not git_output(repository, "status", "--porcelain", check=False):
                result = run(["git", "-C", str(repository), "update-ref", "HEAD", old, current])
                if result.returncode:
                    recovery.append(f"git -C \"{repository}\" update-ref HEAD {old} {current}")
            elif not old and repository.exists() and not git_output(repository, "status", "--porcelain", check=False):
                shutil.rmtree(repository)
        if "rlm" in changed and before["rlm_tools_bsl"].get("before"):
            old_version = before["rlm_tools_bsl"]["before"]
            result = run([str(python), "-m", "pip", "install", f"{manifest['rlm_tools_bsl']['package']}=={old_version}"], timeout=300)
            if result.returncode:
                recovery.append(f"{python} -m pip install {manifest['rlm_tools_bsl']['package']}=={old_version}")
        shutil.copy2(backup_dir / LOCAL_CONFIG_PATH.name, local_path)
        detail = str(exc)
        if recovery:
            detail += "; RECOVERY_REQUIRED: " + " ; ".join(recovery)
        raise ExternalToolsError(detail) from exc


def overall_state(tools: dict[str, Any]) -> str:
    states = {value["state"] for value in tools.values()}
    if "BLOCKED" in states:
        return "BLOCKED"
    if "INVALID" in states:
        return "UPDATE_AVAILABLE" if all(value["state"] != "INVALID" or value.get("recoverable") for value in tools.values()) else "BLOCKED"
    if "MISSING" in states:
        return "UPDATE_AVAILABLE" if all(value["state"] != "MISSING" or value.get("recoverable") for value in tools.values()) else "BLOCKED"
    if "REVIEW_REQUIRED" in states:
        return "REVIEW_REQUIRED"
    if "UPDATE_AVAILABLE" in states:
        return "UPDATE_AVAILABLE"
    return "READY"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--mode", choices=("check", "apply", "connect-skills"), default="check")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.mode == "connect-skills":
            connect_skills(manifest)
            payload = {"schema_version": 1, "state": "CONNECTED"}
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "External skills: CONNECTED")
            return 0
        network = bool(manifest["policy"].get("allow_network", True)) and not args.offline
        tools = check_tools(manifest, network=network)
        state = overall_state(tools)
        if args.mode == "apply":
            if not args.backup_dir:
                raise ExternalToolsError("--backup-dir is required in apply mode")
            tools = apply_updates(manifest, tools, backup_dir=args.backup_dir)
            state = overall_state(tools)
        payload = {"schema_version": 1, "state": state, "external_tools": tools}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else f"External tools: {state}")
        return 0 if state in {"READY", "UPDATE_AVAILABLE"} else 2
    except ExternalToolsError as exc:
        state = "RECOVERY_REQUIRED" if "RECOVERY_REQUIRED:" in str(exc) else "BLOCKED"
        payload = {"schema_version": 1, "state": state, "external_tools": {}, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else f"External tools: {state} - {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
