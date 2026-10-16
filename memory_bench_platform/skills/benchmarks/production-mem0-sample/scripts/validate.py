"""Validate the Mem0 sample mapping before replay writes."""
import json
import sys
from pathlib import Path


script = Path(__file__).resolve().parents[2] / "production-http-replay/scripts"
sys.path.insert(0, str(script))
from run_replay import validate_dataset


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate.py DATASET_DIRECTORY")
    try:
        result = validate_dataset(Path(sys.argv[1]), adapter_mode="mem0-sample")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
