#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from memory_bench_platform.official_small_diagnostics import diagnose_official_small_run


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: diagnose_official_small.py RUN_DIR")
    run_dir = Path(sys.argv[1])
    print(json.dumps(diagnose_official_small_run(run_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
