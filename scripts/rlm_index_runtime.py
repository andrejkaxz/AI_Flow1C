"""Persistent index jobs and validation receipts; never edit RLM's database.

The PowerShell adapter and doctor use this one implementation. A receipt replaces
only RLM's calendar check, after a successful build/update and strict validation.
Source or database changes, expiry, and tool upgrades invalidate it.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

try:
    from rlm_index_policy import receipt_is_current, validation_succeeded
except ModuleNotFoundError:
    from scripts.rlm_index_policy import receipt_is_current, validation_succeeded

IGNORED_DIRS = {".git", ".tools", ".venv", ".workspace", "node_modules", "build"}


class IndexRuntimeError(RuntimeError):
    """An index boundary failed; source and previous index are preserved."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise IndexRuntimeError(f"Cannot read index checkpoint {path}: {exc}") from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def job_lock(path: Path) -> Iterator[None]:
    """Serialize starts across processes; the OS releases the lock after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise IndexRuntimeError("Another index action is saving this source; retry the same action.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def process_identity(pid: int) -> dict[str, Any] | None:
    """Check creation time as well as PID, including on Windows Python launchers."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return None
        try:
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            if api.WaitForSingleObject(handle, 0) == 0:
                return None
            creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
            api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            if not api.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
                raise IndexRuntimeError(f"Cannot verify creation time of index process {pid}.")
            size = wintypes.DWORD(32768)
            executable = ctypes.create_unicode_buffer(size.value)
            api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            if not api.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(size)):
                raise IndexRuntimeError(f"Cannot verify executable of index process {pid}.")
            return {"pid": pid, "created": (creation.dwHighDateTime << 32) | creation.dwLowDateTime,
                    "executable": executable.value}
        finally:
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            api.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {"pid": pid, "created": stat[19], "executable": str(Path(f"/proc/{pid}/exe").resolve())}
    except FileNotFoundError:
        return None


def source_fingerprint(source: Path) -> str:
    """Stat all input files, including XML; do not follow external links."""
    digest = hashlib.sha256()
    def fail_walk(error: OSError) -> None:
        raise error
    for directory, folders, files in os.walk(source, followlinks=False, onerror=fail_walk):
        base = Path(directory)
        # Only VCS internals are omitted. RLM recursively sees input files in
        # other directories too, so their changes must invalidate the receipt.
        folders[:] = sorted(name for name in folders if name != ".git")
        for name in folders + sorted(files):
            path = base / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise IndexRuntimeError(f"Source contains a linked path; use an ordinary export directory: {path}")
        for name in sorted(files):
            path = base / name
            stat = path.stat()
            row = [path.relative_to(source).as_posix(), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            digest.update(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def resolve_source(source: Path) -> Path:
    if not source.is_absolute() or not source.is_dir():
        raise IndexRuntimeError(f"Source must be an existing absolute directory: {source}")
    source = source.resolve()
    if (source / "Configuration.xml").is_file():
        return source
    candidates = []
    for directory, folders, files in os.walk(source, followlinks=False):
        folders[:] = [name for name in folders if name not in IGNORED_DIRS]
        if "Configuration.xml" in files:
            candidates.append(Path(directory))
    if len(candidates) > 1:
        raise IndexRuntimeError("Multiple Configuration.xml roots; pass the exact source directory.")
    return candidates[0] if candidates else source


def database_identity(path: Path) -> str:
    # Reading just the immutable metadata and file attributes avoids hashing GBs.
    if not path.is_file():
        return ""
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("SELECT key,value FROM index_meta ORDER BY key").fetchall()
    signatures = []
    for candidate in (path, Path(str(path) + "-wal")):
        if candidate.exists():
            stat = candidate.stat()
            signatures.append([str(candidate), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
    return hashlib.sha256(json.dumps([rows, signatures]).encode()).hexdigest()


class IndexManager:
    def __init__(self, root: Path, source: Path, *, command: list[str] | None = None,
                 tool_version: str | None = None, database: Path | None = None):
        self.root = root.resolve()
        self.source = resolve_source(source)
        self.command = command or [sys.executable, "-m", "rlm_tools_bsl.cli"]
        self.tool_version = tool_version or importlib.metadata.version("rlm-tools-bsl")
        self.database = database
        source_id = hashlib.sha256(str(self.source).lower().encode()).hexdigest()[:16]
        self.directory = self.root / ".workspace" / "rlm-index" / source_id
        if not self.directory.resolve().is_relative_to(self.root):
            raise IndexRuntimeError("Index runtime directory escapes this checkout through a linked path.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.job_path = self.directory / "job.json"
        self.receipt_path = self.directory / "validation.json"
        self.lock_path = self.directory / "job.lock"

    def archive_job(self, record: dict[str, Any]) -> None:
        if record:
            write_json(self.directory / "history" / (str(uuid.uuid4()) + ".json"), record)

    def db_path(self) -> Path:
        if self.database is not None:
            return self.database
        from rlm_tools_bsl.bsl_index import get_index_db_path
        return get_index_db_path(str(self.source)).resolve()

    def max_age(self) -> float:
        days = int(os.environ.get("RLM_INDEX_MAX_AGE_DAYS", "7"))
        if days < 0:
            raise IndexRuntimeError("RLM_INDEX_MAX_AGE_DAYS must be non-negative.")
        return days * 86400

    def receipt_current(self) -> bool:
        receipt = read_json(self.receipt_path)
        if not receipt:
            return False
        return receipt_is_current(receipt, now=time.time(), max_age_seconds=self.max_age(),
                                  source_fingerprint=source_fingerprint(self.source),
                                  index_identity=database_identity(self.db_path()), tool_version=self.tool_version)

    def info(self, *, ignore_age: bool = False) -> dict[str, Any]:
        environment = os.environ.copy()
        if ignore_age:
            # Scoped to the final strict check after a successful job. Structural,
            # incomplete-index and content checks remain enabled; no global change.
            environment["RLM_INDEX_MAX_AGE_DAYS"] = "2147483647"
        result = subprocess.run([*self.command, "index", "info", str(self.source)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=120, check=False, env=environment)
        output = "\n".join((result.stdout, result.stderr)).strip()
        match = re.search(r"^\s*Status:\s*([^\r\n]+)", output, re.I | re.M)
        status = match.group(1).strip() if match else ""
        if result.returncode:
            raise IndexRuntimeError(f"index info exited {result.returncode}: {output[-2000:]}")
        if not status and "Index not found:" not in output:
            raise IndexRuntimeError(f"Unrecognized index info result: {output[-2000:]}")
        return {"status": status, "detail": status or "index is missing"}

    def status(self) -> dict[str, Any]:
        job = read_json(self.job_path)
        running = False
        for field in ("worker_identity", "child_identity"):
            identity = job.get(field)
            if identity and process_identity(int(identity["pid"])) == identity:
                running = True
        # Schema-1 jobs predate workers. Don't launch over a live legacy process.
        if job.get("schema_version") == 1 and job.get("pid"):
            identity = process_identity(int(job["pid"]))
            if identity and Path(identity["executable"]).name.casefold().startswith("rlm-bsl-index"):
                running = True
        result = {"schema_version": 2, "source_path": str(self.source),
                  "source_id": self.directory.name, "job_id": job.get("job_id"),
                  "pid": job.get("pid"), "started_at": job.get("started_at"),
                  "heartbeat_at": job.get("heartbeat_at"), "exit_code": job.get("exit_code"),
                  "stdout_log": job.get("stdout_log"), "stderr_log": job.get("stderr_log")}
        if running:
            return {**result, "state": "RUNNING", "detail": "Index job is running; resume the same update.",
                    "elapsed_seconds": max(0, int(time.time() - job.get("started_epoch", time.time())))}
        if job.get("schema_version") == 2 and job.get("state") == "RUNNING":
            return {**result, "state": "RECOVERY_REQUIRED", "detail": "Index worker stopped without a result; inspect logs before retrying Ensure."}
        if job.get("state") in {"FAILED", "RECOVERY_REQUIRED"}:
            return {**result, "state": job["state"], "detail": job.get("detail", "Index job failed; inspect logs.")}
        if self.receipt_current():
            return {**result, "state": "FRESH", "index_status": "fresh", "validation_basis": "verified-receipt",
                    "detail": "Source, index and tool match a recent successful validation."}
        info = self.info()
        state = "FRESH" if info["status"].casefold() == "fresh" else "STALE" if info["status"] else "MISSING"
        if state == "FRESH" and self.receipt_path.exists():
            state = "STALE"
            info["detail"] = "Previous validation was invalidated by source/index/tool changes or expiry."
        return {**result, "state": state, "index_status": info["status"], "detail": info["detail"]}

    def ensure(self, *, force: bool = False, request_id: str | None = None) -> dict[str, Any]:
        with job_lock(self.lock_path):
            state = self.status()
            prior = read_json(self.job_path)
            already_forced = request_id and prior.get("request_id") == request_id and prior.get("state") == "FRESH"
            if state["state"] == "RUNNING" or state["state"] == "FRESH" and (not force or already_forced):
                return state
            if state["state"] in {"FAILED", "RECOVERY_REQUIRED"}:
                raise IndexRuntimeError(state["detail"] + " Use RetryFailed only after inspecting the preserved job.")
            job_id = str(uuid.uuid4())
            stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + job_id[:8]
            job = {"schema_version": 2, "job_id": job_id, "state": "RUNNING",
                   "request_id": request_id,
                   "source_path": str(self.source), "source_id": self.directory.name,
                   "mode": "build" if state["state"] == "MISSING" else "update",
                   "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "started_epoch": time.time(),
                   "stdout_log": str(self.directory / (stamp + ".stdout.log")),
                   "stderr_log": str(self.directory / (stamp + ".stderr.log"))}
            self.archive_job(prior)
            write_json(self.job_path, job)
            command = [sys.executable, str(Path(__file__).resolve()), "worker", "--root", str(self.root),
                       "--source", str(self.source), "--job-id", job_id]
            options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                       "stderr": subprocess.DEVNULL, "cwd": self.root, "close_fds": True}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **options)
                job.update(pid=process.pid, worker_identity=process_identity(process.pid))
                if not job["worker_identity"]:
                    raise IndexRuntimeError("Index worker exited before its identity was recorded.")
                write_json(self.job_path, job)
            except Exception as exc:
                job.update(state="FAILED", detail=str(exc))
                write_json(self.job_path, job)
                raise
            return {**state, **job, "state": "RUNNING", "detail": "Index job started; progress is saved in job.json."}

    def run_worker(self, job_id: str, *, timeout: float = 3600) -> None:
        # Wait for the starter to persist PID under the same lock.
        for _ in range(100):
            try:
                with job_lock(self.lock_path):
                    job = read_json(self.job_path)
                break
            except IndexRuntimeError:
                time.sleep(0.05)
        else:
            raise IndexRuntimeError("Cannot acquire index checkpoint lock.")
        if job.get("job_id") != job_id or job.get("state") != "RUNNING":
            raise IndexRuntimeError("Index job identity changed; refusing to run.")
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(15):
                job["heartbeat_at"] = time.time()
                write_json(self.job_path, job)

        thread = threading.Thread(target=heartbeat, daemon=True)
        try:
            for key in ("stdout_log", "stderr_log"):
                log = Path(job[key])
                if log.is_symlink() or not log.resolve().is_relative_to(self.directory.resolve()):
                    raise IndexRuntimeError("Index job log path escapes its runtime directory.")
            before = source_fingerprint(self.source)
            job["source_fingerprint_before"] = before
            thread.start()
            environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
            with Path(job["stdout_log"]).open("w", encoding="utf-8") as stdout, Path(job["stderr_log"]).open("w", encoding="utf-8") as stderr:
                options: dict[str, Any] = {"stdout": stdout, "stderr": stderr, "stdin": subprocess.DEVNULL, "env": environment}
                if os.name != "nt":
                    options["start_new_session"] = True
                child = subprocess.Popen([*self.command, "index", job["mode"], str(self.source)], **options)
                job["child_identity"] = process_identity(child.pid)
                job["heartbeat_at"] = time.time()
                write_json(self.job_path, job)
                try:
                    code = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    if job["child_identity"] and process_identity(child.pid) == job["child_identity"]:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=15, check=False)
                        else:
                            import signal
                            os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=15)
                    job["exit_code"] = child.returncode
                    raise IndexRuntimeError(f"Index exceeded its worker budget ({timeout:g} seconds); previous files and logs are preserved.")
            job["exit_code"] = code
            if code:
                raise IndexRuntimeError(f"Index {job['mode']} exited {code}; see {job['stderr_log']}.")
            after = source_fingerprint(self.source)
            strict = self.info(ignore_age=True)
            final = source_fingerprint(self.source)
            job["source_fingerprint_after"] = final
            if after != final:
                raise IndexRuntimeError("Source changed during final index validation; retry after source updates finish.")
            if not validation_succeeded(code, before, after, strict["status"]):
                raise IndexRuntimeError(f"Post-index validation failed ({strict['detail']}); source may have changed during indexing.")
            identity = database_identity(self.db_path())
            if not identity:
                raise IndexRuntimeError("The completed index database is missing.")
            write_json(self.receipt_path, {"schema_version": 1, "source_path": str(self.source),
                       "validated_at": time.time(), "source_fingerprint": after,
                       "index_identity": identity, "tool_version": self.tool_version, "job_id": job_id})
            job.update(state="FRESH", detail="Index completed and source/index consistency verified.")
        except Exception as exc:
            job.update(state="FAILED", detail=str(exc))
        finally:
            stop.set()
            if thread.is_alive():
                thread.join(timeout=2)
            job["completed_at"] = time.time()
            write_json(self.job_path, job)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("Ensure", "Status", "Wait", "worker"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=480)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--job-id")
    parser.add_argument("--request-id")
    args = parser.parse_args()
    try:
        manager = IndexManager(args.root, args.source)
        if args.action == "worker":
            manager.run_worker(str(args.job_id))
            return 0
        if args.retry_failed:
            with job_lock(manager.lock_path):
                state = manager.status()
                if state["state"] in {"FAILED", "RECOVERY_REQUIRED"}:
                    old = read_json(manager.job_path)
                    manager.archive_job(old)
                    manager.job_path.unlink()
        result = manager.ensure(force=args.force, request_id=args.request_id) if args.action == "Ensure" else manager.status()
        if args.action == "Wait":
            deadline = time.monotonic() + min(540, max(0, args.wait_seconds))
            while result["state"] == "RUNNING" and time.monotonic() < deadline:
                time.sleep(min(2, max(0, deadline - time.monotonic())))
                result = manager.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["state"] in {"FAILED", "RECOVERY_REQUIRED"} else 0
    except Exception as exc:
        print(json.dumps({"schema_version": 2, "state": "FAILED", "ready": False,
                          "source_path": str(args.source), "detail": str(exc),
                          "errors": [{"code": "RLM_INDEX_FAILED", "message": str(exc), "recoverable": True,
                                      "next_action": "Inspect the source and preserved index job logs before retrying."}]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
