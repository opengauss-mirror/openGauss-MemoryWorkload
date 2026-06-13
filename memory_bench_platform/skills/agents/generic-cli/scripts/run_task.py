from __future__ import annotations

import json
import sys


def main() -> None:
    request = json.load(sys.stdin)
    response = {
        "status": "ok",
        "agent": "generic-cli",
        "request": request,
        "turns": [],
        "artifacts": [],
        "metrics": [],
    }
    json.dump(response, sys.stdout)


if __name__ == "__main__":
    main()
