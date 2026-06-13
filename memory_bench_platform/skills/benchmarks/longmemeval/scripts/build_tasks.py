from __future__ import annotations

import json


def main() -> None:
    print(json.dumps({"tasks": []}, ensure_ascii=False))


if __name__ == "__main__":
    main()
