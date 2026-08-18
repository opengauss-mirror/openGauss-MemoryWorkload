#!/usr/bin/env bash
set -euo pipefail

LOCK_DIR="${1:?lock dir required}"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/locomo_eval.lock"

if [ -f "$LOCK_FILE" ]; then
  echo "LOCKED:$LOCK_FILE"
  exit 2
fi

cleanup() {
  rm -f "$LOCK_FILE"
}

trap cleanup EXIT INT TERM

echo "$$" > "$LOCK_FILE"
shift
"$@"
