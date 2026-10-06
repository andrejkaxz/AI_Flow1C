#!/usr/bin/env python3
"""Compatible Flow1C file entry point, independent of the current directory."""

import sys
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
# Replace Python's script-directory entry so specialized modules have one identity.
if sys.path and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
    sys.path[0] = str(PRODUCT_ROOT)
elif not sys.path or sys.path[0] != str(PRODUCT_ROOT):
    sys.path.insert(0, str(PRODUCT_ROOT))

from flow1c.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
