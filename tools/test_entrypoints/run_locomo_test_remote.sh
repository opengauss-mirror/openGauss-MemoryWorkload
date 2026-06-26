#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="${SSH_HOST:-jcp@123.60.114.206}"
SSH_PORT="${SSH_PORT:-10008}"
REMOTE_CONTAINER="${REMOTE_CONTAINER:-jcp-dev}"
REMOTE_ROOT="${REMOTE_ROOT:-/tmp/locomo_test}"
REMOTE_LOCK_DIR="${REMOTE_LOCK_DIR:-/tmp/locomo-entrypoint-locks}"
RUN_ID="${RUN_ID:-locomo_test_remote_$(date +%Y%m%d_%H%M%S)}"
LOCAL_OUTPUT_ROOT="${LOCAL_OUTPUT_ROOT:-/tmp/locomo_test_output}"
LOCAL_OUTPUT_DIR="${LOCAL_OUTPUT_ROOT}/${RUN_ID}"
REMOTE_OUTPUT_DIR="/tmp/locomo_test_output/${RUN_ID}"
REMOTE_MONITOR_DIR="${REMOTE_OUTPUT_DIR}/monitor"
LOCOMO_TEST_CONFIG="${LOCOMO_TEST_CONFIG:-openviking-small-stable.toml}"
OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-${RUN_ID}}"
OPENCLAW_HOME_DIR="${OPENCLAW_HOME_DIR:-/tmp/openclaw-home-${RUN_ID}}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"
OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-$(python3 -c 'import sys; s=sum(ord(c) for c in sys.argv[1]); print(28000 + (s % 1000))' "${RUN_ID}")}"
OPENCLAW_ENV="${OPENCLAW_ENV:-${OPENCLAW_STATE_DIR}/openviking.env}"
OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-${RUN_ID}}"
OPENVIKING_PORT="${OPENVIKING_PORT:-$(python3 -c 'import sys; s=sum(ord(c) for c in sys.argv[1]); print(22000 + (s % 1000))' "${RUN_ID}")}"
OV_CONF_PATH="${OV_CONF_PATH:-${OPENVIKING_INSTANCE_DIR}/ov.conf}"
OV_DATA_DIR="${OV_DATA_DIR:-${OPENVIKING_INSTANCE_DIR}/data}"
OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-/root/.openviking/venv-0.3.24/bin/python}"
OV_LOG="${OV_LOG:-/tmp/${RUN_ID}_openviking.log}"
GW_LOG="${GW_LOG:-/tmp/${RUN_ID}_openclaw_gateway.log}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

TMP_TAR="$(mktemp)"
trap 'rm -f "${TMP_TAR}"' EXIT

tar czf "${TMP_TAR}" -C "${WORKSPACE_ROOT}" locomo_test memory_bench_platform
cat "${TMP_TAR}" | ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} bash -lc 'rm -rf ${REMOTE_ROOT} && mkdir -p /tmp && tar xzf - -C /tmp'"

ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} bash -s" <<INNER
set -euo pipefail

LOCK_DIR="${REMOTE_LOCK_DIR}"
mkdir -p "\$LOCK_DIR"
LOCK_FILE="\$LOCK_DIR/locomo_test_remote.lock"
if [ -f "\$LOCK_FILE" ]; then
  echo "LOCKED:\$LOCK_FILE" >&2
  exit 2
fi
cleanup() {
  rm -f "\$LOCK_FILE"
  if [ -n "\${MONITOR_PID:-}" ]; then
    kill "\${MONITOR_PID}" >/dev/null 2>&1 || true
  fi
  pkill -f "${OPENCLAW_STATE_DIR}" >/dev/null 2>&1 || true
  pkill -f "${OV_CONF_PATH}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
echo \$\$ > "\$LOCK_FILE"

cd "${REMOTE_ROOT}"
export PYTHONPATH="/tmp/locomo_test:/tmp/memory_bench_platform"
mkdir -p "${REMOTE_OUTPUT_DIR}" "${REMOTE_MONITOR_DIR}"

python3 - "${REMOTE_MONITOR_DIR}" <<'PY' &
from __future__ import annotations

import csv
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

out_dir = Path(sys.argv[1])
out_dir.mkdir(parents=True, exist_ok=True)
cpu_path = out_dir / "cpu_status.csv"
mem_path = out_dir / "mem_status.csv"
with cpu_path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerow(["timestamp", "summary_util_user", "summary_util_sys", "summary_util_idle"])
with mem_path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerow(["timestamp", "mem_free_mb", "mem_used_mb"])

running = True

def _stop(signum, frame):
    del signum, frame
    global running
    running = False

signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

def _read_cpu():
    cpu_line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    parts = cpu_line.split()[1:]
    values = [float(item) for item in parts[:7]]
    total = sum(values) or 1.0
    return round(values[0] / total * 100, 2), round(values[2] / total * 100, 2), round(values[3] / total * 100, 2)

def _read_mem():
    meminfo = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        meminfo[key] = float(value.strip().split()[0])
    total_mb = meminfo.get("MemTotal", 0.0) / 1024.0
    available_mb = meminfo.get("MemAvailable", 0.0) / 1024.0
    used_mb = max(0.0, total_mb - available_mb)
    return round(available_mb, 2), round(used_mb, 2)

while running:
    ts = datetime.now().isoformat()
    user, system, idle = _read_cpu()
    free_mb, used_mb = _read_mem()
    with cpu_path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([ts, user, system, idle])
    with mem_path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([ts, free_mb, used_mb])
    time.sleep(1.0)
PY
MONITOR_PID=$!

python3 - "${REMOTE_ROOT}/configs/${LOCOMO_TEST_CONFIG}" "${RUN_ID}" "${OV_CONF_PATH}" "${OV_DATA_DIR}" "${OPENVIKING_PORT}" <<'PY'
import json
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

config_path = Path(sys.argv[1])
run_id = sys.argv[2]
ov_conf_path = Path(sys.argv[3])
ov_data_dir = Path(sys.argv[4])
ov_port = int(sys.argv[5])

runtime_cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
runtime_user = str(runtime_cfg.get("general", {}).get("user", "eval-1"))
payload = {
    "account_id": f"acct-{run_id}",
    "user_id": runtime_user,
}
base_ov_conf = Path("/root/.openviking/ov.conf")
ov_conf = json.loads(base_ov_conf.read_text(encoding="utf-8"))
ov_conf.setdefault("server", {})["port"] = ov_port
ov_conf.setdefault("storage", {})["workspace"] = str(ov_data_dir)
ov_conf.setdefault("memory", {}).pop("wm_v2_preprocess_enabled", None)
ov_conf_path.parent.mkdir(parents=True, exist_ok=True)
ov_data_dir.mkdir(parents=True, exist_ok=True)
ov_conf_path.write_text(json.dumps(ov_conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
PY

nohup "${OPENVIKING_PYTHON_BIN}" -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1 >"${OV_LOG}" 2>&1 &
for _ in \$(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${OPENVIKING_PORT}/health" >/tmp/"${RUN_ID}"_ov_health.json 2>/dev/null; then
    cat /tmp/"${RUN_ID}"_ov_health.json
    break
  fi
  sleep 1
done

if ! curl -fsS "http://127.0.0.1:${OPENVIKING_PORT}/health" >/dev/null 2>&1; then
  echo "failed to start isolated openviking" >&2
  tail -n 120 "${OV_LOG}" 2>/dev/null || true
  exit 1
fi

PYTHONPATH="${PYTHONPATH}" python3 -m locomo_test.bootstrap_remote_runtime \
  --base-state-dir /root/.openclaw \
  --base-ov-conf "${OV_CONF_PATH}" \
  --state-dir "${OPENCLAW_STATE_DIR}" \
  --home-dir "${OPENCLAW_HOME_DIR}" \
  --config-path "${OPENCLAW_CONFIG_PATH}" \
  --env-path "${OPENCLAW_ENV}" \
  --gateway-port "${OPENCLAW_GATEWAY_PORT}" \
  --run-id "${RUN_ID}" \
  --runtime-config-src "${REMOTE_ROOT}/configs/${LOCOMO_TEST_CONFIG}" \
  --runtime-config-dst "${REMOTE_ROOT}/configs/${LOCOMO_TEST_CONFIG%.toml}-runtime.toml" \
  --output-dir "/tmp/locomo_test_output"

# shellcheck disable=SC1090
source "${OPENCLAW_ENV}" 2>/dev/null || true
nohup env HOME="${OPENCLAW_HOME_DIR}" OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" OPENVIKING_BASE_URL="${OPENVIKING_BASE_URL:-http://127.0.0.1:${OPENVIKING_PORT}}" OPENVIKING_API_KEY="${OPENVIKING_API_KEY:-}" openclaw gateway >"${GW_LOG}" 2>&1 &
for _ in \$(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/health" >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then
    cat /tmp/"${RUN_ID}"_gw_health.json
    break
  fi
  sleep 1
done

if ! curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/health" >/dev/null 2>&1; then
  echo "failed to start isolated gateway" >&2
  tail -n 120 "${GW_LOG}" 2>/dev/null || true
  exit 1
fi

PYTHONPATH="${PYTHONPATH}" python3 -m locomo_test.cli run "configs/${LOCOMO_TEST_CONFIG%.toml}-runtime.toml"
INNER

mkdir -p "${LOCAL_OUTPUT_ROOT}"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'tar czf - -C /tmp/locomo_test_output ${RUN_ID}'" \
  | tar xzf - -C "${LOCAL_OUTPUT_ROOT}"

if [ -d "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" ] && [ "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" != "${LOCAL_OUTPUT_DIR}" ]; then
  rm -rf "${LOCAL_OUTPUT_DIR}"
  mv "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" "${LOCAL_OUTPUT_DIR}"
fi
