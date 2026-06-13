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
    workspace_root = Path(__file__).resolve().parents[5]
    default_path = workspace_root / "locomo_test" / "data" / "locomo_small.json"
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    print(json.dumps(validate(target), ensure_ascii=False))
