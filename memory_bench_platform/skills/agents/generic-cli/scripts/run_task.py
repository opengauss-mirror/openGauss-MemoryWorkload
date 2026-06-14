from __future__ import annotations

import json
import sys


def main() -> None:
    request = json.load(sys.stdin)
    user_message = ""
    for message in reversed(request.get("messages", [])):
        if message.get("role") == "user":
            user_message = str(message.get("content", ""))
            break
    response = {
        "status": "ok",
        "agent": "generic-cli",
        "request": request,
        "turns": [{"text": user_message}],
        "artifacts": [],
        "metrics": [{"name": "duration_ms", "value": 0}],
    }
    json.dump(response, sys.stdout)


if __name__ == "__main__":
    main()
