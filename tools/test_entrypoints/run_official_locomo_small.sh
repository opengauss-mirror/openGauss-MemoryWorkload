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
QA_DISABLE_AUTOCAPTURE="${QA_DISABLE_AUTOCAPTURE:-}"
RUN_ID="${RUN_ID:-official_${MODE}_sample${SAMPLE}_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/${RUN_ID}}"
LOCAL_OUTPUT_DIR="${OUTPUT_DIR}"
REMOTE_OUTPUT_DIR="/tmp/${RUN_ID}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

python3 "${SCRIPT_DIR}/prepare_remote_locomo_runtime.py" \
  --ssh-host "${SSH_HOST}" \
  --ssh-port "${SSH_PORT}" \
  --remote-container "${REMOTE_CONTAINER}" \
  --benchmark-dir "${REMOTE_BENCH_DIR}"

REMOTE_CFG_JSON="$(
  ssh -p "${SSH_PORT}" "${SSH_HOST}" \
    "docker exec ${REMOTE_CONTAINER} python3 -c 'import json; cfg=json.load(open(\"/root/.openviking/ov.conf\")); print(json.dumps({\"root_key\": cfg[\"server\"][\"root_api_key\"], \"seed_key\": cfg[\"vlm\"][\"api_key\"], \"base_url\": cfg[\"vlm\"][\"api_base\"], \"model\": cfg[\"vlm\"][\"model\"]}))'"
)"

ROOT_KEY="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["root_key"])' "${REMOTE_CFG_JSON}")"
SEED_KEY="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["seed_key"])' "${REMOTE_CFG_JSON}")"
BASE_URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["base_url"])' "${REMOTE_CFG_JSON}")"
JUDGE_MODEL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["model"])' "${REMOTE_CFG_JSON}")"

capture_remote_snapshot() {
  local snapshot_name="$1"
  ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'python3 - <<\"PY\" > /tmp/${RUN_ID}.${snapshot_name}.json
import json
import requests
import subprocess

headers = {"X-API-Key": "${ROOT_KEY}"}
base = "http://127.0.0.1:1933"

def fetch(path):
    try:
        resp = requests.get(base + path, headers=headers, timeout=20)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        return {"status_code": resp.status_code, "body": body}
    except Exception as exc:
        return {"error": str(exc)}

def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"ERROR: {exc}"

payload = {
    "run_id": "${RUN_ID}",
    "snapshot": "${snapshot_name}",
    "openviking_git_describe": run_cmd(["bash", "-lc", "cd ${REMOTE_BENCH_DIR%/benchmark/locomo/openclaw} && git describe --tags --always --dirty 2>/dev/null || true"]),
    "openviking_head": run_cmd(["bash", "-lc", "cd ${REMOTE_BENCH_DIR%/benchmark/locomo/openclaw} && git rev-parse --short HEAD 2>/dev/null || true"]),
    "health": fetch("/health"),
    "observer_system": fetch("/api/v1/observer/system"),
    "observer_models": fetch("/api/v1/observer/models"),
}
try:
    import inspect
    from openviking.session.compressor_v2 import SessionCompressorV2
    from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider

    provider_params = list(inspect.signature(SessionExtractContextProvider.__init__).parameters.keys())
    long_term_params = list(inspect.signature(SessionCompressorV2.extract_long_term_memories).parameters.keys())
    agent_params = list(inspect.signature(SessionCompressorV2.extract_agent_memories).parameters.keys())
    payload["extract_compatibility"] = {
        "session_extract_context_provider": {
            "file": inspect.getsourcefile(SessionExtractContextProvider),
            "params": provider_params,
            "accepts_latest_archive_session_time": "latest_archive_session_time" in provider_params,
        },
        "extract_long_term_memories": {
            "file": inspect.getsourcefile(SessionCompressorV2.extract_long_term_memories),
            "params": long_term_params,
            "accepts_latest_archive_overview": "latest_archive_overview" in long_term_params,
            "accepts_latest_archive_session_time": "latest_archive_session_time" in long_term_params,
        },
        "extract_agent_memories": {
            "file": inspect.getsourcefile(SessionCompressorV2.extract_agent_memories),
            "params": agent_params,
            "accepts_latest_archive_overview": "latest_archive_overview" in agent_params,
            "accepts_latest_archive_session_time": "latest_archive_session_time" in agent_params,
        },
    }
except Exception as exc:
    payload["extract_compatibility"] = {
        "error": str(exc),
    }
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY' >/dev/null"
}

mkdir -p "${LOCAL_OUTPUT_DIR}"

EXISTING_PHASE_CSV="$(find "${LOCAL_OUTPUT_DIR}" -maxdepth 1 -name 'phaseA*.csv' | head -1 || true)"
if [ -z "${EXISTING_PHASE_CSV}" ]; then

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

export OPENCLAW_GATEWAY_TOKEN="\$TOKEN"
export OPENVIKING_ROOT_API_KEY="${ROOT_KEY}"
export OPENAI_API_KEY="${SEED_KEY}"
export ARK_API_KEY="${SEED_KEY}"
export OPENAI_BASE_URL="${BASE_URL}"
export MODE="${MODE}"
export SAMPLE="${SAMPLE}"
export SESSIONS="${SESSIONS}"
export JUDGE_PARALLEL="${JUDGE_PARALLEL}"
export SKIP_JUDGE="${SKIP_JUDGE}"
export QA_DISABLE_AUTOCAPTURE="${QA_DISABLE_AUTOCAPTURE}"
export RUN_ID="${RUN_ID}"
export OUTPUT_DIR="${REMOTE_OUTPUT_DIR}"
export MASTER_LOG="/tmp/${RUN_ID}.master.log"
export OV_LOG="/tmp/${RUN_ID}.ov.log"
export GW_LOG="/tmp/${RUN_ID}.gw.log"

capture_remote_snapshot preflight

bash ./run_clean_small_in_container.sh

capture_remote_snapshot postrun

META_FILE=\$(find "${REMOTE_OUTPUT_DIR}" -maxdepth 1 -name 'phaseA*_meta.json' | head -1 || true)
CSV_FILE=\$(find "${REMOTE_OUTPUT_DIR}" -maxdepth 1 -name 'phaseA*.csv' | head -1 || true)
if [ -z "\${META_FILE}" ] && [ -n "\${CSV_FILE}" ]; then
  META_FILE="\${CSV_FILE%.csv}_meta.json"
fi
if [ -n "\${META_FILE}" ] && [ -f "\${MASTER_LOG}" ] && [ -n "\${CSV_FILE}" ]; then
  python3 "${WORKSPACE_ROOT}/tools/test_entrypoints/ov_phasea_enrich.py" \
    "\${META_FILE}" \
    "\${CSV_FILE}" \
    "\${MASTER_LOG}" \
    "http://127.0.0.1:1933" \
    "${ROOT_KEY}" \
    "acct-${RUN_ID}" \
    "user-${RUN_ID}" \
    "acct-${RUN_ID}_locomo-eval" \
    >/tmp/${RUN_ID}.enrich.json || true
fi
INNER

TMP_PARENT="$(dirname "${LOCAL_OUTPUT_DIR}")"
mkdir -p "${TMP_PARENT}"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'tar czf - -C /tmp ${RUN_ID} ${RUN_ID}.master.log ${RUN_ID}.ov.log ${RUN_ID}.gw.log ${RUN_ID}.enrich.json ${RUN_ID}.preflight.json ${RUN_ID}.postrun.json 2>/dev/null || tar czf - -C /tmp ${RUN_ID}'" \
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
for snapshot_name in "${RUN_ID}.preflight.json" "${RUN_ID}.postrun.json"; do
  if [ -f "${TMP_PARENT}/${snapshot_name}" ]; then
    mv "${TMP_PARENT}/${snapshot_name}" "${LOCAL_OUTPUT_DIR}/remote_logs/${snapshot_name}"
  fi
done
if [ -f "${TMP_PARENT}/${RUN_ID}.enrich.json" ]; then
  mv "${TMP_PARENT}/${RUN_ID}.enrich.json" "${LOCAL_OUTPUT_DIR}/remote_logs/${RUN_ID}.enrich.json"
fi

fi

if [ "${SKIP_JUDGE}" != "true" ]; then
  PHASE_CSV="$(find "${LOCAL_OUTPUT_DIR}" -maxdepth 1 -name 'phaseA*.csv' | head -1)"
  if [ -z "${PHASE_CSV}" ]; then
    echo "phaseA csv not found under ${LOCAL_OUTPUT_DIR}" >&2
    exit 1
  fi

  PYTHONPATH="${WORKSPACE_ROOT}/locomo_test" \
    python3 -m locomo_test.cli judge \
    --input "${PHASE_CSV}" \
    --token "${SEED_KEY}" \
    --base-url "${BASE_URL}" \
    --model "${JUDGE_MODEL}" \
    --parallel "${JUDGE_PARALLEL}"

  QA_CSV="${LOCAL_OUTPUT_DIR}/qa_results.csv"
  python3 - "${QA_CSV}" "${LOCAL_OUTPUT_DIR}/meta.json" "${RUN_ID}" <<'PY'
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

csv_path = Path(sys.argv[1])
meta_path = Path(sys.argv[2])
run_id = sys.argv[3]
rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
by_category = defaultdict(lambda: {"correct": 0, "total": 0, "accuracy": 0.0})
total_correct = 0
total_graded = 0
token_totals = {"input_tokens": 0, "output_tokens": 0, "cacheRead": 0, "cacheWrite": 0, "total_tokens": 0}

for row in rows:
    category = str(row.get("category") or "")
    result = str(row.get("result") or "").strip().upper()
    if not result:
      continue
    total_graded += 1
    by_category[category]["total"] += 1
    if result == "CORRECT":
      total_correct += 1
      by_category[category]["correct"] += 1
    for key in token_totals:
      token_totals[key] += int(row.get(key, 0) or 0)

for bucket in by_category.values():
    bucket["accuracy"] = round(bucket["correct"] / bucket["total"], 4) if bucket["total"] else 0.0

meta = {
    "name": run_id,
    "dataset": "small" if "sample0" in run_id or "small" in run_id else "unknown",
    "overall_accuracy": round(total_correct / total_graded, 4) if total_graded else 0.0,
    "total_correct": total_correct,
    "total_graded": total_graded,
    "total_questions": len(rows),
    "accuracy_by_category": dict(by_category),
    "token_totals": token_totals,
    "memory_token_totals": {},
}
meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
PY
fi
