#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="${SSH_HOST:-jcp@123.60.114.206}"
SSH_PORT="${SSH_PORT:-10008}"
REMOTE_CONTAINER="${REMOTE_CONTAINER:-jcp-dev}"
REMOTE_BENCH_DIR="${REMOTE_BENCH_DIR:-/home/jcp/agent/code/OpenViking/benchmark/locomo/openclaw}"
REMOTE_LOCK_DIR="${REMOTE_LOCK_DIR:-/tmp/locomo-entrypoint-locks}"

MODE="${MODE:-on}"
SAMPLE="${SAMPLE:-0}"
SESSIONS="${SESSIONS:-1-4}"
JUDGE_PARALLEL="${JUDGE_PARALLEL:-5}"
SKIP_JUDGE="${SKIP_JUDGE:-false}"
RUN_ID="${RUN_ID:-official_${MODE}_sample${SAMPLE}_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/${RUN_ID}}"
LOCAL_OUTPUT_DIR="${OUTPUT_DIR}"
REMOTE_OUTPUT_DIR="/tmp/${RUN_ID}"

mkdir -p "${LOCAL_OUTPUT_DIR}"

ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} bash -s" <<INNER
set -euo pipefail

LOCK_DIR="${REMOTE_LOCK_DIR}"
mkdir -p "\$LOCK_DIR"
LOCK_FILE="\$LOCK_DIR/official_locomo_small.lock"
if [ -f "\$LOCK_FILE" ]; then
  echo "LOCKED:\$LOCK_FILE" >&2
  exit 2
fi
cleanup() {
  rm -f "\$LOCK_FILE"
}
trap cleanup EXIT INT TERM
echo \$\$ > "\$LOCK_FILE"

cd "${REMOTE_BENCH_DIR}"
TOKEN=\$(python3 -c 'import json;print(json.load(open("/root/.openclaw/openclaw.json"))["gateway"]["auth"]["token"])')
ROOT_KEY=\$(python3 -c 'import json;print(json.load(open("/root/.openviking/ov.conf"))["server"]["root_api_key"])')
SEED_KEY=\$(python3 -c 'import json;print(json.load(open("/root/.openviking/ov.conf"))["vlm"]["api_key"])')
BASE_URL=\$(python3 -c 'import json;print(json.load(open("/root/.openviking/ov.conf"))["vlm"]["api_base"])')

export OPENCLAW_GATEWAY_TOKEN="\$TOKEN"
export OPENVIKING_ROOT_API_KEY="\$ROOT_KEY"
export OPENAI_API_KEY="\$SEED_KEY"
export ARK_API_KEY="\$SEED_KEY"
export OPENAI_BASE_URL="\$BASE_URL"
export MODE="${MODE}"
export SAMPLE="${SAMPLE}"
export SESSIONS="${SESSIONS}"
export JUDGE_PARALLEL="${JUDGE_PARALLEL}"
export SKIP_JUDGE="${SKIP_JUDGE}"
export RUN_ID="${RUN_ID}"
export OUTPUT_DIR="${REMOTE_OUTPUT_DIR}"
export MASTER_LOG="/tmp/${RUN_ID}.master.log"
export OV_LOG="/tmp/${RUN_ID}.ov.log"
export GW_LOG="/tmp/${RUN_ID}.gw.log"

bash ./run_clean_small_in_container.sh
INNER

TMP_PARENT="$(dirname "${LOCAL_OUTPUT_DIR}")"
mkdir -p "${TMP_PARENT}"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'tar czf - -C /tmp ${RUN_ID} ${RUN_ID}.master.log ${RUN_ID}.ov.log ${RUN_ID}.gw.log 2>/dev/null || tar czf - -C /tmp ${RUN_ID}'" \
  | tar xzf - -C "${TMP_PARENT}"

if [ -d "${TMP_PARENT}/${RUN_ID}" ] && [ "${TMP_PARENT}/${RUN_ID}" != "${LOCAL_OUTPUT_DIR}" ]; then
  rm -rf "${LOCAL_OUTPUT_DIR}"
  mv "${TMP_PARENT}/${RUN_ID}" "${LOCAL_OUTPUT_DIR}"
fi

mkdir -p "${LOCAL_OUTPUT_DIR}/remote_logs"
for log_name in "${RUN_ID}.master.log" "${RUN_ID}.ov.log" "${RUN_ID}.gw.log"; do
  if [ -f "${TMP_PARENT}/${log_name}" ]; then
    mv "${TMP_PARENT}/${log_name}" "${LOCAL_OUTPUT_DIR}/remote_logs/${log_name}"
  fi
done
