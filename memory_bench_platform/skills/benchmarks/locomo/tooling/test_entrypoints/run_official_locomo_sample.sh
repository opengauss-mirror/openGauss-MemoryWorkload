#!/usr/bin/env bash
set -euo pipefail

SAMPLE="${SAMPLE:-0}"
SESSIONS="${SESSIONS:-1-19}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export SAMPLE
export SESSIONS

bash "${SCRIPT_DIR}/run_official_locomo_small.sh"
