"""PowerShell scaffold entry point; business logic belongs to flow1c.documentation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flow1c.documentation import initialize
from flow1c.errors import WorkflowError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documentation", required=True, type=Path)
    parser.add_argument("--templates", required=True, type=Path)
    parser.add_argument("--preserve-legacy", action="store_true")
    args = parser.parse_args()
    try:
        paths = initialize(args.documentation, args.templates, preserve_legacy=args.preserve_legacy)
    except (WorkflowError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(paths, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
