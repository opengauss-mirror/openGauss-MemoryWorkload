#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="${OUT_DIR:-${ROOT}/outputs/test-entrypoint-validation-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "${OUT_DIR}"

echo "[1/3] official wrapper"
bash "${ROOT}/tools/test_entrypoints/run_official_locomo_small.sh"

echo "[2/3] memory_bench_platform external runner"
(
  cd "${ROOT}/memory_bench_platform"
  python3 -m memory_bench_platform.cli run --benchmark locomo --agent openclaw --entrypoint official_small
) | tee "${OUT_DIR}/memory_bench_platform.run_path.txt"

echo "[3/3] locomo_test stable config"
echo "Manual prerequisite: prepare ${ROOT}/locomo_test/configs/env.toml for the active environment."
echo "Then run:"
echo "  cd ${ROOT}/locomo_test && python3 -m locomo_test.cli run configs/openviking-small-stable.toml"
