#!/usr/bin/env python3
"""Offline Windows updater smoke in a clean clone and a newly bootstrapped venv.

Git, PowerShell, workers, checkpoints and receipts are real. RLM and service/
external-tool boundaries use synthetic fixtures; no user configuration is used.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, *, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    # An isolated fixture must not depend on global Git identity, excludes or RLM paths.
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       RLM_INDEX_MAX_AGE_DAYS="7")
    for key in ("RLM_CONFIG_FILE", "RLM_INDEX_DIR", "PYTHONPATH"):
        environment.pop(key, None)
    return subprocess.run(command, cwd=cwd, env=environment, text=True, encoding="utf-8",
                          errors="replace", capture_output=True, timeout=timeout, check=check)


def smoke(parent: Path, powershell: str = "powershell.exe") -> dict:
    seed, checkout = parent / "origin", parent / "clean checkout"
    (seed / "scripts").mkdir(parents=True)
    for name in ("update.ps1", "git-identity.ps1", "rlm-index.ps1", "rlm_index_runtime.py", "rlm_index_policy.py", "migrate-local-config.py"):
        shutil.copy2(ROOT / "scripts" / name, seed / "scripts" / name)
    (seed / ".opencode/agents").mkdir(parents=True)
    (seed / ".opencode/agents/flow1c-controller.md").write_text("fixture v1", encoding="utf-8")
    (seed / "VERSION").write_text("0.1.0", encoding="utf-8")
    (seed / ".gitignore").write_text(".workspace/\n.venv/\n.flow1c.local.json\n__pycache__/\n")
    (seed / "config").mkdir()
    (seed / "config/external-tools.json").write_text(json.dumps({"policy": {"apply_updates": False}}))
    (seed / "scripts/start-rlm-tools-bsl.ps1").write_text("Write-Output 'fixture service healthy'\nexit 0\n")
    (seed / "scripts/external_tools.py").write_text(
        "import json,pathlib\n"
        "root=pathlib.Path(__file__).resolve().parents[1]\n"
        "p=root/'.workspace/external-checks.txt';p.parent.mkdir(exist_ok=True)\n"
        "with p.open('a') as f:f.write('check\\n')\n"
        "print(json.dumps({'state':'CURRENT','external_tools':{'rlm_tools_bsl':{'state':'CURRENT'}}}))\n")
    (seed / "scripts/flow1c.py").write_text(
        "import json,pathlib\nfrom rlm_index_runtime import IndexManager\n"
        "root=pathlib.Path(__file__).resolve().parents[1]\n"
        "config=json.loads((root/'.flow1c.local.json').read_text(encoding='utf-8-sig'))\n"
        "states=[IndexManager(root,pathlib.Path(config[k])).status()['state'] for k in ('configuration_path','extension_path')]\n"
        "ready=all(s=='FRESH' for s in states)\n"
        "print(json.dumps({'ready':ready,'state':'READY' if ready else 'BLOCKED'}))\n"
        "raise SystemExit(0 if ready else 1)\n")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required for update smoke")
    def git_run(*args: str, cwd: Path = seed) -> str:
        return run([git, *args], cwd).stdout.strip()
    git_run("init", "-b", "main")
    git_run("config", "user.name", "Update fixture")
    git_run("config", "user.email", "fixture@example.invalid")
    git_run("add", ".")
    git_run("commit", "-m", "fixture initial")
    old_head = git_run("rev-parse", "HEAD")
    git_run("clone", str(seed), str(checkout), cwd=parent)
    (seed / "VERSION").write_text("0.1.1")
    (seed / ".opencode/agents/flow1c-controller.md").write_text("fixture v2")
    git_run("add", ".")
    git_run("commit", "-m", "fixture upgrade")
    new_head = git_run("rev-parse", "HEAD")
    run([sys.executable, "-m", "venv", "--without-pip", str(checkout / ".venv")], parent, timeout=90)
    python = checkout / ".venv/Scripts/python.exe"
    library = checkout / ".venv/Lib/site-packages"
    package = library / "rlm_tools_bsl"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    metadata = library / "rlm_tools_bsl-1.41.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: rlm-tools-bsl\nVersion: 1.41.0\n")
    cache = checkout / ".workspace/fixture-indexes"
    (package / "bsl_index.py").write_text(
        "import pathlib,hashlib\n"
        f"CACHE=pathlib.Path({str(cache)!r})\n"
        "def get_index_db_path(source):return CACHE/(hashlib.sha256(source.encode()).hexdigest()[:16]+'.db')\n")
    (package / "cli.py").write_text(
        "import sys,os,time,sqlite3,pathlib\nfrom contextlib import closing\n"
        "from .bsl_index import get_index_db_path,CACHE\n"
        "source=sys.argv[-1];db=get_index_db_path(source);action=sys.argv[2]\n"
        "if action=='info':\n"
        " if not db.exists():print('Index not found: '+str(db))\n"
        " else:print('Status: '+('fresh' if os.environ.get('RLM_INDEX_MAX_AGE_DAYS')=='2147483647' else 'stale (age)'))\n"
        "else:\n"
        " CACHE.mkdir(parents=True,exist_ok=True)\n"
        " with (CACHE/'invocations.txt').open('a') as f:f.write(source+'\\n')\n"
        " print('fixture indexing',flush=True);time.sleep(3)\n"
        " with closing(sqlite3.connect(db)) as c,c:\n"
        "  c.execute('CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY,value TEXT)')\n"
        "  c.execute(\"INSERT OR IGNORE INTO index_meta VALUES ('built_at','1')\")\n"
        " print('Updated: Added=0 Changed=0 Removed=0',flush=True)\n")
    sources = [parent / "configuration", parent / "extension"]
    for source in sources:
        source.mkdir()
        (source / "Configuration.xml").write_text("<fixture/>")
        (source / "Module.bsl").write_text("// synthetic fixture only")
    extension = sources[1]
    git_run("init", "-b", "main", cwd=extension)
    git_run("config", "user.name", "Update fixture", cwd=extension)
    git_run("config", "user.email", "fixture@example.invalid", cwd=extension)
    git_run("add", ".", cwd=extension)
    git_run("commit", "-m", "extension fixture", cwd=extension)
    extension_origin = parent / "extension-origin.git"
    git_run("clone", "--bare", str(extension), str(extension_origin), cwd=parent)
    extension_url = extension_origin.as_uri()
    git_run("remote", "add", "origin", extension_url, cwd=extension)
    git_run("fetch", "origin", cwd=extension)
    git_run("branch", "--set-upstream-to=origin/main", cwd=extension)
    git_run("remote", "set-url", "origin", (parent / "wrong-extension.git").as_uri(), cwd=extension)
    documentation = parent / "project documentation"
    (documentation / ".flow1c/drafts/saved-request").mkdir(parents=True)
    (documentation / ".flow1c/layout.json").write_text(json.dumps({"schema_version": 1, "layout_version": 2}))
    (documentation / ".flow1c/drafts/saved-request/evidence.json").write_text(json.dumps({"answers": ["saved"], "state": "WAITING_USER"}))
    legacy_documentation = parent / "legacy documentation"
    (legacy_documentation / "work-items/USER-1").mkdir(parents=True)
    (legacy_documentation / "work-items/USER-1/manifest.yaml").write_text(json.dumps({"approvals": {"user": "approved"}}))
    preserved_documents = {path: path.read_bytes() for folder in (documentation, legacy_documentation)
                           for path in folder.rglob("*") if path.is_file()}
    local = {"documentation_path": str(documentation), "configuration_path": str(sources[0]), "extension_path": str(sources[1]),
             "extension_mode": "git", "extension_repository_url": extension_url,
             "custom_user_setting": {"keep": "unchanged"}}
    config = checkout / ".flow1c.local.json"
    config.write_text(json.dumps(local), encoding="utf-8")
    before = config.read_bytes()
    def update(*args: str) -> dict:
        result = run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      str(checkout / "scripts/update.ps1"), "-Json", *args], checkout, check=False)
        try:
            data = json.loads(result.stdout.lstrip("\ufeff"))
        except ValueError as exc:
            raise RuntimeError(f"Updater emitted invalid JSON: {result.stdout}\n{result.stderr}") from exc
        if result.returncode and data.get("state") != "BLOCKED":
            raise RuntimeError(f"Unexpected updater failure: {data}")
        return data
    blocked_extension = update()
    assert blocked_extension["state"] == "BLOCKED" and "remote URL" in blocked_extension["error"], blocked_extension
    blocked_backup = Path(blocked_extension["backup_directory"])
    assert (blocked_backup / ".flow1c.local.json").read_bytes() == before
    git_run("remote", "set-url", "origin", extension_url, cwd=extension)
    first = update("-UpdateId", blocked_extension["update_id"])
    assert first["update_id"] == blocked_extension["update_id"]
    assert first["backup_directory"] == str(blocked_backup)
    assert len(list((checkout / ".workspace/backups").glob("update-*"))) == 1
    if first["state"] != "WAITING_BACKGROUND":
        raise AssertionError(first)
    update_id, backup = first["update_id"], Path(first["backup_directory"])
    assert first["previous_commit"] == old_head and first["current_commit"] == new_head
    assert first["agent_runtime"]["restart_required"]
    assert (backup / ".flow1c.local.json").read_bytes() == before
    migrated = json.loads(config.read_text(encoding="utf-8-sig"))
    assert migrated["schema_version"] == 2 and migrated["custom_user_setting"] == local["custom_user_setting"]
    migrated_bytes = config.read_bytes()
    resumed = update("-UpdateId", update_id, "-WaitSeconds", "5")
    for _ in range(5):
        if resumed["state"] != "WAITING_BACKGROUND":
            break
        resumed = update("-UpdateId", update_id, "-WaitSeconds", "5")
    assert resumed["state"] == "READY", resumed
    assert resumed["previous_commit"] == old_head and resumed["current_commit"] == new_head
    assert resumed["backup_directory"] == str(backup)
    assert resumed["agent_runtime"]["restart_required"]
    assert (checkout / ".workspace/external-checks.txt").read_text().count("check") == 1
    repeated = update()
    assert repeated["state"] == "READY", repeated
    assert len((cache / "invocations.txt").read_text().splitlines()) == 2
    assert config.read_bytes() == migrated_bytes
    assert all(path.read_bytes() == content for path, content in preserved_documents.items())
    assert not (documentation / "drafts").exists()
    assert not (legacy_documentation / ".flow1c").exists()
    # Hold the same exclusive Windows file handle as another updater call.
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    lock = api.CreateFileW(str(checkout / ".workspace/updates/update.lock"),
                           0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
    if lock == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    checkpoint = checkout / ".workspace/updates" / (update_id + ".json")
    original_checkpoint = checkpoint.read_bytes()
    progress = checkpoint.with_suffix(".progress.json")
    original_progress = progress.read_bytes()
    try:
        concurrent = update("-UpdateId", update_id)
        assert concurrent["state"] == "BLOCKED" and "Another update call" in concurrent["error"]
        assert checkpoint.read_bytes() == original_checkpoint
        assert progress.read_bytes() == original_progress
        assert config.read_bytes() == migrated_bytes
    finally:
        api.CloseHandle(lock)
    assert update("-UpdateId", update_id)["state"] == "READY"
    # A changed config must block this checkpoint while preserving the user's edit.
    edited = {**migrated, "custom_user_setting": {"keep": "user changed it"}}
    checkpoint = checkout / ".workspace/updates" / (update_id + ".json")
    original_checkpoint = checkpoint.read_bytes()
    config.write_text(json.dumps(edited), encoding="utf-8")
    blocked = update("-UpdateId", update_id)
    assert blocked["state"] == "BLOCKED" and "configuration changed" in blocked["error"]
    assert json.loads(config.read_text()) == edited
    assert checkpoint.read_bytes() == original_checkpoint
    return {"schema_version": 1, "state": "PASSED", "platform": sys.platform,
            "powershell": powershell, "clean_venv_bootstrap": True, "network_used": False,
            "external_boundaries": "synthetic RLM/service/tool fixtures",
            "checks": ["fast-forward upgrade", "legacy configuration migration", "user field preservation", "v2 documentation and legacy approvals preserved",
                       "blocked extension recovery on the same checkpoint and original backup",
                       "two background sources", "same checkpoint and backup on resume",
                       "restart requirement preserved", "no repeated fetch/tool check on resume",
                       "zero-delta old-index validation", "idempotent repeat update",
                       "concurrent updater blocked without state overwrite", "config drift blocked"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--powershell", default="powershell.exe")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This smoke exercises the supported Windows/PowerShell updater.")
    with tempfile.TemporaryDirectory(prefix="flow1c-update-smoke-") as directory:
        try:
            evidence = smoke(Path(directory), args.powershell)
        finally:
            # Even a failing test must let its detached synthetic workers finish
            # before deleting the venv they are running from.
            from rlm_index_runtime import process_identity, read_json
            jobs = Path(directory) / "clean checkout/.workspace/rlm-index"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                live = False
                for path in jobs.glob("*/job.json"):
                    record = read_json(path)
                    for field in ("worker_identity", "child_identity"):
                        identity = record.get(field)
                        live |= bool(identity and process_identity(identity["pid"]) == identity)
                if not live:
                    break
                time.sleep(0.2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
