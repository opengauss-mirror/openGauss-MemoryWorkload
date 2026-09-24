from __future__ import annotations

import json
import sys
from pathlib import Path

from run_replay import validate_dataset


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate.py DATASET_DIRECTORY")
    try:
        result = validate_dataset(Path(sys.argv[1]))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
