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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

TMP_TAR="$(mktemp)"
trap 'rm -f "${TMP_TAR}"' EXIT

tar czf "${TMP_TAR}" -C "${WORKSPACE_ROOT}" locomo_test
cat "${TMP_TAR}" | ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} bash -lc 'rm -rf ${REMOTE_ROOT} && mkdir -p /tmp && tar xzf - -C /tmp'"

REMOTE_CFG_JSON="$(
  ssh -p "${SSH_PORT}" "${SSH_HOST}" \
    "docker exec ${REMOTE_CONTAINER} python3 -c 'import json; oc=json.load(open(\"/root/.openclaw/openclaw.json\")); ov=json.load(open(\"/root/.openviking/ov.conf\")); print(json.dumps({\"gateway_port\": oc[\"gateway\"][\"port\"], \"gateway_token\": oc[\"gateway\"][\"auth\"][\"token\"], \"state_dir\": oc.get(\"stateDir\") or \"/root/.openclaw\", \"ov_port\": ov[\"server\"][\"port\"], \"judge_key\": ov[\"vlm\"][\"api_key\"], \"judge_base_url\": ov[\"vlm\"][\"api_base\"], \"judge_model\": ov[\"vlm\"][\"model\"]}))'"
)"

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
}
trap cleanup EXIT INT TERM
echo \$\$ > "\$LOCK_FILE"

cd "${REMOTE_ROOT}"

python3 - <<'PY'
import json
from pathlib import Path

cfg = json.loads("""${REMOTE_CFG_JSON}""")
env_toml = Path("${REMOTE_ROOT}/configs/env.toml")
env_toml.write_text(
    f"""[gateway]
port = {cfg['gateway_port']}
token = "{cfg['gateway_token']}"
state_dir = "{cfg['state_dir']}"

[openviking]
port = {cfg['ov_port']}

[judge]
api_key = "{cfg['judge_key']}"
base_url = "{cfg['judge_base_url']}"
model = "{cfg['judge_model']}"
api_format = "openai"
parallel = 5
""",
    encoding="utf-8",
)
PY

python3 - <<'PY'
from pathlib import Path

src = Path("${REMOTE_ROOT}/configs/openviking-small-stable.toml")
dst = Path("${REMOTE_ROOT}/configs/openviking-small-stable-runtime.toml")
text = src.read_text(encoding="utf-8")
text = text.replace('name = "openviking-small-stable"', 'name = "${RUN_ID}"')
text = text.replace('[general]\n', '[general]\noutput_dir = "/tmp/locomo_test_output"\n', 1)
dst.write_text(text, encoding="utf-8")
PY

PYTHONPATH="${REMOTE_ROOT}" python3 -m locomo_test.cli run configs/openviking-small-stable-runtime.toml --skip health_check
INNER

mkdir -p "${LOCAL_OUTPUT_ROOT}"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'tar czf - -C /tmp/locomo_test_output ${RUN_ID}'" \
  | tar xzf - -C "${LOCAL_OUTPUT_ROOT}"

if [ -d "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" ] && [ "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" != "${LOCAL_OUTPUT_DIR}" ]; then
  rm -rf "${LOCAL_OUTPUT_DIR}"
  mv "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" "${LOCAL_OUTPUT_DIR}"
fi
