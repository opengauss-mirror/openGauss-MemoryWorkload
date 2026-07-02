#!/usr/bin/env bash
set -euo pipefail

RUN_ID="${RUN_ID:-locomo_openclaw_import_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/${RUN_ID}}"
SOURCE_DIR="${OPENCLAW_LOCOMO_IMPORT_SOURCE:-${LOCOMO_OPENCLAW_IMPORT_SOURCE:-${DATA_PATH:-}}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="${LOCOMO_OPENCLAW_IMPORT_MODE:-execute}"

if [[ "${MODE}" == "copy" ]]; then
  if [[ -z "${SOURCE_DIR}" ]]; then
    echo "OPENCLAW_LOCOMO_IMPORT_SOURCE or DATA_PATH is required when LOCOMO_OPENCLAW_IMPORT_MODE=copy" >&2
    exit 2
  fi

  if [[ ! -d "${SOURCE_DIR}" ]]; then
    echo "OpenClaw LoCoMo import source is not a directory: ${SOURCE_DIR}" >&2
    exit 3
  fi

  if [[ ! -f "${SOURCE_DIR}/qa_results.csv" ]] && ! compgen -G "${SOURCE_DIR}/phaseA*.csv" >/dev/null; then
    echo "OpenClaw LoCoMo import source must contain qa_results.csv or phaseA*.csv: ${SOURCE_DIR}" >&2
    exit 4
  fi

  mkdir -p "${OUTPUT_DIR}"

  python3 - "${SOURCE_DIR}" "${OUTPUT_DIR}" "${RUN_ID}" <<'PY'
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

source = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
run_id = sys.argv[3]

for item in source.iterdir():
    target = output / item.name
    if item.is_dir():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(item, target)
    else:
        shutil.copy2(item, target)

(output / "openclaw_import_manifest.json").write_text(
    json.dumps(
        {
            "run_id": run_id,
            "source": "openclaw_locomo_import",
            "source_dir": str(source),
            "imported_at": datetime.now().isoformat(),
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY

  echo "openclaw_import_source=${SOURCE_DIR}"
  echo "output_dir=${OUTPUT_DIR}"
  exit 0
fi

# Default mode is a full OpenClaw-driven LoCoMo execution. It reuses the same
# isolated remote bootstrap as ov ingest, but switches locomo_test to
# memory_mode=openclaw so the benchmark side does not call OpenViking commit,
# recall, or task APIs directly.
export RUN_ID
export OUTPUT_DIR
export LOCOMO_TEST_CONFIG="${LOCOMO_TEST_CONFIG:-openclaw-small-stable.toml}"
exec "${SCRIPT_DIR}/run_locomo_test_remote.sh"
