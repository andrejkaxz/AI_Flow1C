#!/usr/bin/env python3
"""Exercise the installed RLM CLI on a tiny synthetic source and isolated cache."""
from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from rlm_index_runtime import IndexManager, read_json, write_json


def smoke(directory: Path) -> dict:
    source, cache, root = directory / "source", directory / "indexes", directory / "workflow"
    module = source / "CommonModules/IndexSmokeFixture/Ext/Module.bsl"
    module.parent.mkdir(parents=True)
    root.mkdir()
    module.write_text("Процедура ПроверкаИндекса() Экспорт\n    Сообщить(\"fixture\");\nКонецПроцедуры\n", encoding="utf-8")
    (source / "Configuration.xml").write_text(
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"><Configuration>'
        '<Properties><Name>IndexSmokeFixture</Name><Version>1</Version></Properties>'
        '</Configuration></MetaDataObject>', encoding="utf-8")
    environment = os.environ.copy()
    os.environ["RLM_INDEX_DIR"] = str(cache)
    os.environ["RLM_INDEX_MAX_AGE_DAYS"] = "7"
    os.environ.pop("RLM_CONFIG_FILE", None)
    try:
        result = subprocess.run([sys.executable, "-m", "rlm_tools_bsl.cli", "index", "build", str(source)],
                                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        manager = IndexManager(root, source)
        database = manager.db_path()
        # Change only this synthetic test DB to reproduce the seven-day boundary.
        old = str(time.time() - 8 * 86400)
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE index_meta SET value=? WHERE key='built_at'", (old,))
        assert manager.info()["status"] == "stale (age)"
        job_id = str(uuid.uuid4())
        write_json(manager.job_path, {"schema_version": 2, "job_id": job_id, "state": "RUNNING", "mode": "update",
                   "source_path": str(source), "source_id": manager.directory.name,
                   "stdout_log": str(manager.directory / "compat.stdout.log"),
                   "stderr_log": str(manager.directory / "compat.stderr.log")})
        manager.run_worker(job_id)
        job = read_json(manager.job_path)
        assert job["state"] == "FRESH", job
        assert manager.status()["state"] == "FRESH"
        assert manager.ensure()["state"] == "FRESH"
        assert os.environ["RLM_INDEX_MAX_AGE_DAYS"] == "7"
        (source / "Configuration.xml").write_text((source / "Configuration.xml").read_text() + "\n", encoding="utf-8")
        assert manager.status()["state"] == "STALE"
        return {"schema_version": 1, "state": "PASSED", "rlm_version": importlib.metadata.version("rlm-tools-bsl"),
                "source": "tiny synthetic export", "user_sources_used": False, "cache": "isolated temporary directory",
                "checks": ["actual CLI build", "seven-day stale-age boundary", "zero-delta update validation",
                           "receipt reuse without reindex", "XML change invalidates receipt", "age environment preserved"]}
    finally:
        os.environ.clear()
        os.environ.update(environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="flow1c-index-compat-") as directory:
        evidence = smoke(Path(directory))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
