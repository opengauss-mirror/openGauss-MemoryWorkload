from __future__ import annotations

import json
import sys
from pathlib import Path


def validate(path: Path | None) -> dict:
    if path is None:
        return {
            "status": "missing_source",
            "source_path": None,
            "sample_count": 0,
            "has_haystack_sessions": False,
            "question_id": None,
        }

    data = json.loads(path.read_text(encoding="utf-8"))
    first = data[0] if data else {}
    return {
        "status": "ok",
        "source_path": str(path),
        "sample_count": len(data),
        "has_haystack_sessions": bool(data and "haystack_sessions" in first),
        "question_id": None if not data else str(first.get("question_id", "")),
    }


def main() -> None:
    if len(sys.argv) <= 1:
        raise SystemExit("LongMemEval data path is required; pass --data-path to memory-bench")
    target = Path(sys.argv[1])
    if not target.is_file():
        raise SystemExit(f"LongMemEval data file not found: {target}")
    print(json.dumps(validate(target), ensure_ascii=False))


if __name__ == "__main__":
    main()
