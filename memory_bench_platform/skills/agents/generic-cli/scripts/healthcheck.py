from __future__ import annotations

import json
import sys


def main() -> None:
    payload = {
        "status": "ok",
        "agent": "generic-cli",
        "runtime_mode": "process",
        "protocol_mode": "stateless_cli",
    }
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
