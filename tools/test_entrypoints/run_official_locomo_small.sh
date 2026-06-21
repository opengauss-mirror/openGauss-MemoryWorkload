#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="${SSH_HOST:-jcp@123.60.114.206}"
SSH_PORT="${SSH_PORT:-10008}"
REMOTE_CONTAINER="${REMOTE_CONTAINER:-jcp-dev}"
REMOTE_BENCH_DIR="${REMOTE_BENCH_DIR:-/home/jcp/agent/code/OpenViking/benchmark/locomo/openclaw}"
REMOTE_LOCK_DIR="${REMOTE_LOCK_DIR:-/tmp/locomo-entrypoint-locks}"
REMOTE_RUNTIME_LOCK_FILE="${REMOTE_LOCK_DIR}/official_small_runtime.lock"

MODE="${MODE:-on}"
SAMPLE="${SAMPLE:-0}"
SESSIONS="${SESSIONS:-1-4}"
JUDGE_PARALLEL="${JUDGE_PARALLEL:-5}"
SKIP_JUDGE="${SKIP_JUDGE:-false}"
QA_DISABLE_AUTOCAPTURE="${QA_DISABLE_AUTOCAPTURE:-}"
FAIL_ON_OV_INCOMPATIBLE_EXTRACTION="${FAIL_ON_OV_INCOMPATIBLE_EXTRACTION:-true}"
LOCOMO_EVAL_MODEL="${LOCOMO_EVAL_MODEL:-}"
RUN_ID="${RUN_ID:-official_${MODE}_sample${SAMPLE}_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/${RUN_ID}}"
LOCAL_OUTPUT_DIR="${OUTPUT_DIR}"
REMOTE_OUTPUT_DIR="/tmp/${RUN_ID}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-${RUN_ID}}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"
OPENCLAW_AGENT_DIR="${OPENCLAW_AGENT_DIR:-${OPENCLAW_STATE_DIR}/agents/locomo-eval}"
OPENCLAW_MAIN_AGENT_DIR="${OPENCLAW_MAIN_AGENT_DIR:-${OPENCLAW_STATE_DIR}/agents/main/agent}"
OPENCLAW_ENV="${OPENCLAW_ENV:-${OPENCLAW_STATE_DIR}/openviking.env}"
OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-$(python3 -c 'import sys; s=sum(ord(c) for c in sys.argv[1]); print(28000 + (s % 1000))' "${RUN_ID}")}"
OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-${RUN_ID}}"
OPENVIKING_PORT="${OPENVIKING_PORT:-$(python3 -c 'import sys; s=sum(ord(c) for c in sys.argv[1]); print(21000 + (s % 1000))' "${RUN_ID}")}"
OV_CONF_PATH="${OV_CONF_PATH:-${OPENVIKING_INSTANCE_DIR}/ov.conf}"
OV_DATA_DIR="${OV_DATA_DIR:-${OPENVIKING_INSTANCE_DIR}/data}"
EXPECTED_OPENVIKING_VERSION="${MEMORY_BENCH_EXPECTED_OPENVIKING_VERSION:-}"
EXPECTED_OPENCLAW_VERSION="${MEMORY_BENCH_EXPECTED_OPENCLAW_VERSION:-}"
EXPECTED_LOCOMO_BENCHMARK_VERSION="${MEMORY_BENCH_EXPECTED_LOCOMO_BENCHMARK_VERSION:-}"
OPENVIKING_INTROSPECT_PYTHON_BIN="${OPENVIKING_INTROSPECT_PYTHON_BIN:-}"

acquire_remote_runtime_lock() {
  ssh -p "${SSH_PORT}" "${SSH_HOST}" "bash -lc 'mkdir -p \"${REMOTE_LOCK_DIR}\" && if [ -f \"${REMOTE_RUNTIME_LOCK_FILE}\" ]; then echo LOCKED:${REMOTE_RUNTIME_LOCK_FILE}; exit 2; fi; echo $$ > \"${REMOTE_RUNTIME_LOCK_FILE}\"'"
}

release_remote_runtime_lock() {
  ssh -p "${SSH_PORT}" "${SSH_HOST}" "bash -lc 'rm -f \"${REMOTE_RUNTIME_LOCK_FILE}\"'" >/dev/null 2>&1 || true
}

trap release_remote_runtime_lock EXIT INT TERM
acquire_remote_runtime_lock

PREPARE_ARGS=(
  --ssh-host "${SSH_HOST}"
  --ssh-port "${SSH_PORT}"
  --remote-container "${REMOTE_CONTAINER}"
  --benchmark-dir "${REMOTE_BENCH_DIR}"
)
if [ -n "${LOCOMO_EVAL_MODEL}" ]; then
  PREPARE_ARGS+=(--locomo-model "${LOCOMO_EVAL_MODEL}")
fi
python3 "${SCRIPT_DIR}/prepare_remote_locomo_runtime.py" "${PREPARE_ARGS[@]}"

REMOTE_CFG_JSON="$(
  ssh -p "${SSH_PORT}" "${SSH_HOST}" \
    "docker exec ${REMOTE_CONTAINER} python3 -c 'import json; cfg=json.load(open(\"/root/.openviking/ov.conf\")); print(json.dumps({\"root_key\": cfg[\"server\"][\"root_api_key\"], \"seed_key\": cfg[\"vlm\"][\"api_key\"], \"base_url\": cfg[\"vlm\"][\"api_base\"], \"model\": cfg[\"vlm\"][\"model\"]}))'"
)"

ROOT_KEY="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["root_key"])' "${REMOTE_CFG_JSON}")"
SEED_KEY="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["seed_key"])' "${REMOTE_CFG_JSON}")"
BASE_URL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["base_url"])' "${REMOTE_CFG_JSON}")"
JUDGE_MODEL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["model"])' "${REMOTE_CFG_JSON}")"

resolve_openviking_introspect_python() {
  if [ -n "${OPENVIKING_INTROSPECT_PYTHON_BIN}" ]; then
    echo "${OPENVIKING_INTROSPECT_PYTHON_BIN}"
    return 0
  fi

  local -a candidates=()
  if [ -n "${EXPECTED_OPENVIKING_VERSION}" ]; then
    candidates+=("/root/.openviking/venv-${EXPECTED_OPENVIKING_VERSION}/bin/python")
    candidates+=("/root/.openviking/${EXPECTED_OPENVIKING_VERSION}/bin/python")
  fi
  candidates+=(
    "/root/.openviking/venv/bin/python"
    "/root/.openviking/venv-0.3.24/bin/python"
    "python3"
  )

  local candidate=""
  for candidate in "${candidates[@]}"; do
    if ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'command -v ${candidate} >/dev/null 2>&1'" >/dev/null 2>&1; then
      OPENVIKING_INTROSPECT_PYTHON_BIN="${candidate}"
      echo "${OPENVIKING_INTROSPECT_PYTHON_BIN}"
      return 0
    fi
  done

  echo "python3"
}

OPENVIKING_INTROSPECT_PYTHON_BIN="$(resolve_openviking_introspect_python)"

check_remote_runtime_versions() {
  local remote_json
  remote_json="$(
    ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc '${OPENVIKING_INTROSPECT_PYTHON_BIN} - <<\"PY\"
import json
import re
import subprocess
try:
    import openviking
    ov_version = getattr(openviking, \"__version__\", \"unknown\")
except Exception as exc:
    ov_version = f\"error:{exc}\"
try:
    oc_version = subprocess.check_output([\"openclaw\", \"--version\"], text=True).strip()
except Exception as exc:
    oc_version = f\"error:{exc}\"
print(json.dumps({\"openviking_version\": ov_version, \"openclaw_version\": oc_version}, ensure_ascii=False))
PY'"
  )"
  local actual_ov actual_oc expected_ov expected_oc
  actual_ov="$(python3 -c 'import json,re,sys; value=json.loads(sys.argv[1])["openviking_version"]; m=re.search(r"(v?\\d+(?:\\.\\d+){2,3})", value or ""); print((m.group(1).lstrip("v") if m else value))' "${remote_json}")"
  actual_oc="$(python3 -c 'import json,re,sys; value=json.loads(sys.argv[1])["openclaw_version"]; m=re.search(r"(v?\\d+(?:\\.\\d+){2,3})", value or ""); print((m.group(1).lstrip("v") if m else value))' "${remote_json}")"
  expected_ov="$(python3 -c 'import re,sys; value=sys.argv[1]; m=re.search(r"(v?\\d+(?:\\.\\d+){2,3})", value or ""); print((m.group(1).lstrip("v") if m else value))' "${EXPECTED_OPENVIKING_VERSION}")"
  expected_oc="$(python3 -c 'import re,sys; value=sys.argv[1]; m=re.search(r"(v?\\d+(?:\\.\\d+){2,3})", value or ""); print((m.group(1).lstrip("v") if m else value))' "${EXPECTED_OPENCLAW_VERSION}")"

  if [ -n "${expected_ov}" ] && [ "${actual_ov}" != "${expected_ov}" ]; then
    echo "OpenViking runtime version mismatch: expected ${expected_ov}, got ${actual_ov}" >&2
    exit 11
  fi
  if [ -n "${expected_oc}" ] && [ "${actual_oc}" != "${expected_oc}" ]; then
    echo "OpenClaw runtime version mismatch: expected ${expected_oc}, got ${actual_oc}" >&2
    exit 12
  fi
}

check_remote_runtime_versions

capture_remote_preflight_local() {
  ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc '${OPENVIKING_INTROSPECT_PYTHON_BIN} - <<\"PY\"
import inspect
import json
import re
import subprocess
import requests
from openviking.session.compressor_v2 import SessionCompressorV2
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider

headers = {\"X-API-Key\": \"${ROOT_KEY}\"}
base = \"http://127.0.0.1:1933\"

def fetch(path):
    try:
        resp = requests.get(base + path, headers=headers, timeout=20)
        body = resp.json() if resp.headers.get(\"content-type\", \"\").startswith(\"application/json\") else resp.text
        return {\"status_code\": resp.status_code, \"body\": body}
    except Exception as exc:
        return {\"error\": str(exc)}

provider_sig = inspect.signature(SessionExtractContextProvider.__init__)
long_sig = inspect.signature(SessionCompressorV2.extract_long_term_memories)
agent_attr = getattr(SessionCompressorV2, \"extract_agent_memories\", None)
agent_sig = inspect.signature(agent_attr) if agent_attr else None
try:
    openclaw_version_proc = subprocess.run([\"openclaw\", \"--version\"], text=True, capture_output=True, timeout=20, check=False)
    openclaw_version_stdout = openclaw_version_proc.stdout.strip()
    openclaw_version_stderr = openclaw_version_proc.stderr.strip()
    openclaw_version_exit_code = openclaw_version_proc.returncode
except Exception as exc:
    openclaw_version_stdout = \"\"
    openclaw_version_stderr = str(exc)
    openclaw_version_exit_code = -1

try:
    openviking_git_describe_proc = subprocess.run(
        [\"git\", \"-C\", \"/home/jcp/agent/code/OpenViking\", \"describe\", \"--tags\", \"--always\", \"--dirty\"],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    openviking_git_describe = openviking_git_describe_proc.stdout.strip() or openviking_git_describe_proc.stderr.strip()
except Exception as exc:
    openviking_git_describe = f\"error:{exc}\"

payload = {
    \"run_id\": \"${RUN_ID}\",
    \"snapshot\": \"preflight\",
    \"expected_versions\": {
        \"openviking\": \"${EXPECTED_OPENVIKING_VERSION}\",
        \"openclaw\": \"${EXPECTED_OPENCLAW_VERSION}\",
        \"locomo_benchmark\": \"${EXPECTED_LOCOMO_BENCHMARK_VERSION}\",
    },
    \"health\": fetch(\"/health\"),
    \"openviking_git_describe\": openviking_git_describe,
    \"openclaw_version\": {
        \"stdout\": openclaw_version_stdout,
        \"stderr\": openclaw_version_stderr,
        \"exit_code\": openclaw_version_exit_code,
    },
    \"extract_compatibility\": {
        \"session_extract_context_provider\": {
            \"file\": inspect.getsourcefile(SessionExtractContextProvider),
            \"params\": list(provider_sig.parameters.keys()),
            \"accepts_latest_archive_session_time\": \"latest_archive_session_time\" in provider_sig.parameters,
        },
        \"extract_long_term_memories\": {
            \"file\": inspect.getsourcefile(SessionCompressorV2.extract_long_term_memories),
            \"params\": list(long_sig.parameters.keys()),
            \"accepts_latest_archive_overview\": \"latest_archive_overview\" in long_sig.parameters,
            \"accepts_latest_archive_session_time\": \"latest_archive_session_time\" in long_sig.parameters,
        },
        \"extract_agent_memories\": {
            \"exists\": agent_attr is not None,
            \"file\": inspect.getsourcefile(agent_attr) if agent_attr else None,
            \"params\": list(agent_sig.parameters.keys()) if agent_sig else [],
            \"accepts_latest_archive_overview\": (\"latest_archive_overview\" in agent_sig.parameters) if agent_sig else False,
            \"accepts_latest_archive_session_time\": (\"latest_archive_session_time\" in agent_sig.parameters) if agent_sig else False,
        },
    },
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY'"
}

capture_remote_postrun_local() {
  ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc '${OPENVIKING_INTROSPECT_PYTHON_BIN} - <<\"PY\"
import json
import requests

headers = {\"X-API-Key\": \"${ROOT_KEY}\"}
base = \"http://127.0.0.1:1933\"

def fetch(path):
    try:
        resp = requests.get(base + path, headers=headers, timeout=20)
        body = resp.json() if resp.headers.get(\"content-type\", \"\").startswith(\"application/json\") else resp.text
        return {\"status_code\": resp.status_code, \"body\": body}
    except Exception as exc:
        return {\"error\": str(exc)}

payload = {
    \"run_id\": \"${RUN_ID}\",
    \"snapshot\": \"postrun\",
    \"expected_versions\": {
        \"openviking\": \"${EXPECTED_OPENVIKING_VERSION}\",
        \"openclaw\": \"${EXPECTED_OPENCLAW_VERSION}\",
        \"locomo_benchmark\": \"${EXPECTED_LOCOMO_BENCHMARK_VERSION}\",
    },
    \"health\": fetch(\"/health\"),
    \"observer_system\": fetch(\"/api/v1/observer/system\"),
    \"observer_models\": fetch(\"/api/v1/observer/models\"),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
PY'"
}

mkdir -p "${LOCAL_OUTPUT_DIR}"
mkdir -p "${LOCAL_OUTPUT_DIR}/remote_logs"

LOCAL_PREFLIGHT_JSON="${LOCAL_OUTPUT_DIR}/remote_logs/${RUN_ID}.preflight.json"
capture_remote_preflight_local > "${LOCAL_PREFLIGHT_JSON}"

if [ "${FAIL_ON_OV_INCOMPATIBLE_EXTRACTION}" = "true" ]; then
  python3 - "${LOCAL_PREFLIGHT_JSON}" <<'PY'
import json
import re
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
start = raw.find("{")
if start < 0:
    print("Preflight snapshot JSON payload not found.", file=sys.stderr)
    sys.exit(2)
payload = json.loads(raw[start:])
compat = payload.get("extract_compatibility") or {}
provider_ok = bool(
    ((compat.get("session_extract_context_provider") or {}).get("accepts_latest_archive_session_time"))
)
agent_ok = bool(
    ((compat.get("extract_agent_memories") or {}).get("accepts_latest_archive_overview"))
    and ((compat.get("extract_agent_memories") or {}).get("accepts_latest_archive_session_time"))
)
if not provider_ok or not agent_ok:
    print(
        "OpenViking extraction compatibility check failed before benchmark run. "
        "See preflight snapshot for details.",
        file=sys.stderr,
    )
    sys.exit(3)


def normalize_version(value):
    if not isinstance(value, str):
        return None
    match = re.search(r"(v?\d+(?:\.\d+){2,3})", value)
    if not match:
        return None
    return match.group(1).lstrip("v")


expected_versions = payload.get("expected_versions") or {}
expected_openviking = normalize_version(expected_versions.get("openviking"))
expected_openclaw = normalize_version(expected_versions.get("openclaw"))
health_body = (payload.get("health") or {}).get("body") or {}
actual_openviking = normalize_version(
    health_body.get("version") or payload.get("openviking_git_describe")
)
openclaw_version = payload.get("openclaw_version") or {}
actual_openclaw = normalize_version(
    openclaw_version.get("stdout") or openclaw_version.get("stderr")
)
if expected_openviking and actual_openviking and expected_openviking != actual_openviking:
    print(
        f"OpenViking runtime version mismatch: expected {expected_openviking}, got {actual_openviking}",
        file=sys.stderr,
    )
    sys.exit(4)
if expected_openclaw and actual_openclaw and expected_openclaw != actual_openclaw:
    print(
        f"OpenClaw runtime version mismatch: expected {expected_openclaw}, got {actual_openclaw}",
        file=sys.stderr,
    )
    sys.exit(5)
PY
fi

EXISTING_PHASE_CSV="$(find "${LOCAL_OUTPUT_DIR}" -maxdepth 1 -name 'phaseA*.csv' | head -1 || true)"
if [ -z "${EXISTING_PHASE_CSV}" ]; then

set +e
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} bash -s" <<INNER
set -euo pipefail

LOCK_DIR="${REMOTE_LOCK_DIR}"
mkdir -p "\$LOCK_DIR"
LOCK_FILE="\$LOCK_DIR/locomo_eval.lock"
if [ -f "\$LOCK_FILE" ]; then
  echo "LOCKED:\$LOCK_FILE" >&2
  exit 2
fi
cleanup() {
  rm -f "\$LOCK_FILE"
}
trap cleanup EXIT INT TERM
echo \$\$ > "\$LOCK_FILE"

EXISTING_PHASE_RUNS="\$(pgrep -af \"phase_a_off.py\" || true)"
if [ -n "\${EXISTING_PHASE_RUNS}" ]; then
  echo "RUN_CONFLICT:\${EXISTING_PHASE_RUNS}" >&2
  exit 3
fi

cd "${REMOTE_BENCH_DIR}"
TOKEN=\$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["gateway"]["auth"]["token"])' "${OPENCLAW_CONFIG_PATH}")

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
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}"
export OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}"
export OPENCLAW_AGENT_DIR="${OPENCLAW_AGENT_DIR}"
export OPENCLAW_MAIN_AGENT_DIR="${OPENCLAW_MAIN_AGENT_DIR}"
export OPENCLAW_ENV="${OPENCLAW_ENV}"
export OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT}"
export OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR}"
export OPENVIKING_PORT="${OPENVIKING_PORT}"
export OV_CONF_PATH="${OV_CONF_PATH}"
export OV_DATA_DIR="${OV_DATA_DIR}"

bash ./run_clean_small_in_container.sh

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
REMOTE_RUN_EXIT_CODE=$?
set -e

capture_remote_postrun_local > "${LOCAL_OUTPUT_DIR}/remote_logs/${RUN_ID}.postrun.json"

TMP_PARENT="$(dirname "${LOCAL_OUTPUT_DIR}")"
mkdir -p "${TMP_PARENT}"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'tar czf - -C /tmp ${RUN_ID} ${RUN_ID}.master.log ${RUN_ID}.ov.log ${RUN_ID}.gw.log ${RUN_ID}.enrich.json ${RUN_ID}.preflight.json 2>/dev/null || tar czf - -C /tmp ${RUN_ID}'" \
  | tar xzf - -C "${TMP_PARENT}"

if [ -d "${TMP_PARENT}/${RUN_ID}" ] && [ "${TMP_PARENT}/${RUN_ID}" != "${LOCAL_OUTPUT_DIR}" ]; then
  rm -rf "${LOCAL_OUTPUT_DIR}"
  mv "${TMP_PARENT}/${RUN_ID}" "${LOCAL_OUTPUT_DIR}"
fi

for log_name in "${RUN_ID}.master.log" "${RUN_ID}.ov.log" "${RUN_ID}.gw.log"; do
  if [ -f "${TMP_PARENT}/${log_name}" ]; then
    mv "${TMP_PARENT}/${log_name}" "${LOCAL_OUTPUT_DIR}/remote_logs/${log_name}"
  fi
done
for snapshot_name in "${RUN_ID}.preflight.json"; do
  if [ -f "${TMP_PARENT}/${snapshot_name}" ]; then
    mv "${TMP_PARENT}/${snapshot_name}" "${LOCAL_OUTPUT_DIR}/remote_logs/${snapshot_name}"
  fi
done
if [ -f "${TMP_PARENT}/${RUN_ID}.enrich.json" ]; then
  mv "${TMP_PARENT}/${RUN_ID}.enrich.json" "${LOCAL_OUTPUT_DIR}/remote_logs/${RUN_ID}.enrich.json"
fi

if [ "${REMOTE_RUN_EXIT_CODE}" -ne 0 ]; then
  exit "${REMOTE_RUN_EXIT_CODE}"
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
