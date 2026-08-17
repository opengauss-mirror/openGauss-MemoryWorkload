from __future__ import annotations

import json
import sys
from pathlib import Path


def validate(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "status": "ok",
        "source_path": str(path),
        "sample_count": len(data),
        "has_qa": bool(data and "qa" in data[0]),
        "sample_id": None if not data else str(data[0].get("sample_id", "")),
    }


if __name__ == "__main__":
    if len(sys.argv) <= 1:
        raise SystemExit("LoCoMo data path is required; pass --data-path to memory-bench")
    target = Path(sys.argv[1])
    if not target.is_file():
        raise SystemExit(f"LoCoMo data file not found: {target}")
    print(json.dumps(validate(target), ensure_ascii=False))
