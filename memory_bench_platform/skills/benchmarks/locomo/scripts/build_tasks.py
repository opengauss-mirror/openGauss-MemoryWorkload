from __future__ import annotations

import json
import sys
from pathlib import Path


def build_tasks(data_path: Path) -> dict:
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    first = raw[0] if raw else {}
    sample_id = str(first.get("sample_id", "sample-0"))
    tasks = []
    for idx, qa in enumerate(first.get("qa", []), start=1):
        if str(qa.get("category", "")) == "5":
            continue
        tasks.append(
            {
                "task_id": f"{sample_id}-q{idx}",
                "sample_id": sample_id,
                "question": qa.get("question", ""),
                "expected_answer": str(qa.get("answer", "")),
                "category": str(qa.get("category", "")),
            }
        )
    return {"tasks": tasks}


if __name__ == "__main__":
    workspace_root = Path(__file__).resolve().parents[5]
    default_path = Path(sys.argv[1]) if len(sys.argv) > 1 else workspace_root / "locomo_test" / "data" / "locomo_small.json"
    print(json.dumps(build_tasks(default_path), ensure_ascii=False))
