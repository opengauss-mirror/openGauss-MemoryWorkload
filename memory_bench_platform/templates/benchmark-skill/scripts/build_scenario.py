from __future__ import annotations

import json
import sys
from pathlib import Path


def build(source: Path) -> dict:
    payload = json.loads(source.read_text(encoding="utf-8"))
    events = [
        {
            "event_id": item["session_id"],
            "type": "conversation",
            "timestamp": item.get("occurred_at"),
            "payload": {"messages": item["messages"]},
        }
        for item in sorted(payload["sessions"], key=lambda item: item.get("occurred_at") or "")
    ]
    events.append(
        {
            "event_id": "checkpoint-1",
            "type": "checkpoint",
            "timestamp": payload.get("evaluation_at"),
            "payload": {},
            "evaluation": {
                "target": "qa_answer",
                "profile": "llm_judge@1",
                "questions": payload["questions"],
            },
        }
    )
    return {
        "benchmark_id": "example-benchmark",
        "evaluation": {"target": "qa_answer", "profile": "llm_judge@1"},
        "samples": [{"sample_id": payload["episode_id"], "timeline": events}],
    }


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1])), ensure_ascii=False))
