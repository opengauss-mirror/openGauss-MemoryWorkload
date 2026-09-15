from __future__ import annotations

import argparse
from pathlib import Path

from memory_bench_platform.trace_runtime.openclaw_importer import import_openclaw_session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_jsonl")
    parser.add_argument("output_bundle")
    parser.add_argument("--case-id")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    manifest = import_openclaw_session(
        Path(args.session_jsonl),
        Path(args.output_bundle),
        case_id=args.case_id,
        session_id=args.session_id,
    )
    print(manifest.model_dump_json())


if __name__ == "__main__":
    main()
