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
PLATFORM_RUNS_ROOT="${PLATFORM_RUNS_ROOT:-${WORKSPACE_ROOT}/memory_bench_platform/runs}"
PLATFORM_IMPORT_ENABLED="${PLATFORM_IMPORT_ENABLED:-true}"
OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-${RUN_ID}}"
OPENCLAW_HOME_DIR="${OPENCLAW_HOME_DIR:-/tmp/openclaw-home-${RUN_ID}}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"
OPENCLAW_AGENT_DIR="${OPENCLAW_AGENT_DIR:-${OPENCLAW_STATE_DIR}/agents/locomo-eval}"
OPENCLAW_MAIN_AGENT_DIR="${OPENCLAW_MAIN_AGENT_DIR:-${OPENCLAW_STATE_DIR}/agents/main/agent}"
OPENCLAW_ENV="${OPENCLAW_ENV:-${OPENCLAW_STATE_DIR}/openviking.env}"
OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-$(python3 -c 'import sys; s=sum(ord(c) for c in sys.argv[1]); print(28000 + (s % 1000))' "${RUN_ID}")}"
OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-${RUN_ID}}"
OPENVIKING_PORT="${OPENVIKING_PORT:-$(python3 -c 'import sys; s=sum(ord(c) for c in sys.argv[1]); print(21000 + (s % 1000))' "${RUN_ID}")}"
OV_CONF_PATH="${OV_CONF_PATH:-${OPENVIKING_INSTANCE_DIR}/ov.conf}"
OV_DATA_DIR="${OV_DATA_DIR:-${OPENVIKING_INSTANCE_DIR}/data}"
OV_ACCOUNT_ID="${OV_ACCOUNT_ID:-acct-${RUN_ID}}"
OV_USER_ID="${OV_USER_ID:-user-${RUN_ID}}"
EXPECTED_OPENVIKING_VERSION="${MEMORY_BENCH_EXPECTED_OPENVIKING_VERSION:-}"
EXPECTED_OPENCLAW_VERSION="${MEMORY_BENCH_EXPECTED_OPENCLAW_VERSION:-}"
EXPECTED_LOCOMO_BENCHMARK_VERSION="${MEMORY_BENCH_EXPECTED_LOCOMO_BENCHMARK_VERSION:-}"
OPENVIKING_INTROSPECT_PYTHON_BIN="${OPENVIKING_INTROSPECT_PYTHON_BIN:-}"

remote_container_port_is_free() {
  local port="$1"
  ssh -p "${SSH_PORT}" "${SSH_HOST}" \
    "docker exec ${REMOTE_CONTAINER} python3 -c 'import socket, sys; port=int(sys.argv[1]); s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((\"127.0.0.1\", port)); s.close()' '${port}'" >/dev/null 2>&1
}

resolve_remote_free_port() {
  local candidate="$1"
  local max_port="$2"
  while [ "${candidate}" -le "${max_port}" ]; do
    if remote_container_port_is_free "${candidate}"; then
      echo "${candidate}"
      return 0
    fi
    candidate="$((candidate + 1))"
  done
  echo "No free remote port found in range ending at ${max_port}" >&2
  exit 13
}

acquire_remote_runtime_lock() {
  ssh -p "${SSH_PORT}" "${SSH_HOST}" "bash -lc 'mkdir -p \"${REMOTE_LOCK_DIR}\" && if [ -f \"${REMOTE_RUNTIME_LOCK_FILE}\" ]; then pid=\$(cat \"${REMOTE_RUNTIME_LOCK_FILE}\" 2>/dev/null || true); if [ -n \"\${pid}\" ] && kill -0 \"\${pid}\" 2>/dev/null; then cmd=\$(ps -p \"\${pid}\" -o args= 2>/dev/null || true); if printf %s \"\${cmd}\" | grep -F \"run_official_locomo_small.sh\" >/dev/null 2>&1; then echo LOCKED:${REMOTE_RUNTIME_LOCK_FILE}; exit 2; fi; fi; rm -f \"${REMOTE_RUNTIME_LOCK_FILE}\"; fi; echo $$ > \"${REMOTE_RUNTIME_LOCK_FILE}\"'"
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

if [ "${PLATFORM_IMPORT_ENABLED}" = "true" ] && [ -f "${LOCAL_OUTPUT_DIR}/qa_results.csv" ]; then
  PLATFORM_RUN_DIR="$(
    python3 "${WORKSPACE_ROOT}/tools/test_entrypoints/import_official_locomo_run.py" \
      --run-id "${RUN_ID}" \
      --entrypoint-id "official_${MODE}_sample${SAMPLE}" \
      --benchmark-id "locomo" \
      --agent-id "openclaw" \
      --output-dir "${LOCAL_OUTPUT_DIR}" \
      --platform-runs-root "${PLATFORM_RUNS_ROOT}"
  )"

  (
    cd "${WORKSPACE_ROOT}/memory_bench_platform"
    PYTHONPATH=. python3 -m memory_bench_platform.cli analyze-run --run-dir "${PLATFORM_RUN_DIR}"
  )

  mkdir -p "${LOCAL_OUTPUT_DIR}/reports"
  for report_name in summary.json case_results.json external_result_summary.json analysis.json analysis.md timing_report.json timing_report.html; do
    if [ -f "${PLATFORM_RUN_DIR}/reports/${report_name}" ]; then
      cp "${PLATFORM_RUN_DIR}/reports/${report_name}" "${LOCAL_OUTPUT_DIR}/reports/${report_name}"
    fi
  done

  echo "platform_run_dir=${PLATFORM_RUN_DIR}"
  echo "timing_report_html=${LOCAL_OUTPUT_DIR}/reports/timing_report.html"
fi
python3 "${SCRIPT_DIR}/prepare_remote_locomo_runtime.py" "${PREPARE_ARGS[@]}"

ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} python3 -c 'from pathlib import Path; p=Path(\"${REMOTE_BENCH_DIR}/run_clean_small_in_container.sh\"); t=p.read_text(encoding=\"utf-8\"); t=t.replace(\"isolate_user_scope_by_agent\\\":false\", \"isolate_user_scope_by_agent\\\":true\"); t=t.replace(\"isolate_agent_scope_by_user\\\":false\", \"isolate_agent_scope_by_user\\\":true\"); u=\"$\"+\"{ISOLATE_USER_SCOPE_BY_AGENT}\"; a=\"$\"+\"{ISOLATE_AGENT_SCOPE_BY_USER}\"; t=t.replace(f\"  if [[ \\\"{u}\\\" == \\\"false\\\" ]]; then\\n    args+=(--no-isolate-user-scope-by-agent)\\n  fi\\n\", \"\"); t=t.replace(f\"  if [[ \\\"{a}\\\" == \\\"false\\\" ]]; then\\n    args+=(--no-isolate-agent-scope-by-user)\\n  fi\\n\", \"\"); t=t.replace(f\"  if [[ \\\"{u}\\\" == \\\"false\\\" ]]; then\\n  fi\\n\", \"\"); t=t.replace(f\"  if [[ \\\"{a}\\\" == \\\"false\\\" ]]; then\\n  fi\\n\", \"\"); p.write_text(t, encoding=\"utf-8\")'"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} python3 -c 'from pathlib import Path; p=Path(\"${REMOTE_BENCH_DIR}/run_clean_small_in_container.sh\"); t=p.read_text(encoding=\"utf-8\"); t=t.replace(\"    echo \\\"failed to provision OpenViking user key for ${OV_ACCOUNT_ID}/${OV_USER_ID}\\\" >&2\\n    exit 14\\n\", \"    echo \\\"warning: failed to provision OpenViking user key for ${OV_ACCOUNT_ID}/${OV_USER_ID}; keeping root API key with explicit tenant headers\\\" >&2\\n\"); t=t.replace(\"cfg[\\\"apiKey\\\"] = user_key\", \"cfg[\\\"apiKey\\\"] = str(cfg.get(\\\"apiKey\\\") or user_key or \\\"\\\")\"); t=t.replace(\"cfg.pop(\\\"accountId\\\", None)\\n\", \"\"); t=t.replace(\"cfg.pop(\\\"userId\\\", None)\\n\", \"\"); t=t.replace(\"{\\\"plugin_api_key\\\": \\\"set\\\", \\\"accountId\\\": cfg.get(\\\"accountId\\\"), \\\"userId\\\": cfg.get(\\\"userId\\\")}\", \"{\\\"plugin_api_key\\\": \\\"preserved\\\", \\\"has_user_key\\\": bool(user_key), \\\"accountId\\\": cfg.get(\\\"accountId\\\"), \\\"userId\\\": cfg.get(\\\"userId\\\")}\"); p.write_text(t, encoding=\"utf-8\")'"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} python3 -" <<'PY'
from pathlib import Path

paths = [
    Path("/root/.openclaw/extensions/openviking/dist/client.js"),
    Path("/home/jcp/agent/code/OpenViking/examples/openclaw-plugin/dist/client.js"),
]

for path in paths:
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    if "openviking: request ${path}" not in text:
        old = """            if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
                headers.set("Content-Type", "application/json");
            }
            const response = await fetch(`${this.baseUrl}${path}`, {
"""
        new = """            if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
                headers.set("Content-Type", "application/json");
            }
            if (this.routingDebugLog) {
                this.routingDebugLog(`openviking: request ${path} ` +
                    JSON.stringify({
                        X_OpenViking_Agent: effectiveAgentId || null,
                        X_OpenViking_Account: tenantHeaders.accountId ?? null,
                        X_OpenViking_User: tenantHeaders.userId ?? null,
                        hasApiKey: Boolean(tenantHeaders.apiKey),
                    }));
            }
            const response = await fetch(`${this.baseUrl}${path}`, {
"""
        text = text.replace(old, new)
    if "openviking: request error ${path}" not in text:
        old = """            if (!response.ok || payload.status === "error") {
                const code = payload.error?.code ? ` [${payload.error.code}]` : "";
                const message = payload.error?.message ?? `HTTP ${response.status}`;
                throw new Error(`OpenViking request failed${code}: ${message}`);
            }
"""
        new = """            if (!response.ok || payload.status === "error") {
                const code = payload.error?.code ? ` [${payload.error.code}]` : "";
                const message = payload.error?.message ?? `HTTP ${response.status}`;
                this.routingDebugLog?.(`openviking: request error ${path} ` +
                    JSON.stringify({
                        X_OpenViking_Agent: effectiveAgentId || null,
                        X_OpenViking_Account: tenantHeaders.accountId ?? null,
                        X_OpenViking_User: tenantHeaders.userId ?? null,
                        hasApiKey: Boolean(tenantHeaders.apiKey),
                        status: response.status,
                        code: payload.error?.code ?? null,
                        message,
                    }));
                throw new Error(`OpenViking request failed${code}: ${message}`);
            }
"""
        text = text.replace(old, new)
    if 'const configuredUserId = this.userId.trim();' not in text:
        old = """        const fallback = { userId: "default", agentId: effectiveAgentId };
        try {
            const status = await this.request("/api/v1/system/status", {}, agentId);
"""
        new = """        const configuredUserId = this.userId.trim();
        if (configuredUserId) {
            const identity = { userId: configuredUserId, agentId: effectiveAgentId };
            this.identityCache.set(effectiveAgentId, identity);
            return identity;
        }
        const fallback = { userId: "default", agentId: effectiveAgentId };
        try {
            const status = await this.request("/api/v1/system/status", {}, agentId);
"""
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
PY
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} python3 - '${REMOTE_BENCH_DIR}/run_clean_small_in_container.sh'" <<'PY'
from pathlib import Path
import re
import sys
import textwrap

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

sync_block = textwrap.dedent(
    """
    sync_openclaw_plugin_config() {
      python3 - "${OPENCLAW_CONFIG_PATH}" "${OV_USER_ID}" "${OV_ACCOUNT_ID}" "${ISOLATE_USER_SCOPE_BY_AGENT}" "${ISOLATE_AGENT_SCOPE_BY_USER}" "${OPENVIKING_PORT}" <<'PY'
    import json
    import sys
    from pathlib import Path

    config_path = Path(sys.argv[1])
    user_id = sys.argv[2]
    account_id = sys.argv[3]
    isolate_user_scope_by_agent = sys.argv[4].lower() == "true"
    isolate_agent_scope_by_user = sys.argv[5].lower() == "true"
    openviking_port = int(sys.argv[6])

    data = json.loads(config_path.read_text(encoding="utf-8"))
    plugins = data.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    openviking = entries.setdefault("openviking", {})
    cfg = openviking.setdefault("config", {})

    cfg["mode"] = "remote"
    cfg["baseUrl"] = f"http://127.0.0.1:{openviking_port}"
    cfg["userId"] = user_id
    cfg["accountId"] = account_id
    cfg["isolateUserScopeByAgent"] = isolate_user_scope_by_agent
    cfg["isolateAgentScopeByUser"] = isolate_agent_scope_by_user
    cfg["emitStandardDiagnostics"] = True
    cfg["logFindRequests"] = True
    cfg["agent_prefix"] = account_id

    slots = plugins.setdefault("slots", {})
    slots["contextEngine"] = "openviking"

    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    print(json.dumps(cfg, ensure_ascii=False))
    PY
    }
    """
).strip("\n") + "\n\n"
text = re.sub(
    r"(?s)sync_openclaw_plugin_config\(\) \{.*?\n\}\n\nsync_openclaw_auth_profiles\(\) \{",
    sync_block + "sync_openclaw_auth_profiles() {",
    text,
    count=1,
)

start_services_block = (
    'start_services() {\n'
    '  cd "${REPO_ROOT}"\n'
    '\n'
    '  (cd /tmp && nohup "${OPENVIKING_PYTHON_BIN}" -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1 >"${OV_LOG}" 2>&1 &)\n'
    '  for _ in $(seq 1 30); do\n'
    '    if curl -fsS "http://127.0.0.1:${OPENVIKING_PORT}/health" >/tmp/"${RUN_ID}"_ov_health.json 2>/dev/null; then\n'
    '      echo "[$(date -Is)] ov health ok"\n'
    '      cat /tmp/"${RUN_ID}"_ov_health.json\n'
    '      break\n'
    '    fi\n'
    '    sleep 1\n'
    '  done\n'
    '\n'
    '  PLUGIN_USER_KEY=$(python3 - "${OPENVIKING_PORT}" "${OPENVIKING_ROOT_API_KEY}" "${OV_ACCOUNT_ID}" "${OV_USER_ID}" "${RUN_ID}" <<\'__OVTEST_USERKEY_PY__\'\n'
    'import json\n'
    'import sys\n'
    'import time\n'
    'from pathlib import Path\n'
    'from urllib import request, error\n'
    '\n'
    'port = int(sys.argv[1])\n'
    'root_key = sys.argv[2]\n'
    'account_id = sys.argv[3]\n'
    'user_id = sys.argv[4]\n'
    'run_id = sys.argv[5]\n'
    'base = f"http://127.0.0.1:{port}"\n'
    'headers = {"Content-Type": "application/json", "X-API-Key": root_key}\n'
    '\n'
    'def post(path: str, payload: dict) -> dict:\n'
    '    req = request.Request(base + path, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")\n'
    '    try:\n'
    '        with request.urlopen(req, timeout=20) as resp:\n'
    '            return json.loads(resp.read().decode("utf-8") or "{}")\n'
    '    except error.HTTPError as exc:\n'
    '        body = exc.read().decode("utf-8", errors="ignore")\n'
    '        try:\n'
    '            return json.loads(body or "{}")\n'
    '        except Exception:\n'
    '            return {"status": "error", "error": {"message": body or str(exc)}}\n'
    '    except Exception as exc:\n'
    '        return {"status": "error", "error": {"message": str(exc)}}\n'
    '\n'
    'last = {}\n'
    'for _ in range(30):\n'
    '    post("/api/v1/admin/accounts", {\n'
    '        "account_id": account_id,\n'
    '        "admin_user_id": f"{user_id}-admin",\n'
    '        "isolate_user_scope_by_agent": True,\n'
    '        "isolate_agent_scope_by_user": True,\n'
    '    })\n'
    '    last = post(f"/api/v1/admin/accounts/{account_id}/users", {"user_id": user_id, "role": "user"})\n'
    '    user_key = ((last.get("result") or {}).get("user_key")) or ""\n'
    '    if user_key:\n'
    '        Path(f"/tmp/{run_id}_user_create_resp.json").write_text(json.dumps(last, ensure_ascii=False), encoding="utf-8")\n'
    '        print(user_key)\n'
    '        raise SystemExit(0)\n'
    '    time.sleep(2)\n'
    'Path(f"/tmp/{run_id}_user_create_resp.json").write_text(json.dumps(last, ensure_ascii=False), encoding="utf-8")\n'
    '__OVTEST_USERKEY_PY__\n'
    '  )\n'
    '  if [ -z "${PLUGIN_USER_KEY}" ]; then\n'
    '    echo "warning: failed to provision OpenViking user key for ${OV_ACCOUNT_ID}/${OV_USER_ID}; keeping root API key with explicit tenant headers" >&2\n'
    '  fi\n'
    '  python3 -c \'import json,sys; from pathlib import Path; config_path=Path(sys.argv[1]); user_key=sys.argv[2]; data=json.loads(config_path.read_text(encoding="utf-8")); cfg=data.setdefault("plugins", {}).setdefault("entries", {}).setdefault("openviking", {}).setdefault("config", {}); cfg["apiKey"]=str(user_key or cfg.get("apiKey") or ""); config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8"); print(json.dumps({"plugin_api_key": "set_user_key" if user_key else "preserved", "has_user_key": bool(user_key), "accountId": cfg.get("accountId"), "userId": cfg.get("userId")}, ensure_ascii=False))\' "${OPENCLAW_CONFIG_PATH}" "${PLUGIN_USER_KEY}"\n'
    '  # shellcheck disable=SC1090\n'
    '  source "${OPENCLAW_ENV}"\n'
    '  nohup env HOME="${OPENCLAW_HOME_DIR}" OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" OPENVIKING_API_KEY="${OPENVIKING_API_KEY}" OPENVIKING_ACCOUNT_ID="${OV_ACCOUNT_ID}" OPENVIKING_USER_ID="${OV_USER_ID}" OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT="true" OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER="true" openclaw gateway >"${GW_LOG}" 2>&1 &\n'
    '  for _ in $(seq 1 30); do\n'
    '    if curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/health" >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then\n'
    '      echo "[$(date -Is)] gateway health ok"\n'
    '      cat /tmp/"${RUN_ID}"_gw_health.json\n'
    '      break\n'
    '    fi\n'
    '    sleep 1\n'
    '  done\n'
    '}\n\n'
)
text = re.sub(
    r"(?s)start_services\(\) \{.*?\n\}\n\nrun_phase_a\(\) \{",
    start_services_block + "run_phase_a() {",
    text,
    count=1,
)

text = re.sub(
    r"""(?ms)^\s*python3 - "\$\{OPENCLAW_CONFIG_PATH\}" "\$\{PLUGIN_USER_KEY\}" <<'__OVTEST_PLUGIN_USERKEY__'\n.*?^__OVTEST_PLUGIN_USERKEY__\n""",
    """      python3 -c 'import json,sys; from pathlib import Path; config_path=Path(sys.argv[1]); user_key=sys.argv[2]; data=json.loads(config_path.read_text(encoding="utf-8")); cfg=data.setdefault("plugins", {}).setdefault("entries", {}).setdefault("openviking", {}).setdefault("config", {}); cfg["apiKey"]=str(user_key or cfg.get("apiKey") or ""); config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8"); print(json.dumps({"plugin_api_key": "set_user_key" if user_key else "preserved", "has_user_key": bool(user_key), "accountId": cfg.get("accountId"), "userId": cfg.get("userId")}, ensure_ascii=False))' "${OPENCLAW_CONFIG_PATH}" "${PLUGIN_USER_KEY}"
""",
    text,
    count=1,
)

if "set_openclaw_gateway_port()" not in text:
    text = text.replace(
        "\nsync_openclaw_auth_profiles() {",
        "\nset_openclaw_gateway_port() {\n"
        "  python3 - \"${OPENCLAW_CONFIG_PATH}\" \"${OPENCLAW_GATEWAY_PORT}\" <<'__OVTEST_GATEWAY_PORT_PY__'\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "config_path = Path(sys.argv[1])\n"
        "gateway_port = int(sys.argv[2])\n"
        "data = json.loads(config_path.read_text(encoding=\"utf-8\"))\n"
        "gateway = data.setdefault(\"gateway\", {})\n"
        "gateway[\"port\"] = gateway_port\n"
        "control_ui = gateway.setdefault(\"controlUi\", {})\n"
        "control_ui[\"allowedOrigins\"] = [\n"
        "    f\"http://localhost:{gateway_port}\",\n"
        "    f\"http://127.0.0.1:{gateway_port}\",\n"
        "]\n"
        "config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding=\"utf-8\")\n"
        "print(json.dumps({\"gateway_port\": gateway_port}, ensure_ascii=False))\n"
        "__OVTEST_GATEWAY_PORT_PY__\n"
        "}\n\nsync_openclaw_auth_profiles() {",
    )

path.write_text(text, encoding="utf-8")
PY
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} python3 - '${REMOTE_BENCH_DIR}/run_clean_small_in_container.sh'" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = re.sub(
    r"""(?ms)^\s*python3 - "\$\{OPENCLAW_CONFIG_PATH\}" "\$\{PLUGIN_USER_KEY\}".*?^\s*# shellcheck disable=SC1090\n""",
    """      python3 -c 'import json,sys; from pathlib import Path; config_path=Path(sys.argv[1]); user_key=sys.argv[2]; data=json.loads(config_path.read_text(encoding="utf-8")); cfg=data.setdefault("plugins", {}).setdefault("entries", {}).setdefault("openviking", {}).setdefault("config", {}); cfg["apiKey"]=str(user_key or cfg.get("apiKey") or ""); config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8"); print(json.dumps({"plugin_api_key": "set_user_key" if user_key else "preserved", "has_user_key": bool(user_key), "accountId": cfg.get("accountId"), "userId": cfg.get("userId")}, ensure_ascii=False))' "${OPENCLAW_CONFIG_PATH}" "${PLUGIN_USER_KEY}"
      # shellcheck disable=SC1090
""",
    text,
    count=1,
)
path.write_text(text, encoding="utf-8")
PY

ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} python3 - '${REMOTE_BENCH_DIR}/phase_a_off.py'" <<'PY'
from pathlib import Path
import re
import sys
import textwrap

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

start = text.find("\ndef reindex_memory_root(")
marker = "\ndef wait_for_search_visibility("
end = text.find(marker, start + 1) if start != -1 else -1
if start != -1 and end != -1:
    replacement = textwrap.dedent(
        """

        def reindex_memory_root(
            *,
            base_url: str,
            api_key: str,
            account_id: str,
            user_id: str,
            agent_id: str | None = None,
            account_root: Path | None = None,
            timeout: float = 120.0,
            retry_interval: float = 2.0,
        ) -> dict[str, Any]:
            target_uri = f"viking://user/{user_id}/memories"
            if account_root is not None:
                memories_root = resolve_memories_root(
                    account_root=account_root,
                    user_id=user_id,
                    agent_id=agent_id,
                )
                if memories_root is not None:
                    target_uri = build_probe_target_uri(
                        user_id=user_id,
                        agent_id=agent_id,
                        memories_root=memories_root,
                        account_root=account_root,
                    )
            headers = {
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "X-OpenViking-Account": account_id,
                "X-OpenViking-User": user_id,
            }
            payload = {
                "uri": target_uri,
                "mode": "vectors_only",
                "wait": True,
            }
            deadline = time.monotonic() + max(timeout, 1.0)
            attempts = 0
            last_error = ""
            while time.monotonic() < deadline:
                attempts += 1
                try:
                    resp = requests.post(
                        base_url.rstrip("/") + "/api/v1/content/reindex",
                        headers=headers,
                        json=payload,
                        timeout=max(30.0, timeout),
                    )
                    data = resp.json() if resp.content else {}
                    if resp.ok:
                        return {
                            "ok": True,
                            "attempts": attempts,
                            "target_uri": target_uri,
                            "result": data.get("result", data),
                        }
                    last_error = data.get("error", {}).get("message") or resp.text or ("HTTP " + str(resp.status_code))
                    conflict_type = data.get("error", {}).get("details", {}).get("conflict_type")
                    if resp.status_code == 409 and conflict_type == "path_busy":
                        time.sleep(max(retry_interval, 0.1))
                        continue
                    resp.raise_for_status()
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(max(retry_interval, 0.1))
            return {
                "ok": False,
                "attempts": attempts,
                "target_uri": target_uri,
                "last_error": last_error,
            }
        """
    ).rstrip() + "\n"
    text = text[:start] + replacement + text[end:]

call_pattern = re.compile(
    r"(?P<indent>\s*)reindex_result = reindex_memory_root\(\n"
    r"(?P=indent)\s+base_url=args\.openviking_url,\n"
    r"(?P=indent)\s+api_key=args\.ov_api_key,\n"
    r'(?P=indent)\s+account_id=str\(args\.ov_account_id or ""\),\n'
    r"(?P=indent)\s+user_id=user,\n"
    r"(?P=indent)\)",
    re.MULTILINE,
)
text = call_pattern.sub(
    (
        "\\g<indent>reindex_result = reindex_memory_root(\n"
        "\\g<indent>    base_url=args.openviking_url,\n"
        "\\g<indent>    api_key=args.ov_api_key,\n"
        '\\g<indent>    account_id=str(args.ov_account_id or ""),\n'
        "\\g<indent>    user_id=user,\n"
        "\\g<indent>    agent_id=ov_agent_id,\n"
        "\\g<indent>    account_root=Path(DEFAULT_OV_DATA_ROOT) / str(args.ov_account_id or \"\"),\n"
        "\\g<indent>)"
    ),
    text,
)

path.write_text(text, encoding="utf-8")
PY

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
    local normalized_expected_openviking="${EXPECTED_OPENVIKING_VERSION#v}"
    candidates+=("/root/.openviking/venv-${normalized_expected_openviking}/bin/python")
    candidates+=("/root/.openviking/${normalized_expected_openviking}/bin/python")
  fi
  candidates+=(
    "/root/.openviking/venv-0.3.24/bin/python"
    "/root/.openviking/venv/bin/python"
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
OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-${OPENVIKING_INTROSPECT_PYTHON_BIN}}"

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
  actual_ov="$(printf '%s' "${actual_ov}" | sed -E 's/.*(v?[0-9]+(\.[0-9]+){2,3}).*/\1/; s/^v//')"
  actual_oc="$(printf '%s' "${actual_oc}" | sed -E 's/.*(v?[0-9]+(\.[0-9]+){2,3}).*/\1/; s/^v//')"
  expected_ov="$(printf '%s' "${expected_ov}" | sed -E 's/.*(v?[0-9]+(\.[0-9]+){2,3}).*/\1/; s/^v//')"
  expected_oc="$(printf '%s' "${expected_oc}" | sed -E 's/.*(v?[0-9]+(\.[0-9]+){2,3}).*/\1/; s/^v//')"
  actual_ov="${actual_ov#v}"
  actual_oc="${actual_oc#v}"
  expected_ov="${expected_ov#v}"
  expected_oc="${expected_oc#v}"

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

OPENCLAW_GATEWAY_PORT="$(resolve_remote_free_port "${OPENCLAW_GATEWAY_PORT}" 28999)"
OPENVIKING_PORT="$(resolve_remote_free_port "${OPENVIKING_PORT}" 21999)"

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
    import openviking
    ov_version = getattr(openviking, \"__version__\", \"unknown\")
except Exception as exc:
    ov_version = f\"error:{exc}\"
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
    \"openviking_package_version\": ov_version,
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
long_params = ((compat.get("extract_long_term_memories") or {}).get("params") or [])
agent_params = ((compat.get("extract_agent_memories") or {}).get("params") or [])
longterm_ok = bool(
    ((compat.get("extract_long_term_memories") or {}).get("accepts_latest_archive_overview"))
    and (
        ((compat.get("extract_long_term_memories") or {}).get("accepts_latest_archive_session_time"))
        or ("archive_uri" in long_params)
    )
)
agent_ok = bool(
    ((compat.get("extract_agent_memories") or {}).get("accepts_latest_archive_overview"))
    and (
        ((compat.get("extract_agent_memories") or {}).get("accepts_latest_archive_session_time"))
        or ("archive_uri" in agent_params)
    )
)
if not provider_ok or not longterm_ok or not agent_ok:
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
    payload.get("openviking_package_version")
    or health_body.get("version")
    or payload.get("openviking_git_describe")
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
  LOCK_PID=\$(cat "\$LOCK_FILE" 2>/dev/null || true)
  if [ -n "\${LOCK_PID}" ] && kill -0 "\${LOCK_PID}" 2>/dev/null; then
    LOCK_CMD=\$(ps -p "\${LOCK_PID}" -o args= 2>/dev/null || true)
    if printf '%s' "\${LOCK_CMD}" | grep -F "phase_a_off.py" >/dev/null 2>&1; then
      echo "LOCKED:\$LOCK_FILE" >&2
      exit 2
    fi
  fi
  rm -f "\$LOCK_FILE"
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
TOKEN_SOURCE="${OPENCLAW_CONFIG_PATH}"
if [ ! -f "\${TOKEN_SOURCE}" ]; then
  TOKEN_SOURCE="/root/.openclaw/openclaw.json"
fi
TOKEN=\$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["gateway"]["auth"]["token"])' "\${TOKEN_SOURCE}")

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
export LOCK_FILE="/tmp/locomo-openclaw-benchmark-${RUN_ID}.lock"
export MASTER_LOG="/tmp/${RUN_ID}.master.log"
export OV_LOG="/tmp/${RUN_ID}.ov.log"
export GW_LOG="/tmp/${RUN_ID}.gw.log"
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}"
export OPENCLAW_HOME_DIR="${OPENCLAW_HOME_DIR}"
export OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}"
export OPENCLAW_AGENT_DIR="${OPENCLAW_AGENT_DIR}"
export OPENCLAW_MAIN_AGENT_DIR="${OPENCLAW_MAIN_AGENT_DIR}"
export OPENCLAW_ENV="${OPENCLAW_ENV}"
export OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT}"
export OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR}"
export OPENVIKING_PORT="${OPENVIKING_PORT}"
export OV_CONF_PATH="${OV_CONF_PATH}"
export OV_DATA_DIR="${OV_DATA_DIR}"
export OPENVIKING_DATA_ROOT="${OV_DATA_DIR}/viking"
export OV_ACCOUNT_ID="${OV_ACCOUNT_ID}"
export OV_USER_ID="${OV_USER_ID}"
export OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN}"
export ISOLATE_USER_SCOPE_BY_AGENT="true"
export ISOLATE_AGENT_SCOPE_BY_USER="true"
export OPENVIKING_API_KEY="${ROOT_KEY}"
export OPENVIKING_ACCOUNT_ID="${OV_ACCOUNT_ID}"
export OPENVIKING_USER_ID="${OV_USER_ID}"
export OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT="true"
export OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER="true"

python3 - "${OPENCLAW_STATE_DIR}" "${OPENCLAW_HOME_DIR}" "${OPENCLAW_CONFIG_PATH}" "${OPENCLAW_GATEWAY_PORT}" "${LOCOMO_EVAL_MODEL}" "${OPENCLAW_ENV}" "${OPENCLAW_MAIN_AGENT_DIR}" "${OV_CONF_PATH}" "${OV_DATA_DIR}" "${OPENVIKING_PORT}" <<'__OVTEST_BOOTSTRAP_PY__'
import json
import shutil
import sys
from pathlib import Path

state_dir = Path(sys.argv[1])
home_dir = Path(sys.argv[2])
config_path = Path(sys.argv[3])
gateway_port = int(sys.argv[4])
locomo_model = sys.argv[5].strip()
env_path = Path(sys.argv[6])
main_agent_dir = Path(sys.argv[7])
ov_conf_path = Path(sys.argv[8])
ov_data_dir = Path(sys.argv[9])
ov_port = int(sys.argv[10])

base_state_dir = Path("/root/.openclaw")
base_ov_conf = Path("/root/.openviking/ov.conf")

if home_dir.exists() or home_dir.is_symlink():
    if home_dir.is_symlink() or home_dir.is_file():
        home_dir.unlink()
    else:
        shutil.rmtree(home_dir)
home_dir.mkdir(parents=True, exist_ok=True)
state_dir.mkdir(parents=True, exist_ok=True)
ov_data_dir.mkdir(parents=True, exist_ok=True)

link_path = home_dir / ".openclaw"
if link_path.exists() or link_path.is_symlink():
    if link_path.is_symlink() or link_path.is_file():
        link_path.unlink()
    else:
        shutil.rmtree(link_path)
link_path.symlink_to(state_dir, target_is_directory=True)

base_config = json.loads((base_state_dir / "openclaw.json").read_text(encoding="utf-8"))
base_config.pop("stateDir", None)
gateway = base_config.setdefault("gateway", {})
gateway["port"] = gateway_port
control_ui = gateway.setdefault("controlUi", {})
control_ui["allowedOrigins"] = [
    f"http://localhost:{gateway_port}",
    f"http://127.0.0.1:{gateway_port}",
]
if locomo_model:
    base_config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = locomo_model
    for agent in base_config.get("agents", {}).get("list", []):
        if isinstance(agent, dict) and agent.get("id") == "locomo-eval":
            agent["model"] = locomo_model
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(json.dumps(base_config, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")

for rel in [
    ("agents/main/agent/auth-profiles.json", main_agent_dir / "auth-profiles.json"),
    ("agents/main/agent/auth-state.json", main_agent_dir / "auth-state.json"),
    ("openviking.env", env_path),
]:
    src = base_state_dir / rel[0]
    dst = rel[1]
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

extensions_src = base_state_dir / "extensions" / "openviking"
extensions_dst = state_dir / "extensions" / "openviking"
if extensions_src.exists():
    if extensions_dst.exists():
        shutil.rmtree(extensions_dst)
    extensions_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(extensions_src, extensions_dst)

ov_conf = json.loads(base_ov_conf.read_text(encoding="utf-8"))
ov_conf.setdefault("server", {})["port"] = ov_port
ov_conf.setdefault("storage", {})["workspace"] = str(ov_data_dir)
ov_conf_path.parent.mkdir(parents=True, exist_ok=True)
ov_conf_path.write_text(json.dumps(ov_conf, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
__OVTEST_BOOTSTRAP_PY__

python3 - <<'__OVTEST_PATCH_PY__'
from pathlib import Path
import re

script_path = Path("run_clean_small_in_container.sh")
text = script_path.read_text(encoding="utf-8")
placeholder = "__DOLLAR__"
pattern = re.compile(
    re.escape("set_wm_mode() {\n")
    + r".*?\n"
    + re.escape("PY\n}"),
    re.S,
)
new = """set_wm_mode() {
  "__DOLLAR__{OPENVIKING_PYTHON_BIN}" - "__DOLLAR__{OV_CONF_PATH}" "__DOLLAR__{MODE}" <<'PY'
import json
import sys
from pathlib import Path

try:
    import openviking
    ov_version = getattr(openviking, "__version__", "")
except Exception:
    ov_version = ""

conf_path = Path(sys.argv[1])
mode = sys.argv[2]
data = json.loads(conf_path.read_text(encoding="utf-8"))
memory = data.setdefault("memory", {})
if ov_version.startswith("0.3.24"):
    memory.pop("wm_v2_preprocess_enabled", None)
    result = {"wm_v2_preprocess_enabled": None, "skipped_for_version": ov_version}
else:
    memory["wm_v2_preprocess_enabled"] = (mode == "on")
    result = dict(memory)
conf_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
PY
}
"""
new = new.replace(placeholder, "$")
text, count = pattern.subn(new.rstrip("\n"), text, count=1)
if "__OVTEST_PLUGIN_USERKEY__" not in text:
    text = text.replace(
        '  # shellcheck disable=SC1090\n'
        '  source "${OPENCLAW_ENV}"\n',
        '  curl -sS -X POST "http://127.0.0.1:${OPENVIKING_PORT}/api/v1/admin/accounts" \\\n'
        '    -H "Content-Type: application/json" \\\n'
        '    -H "X-API-Key: \${OPENVIKING_ROOT_API_KEY}" \\\n'
        '    -d "{\\"account_id\\":\\"\${OV_ACCOUNT_ID}\\",\\"admin_user_id\\":\\"\${OV_USER_ID}\\",\\"isolate_user_scope_by_agent\\":true,\\"isolate_agent_scope_by_user\\":true}" \\\n'
        '    >/tmp/"\${RUN_ID}"_ensure_account.json 2>/dev/null || true\n'
        '  PLUGIN_USER_KEY=\$(\\\n'
        '    curl -sS -X POST "http://127.0.0.1:\${OPENVIKING_PORT}/api/v1/admin/accounts/\${OV_ACCOUNT_ID}/users" \\\n'
        '      -H "Content-Type: application/json" \\\n'
        '      -H "X-API-Key: \${OPENVIKING_ROOT_API_KEY}" \\\n'
        '      -d "{\\"user_id\\":\\"\${OV_USER_ID}\\",\\"role\\":\\"user\\"}" \\\n'
        '    | python3 -c \'import json,sys; data=json.load(sys.stdin); print(((data.get("result") or {}).get("user_key")) or "")\'\\\n'
        '  )\n'
        '  if [ -z "\${PLUGIN_USER_KEY}" ]; then\n'
        '    echo "warning: failed to provision OpenViking user key for \${OV_ACCOUNT_ID}/\${OV_USER_ID}; keeping root API key with explicit tenant headers" >&2\n'
        '  fi\n'
        '  python3 - "\${OPENCLAW_CONFIG_PATH}" "\${PLUGIN_USER_KEY}" <<\'__OVTEST_PLUGIN_USERKEY__\'\n'
        'import json\n'
        'import sys\n'
        'from pathlib import Path\n'
        '\n'
        'config_path = Path(sys.argv[1])\n'
        'user_key = sys.argv[2]\n'
        'data = json.loads(config_path.read_text(encoding="utf-8"))\n'
        'cfg = data.setdefault("plugins", {}).setdefault("entries", {}).setdefault("openviking", {}).setdefault("config", {})\n'
        'cfg["apiKey"] = str(cfg.get("apiKey") or user_key or "")\n'
        'config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")\n'
        'print(json.dumps({"plugin_api_key": "preserved", "has_user_key": bool(user_key), "accountId": cfg.get("accountId"), "userId": cfg.get("userId")}, ensure_ascii=False))\n'
        '__OVTEST_PLUGIN_USERKEY__\n'
        '  # shellcheck disable=SC1090\n'
        '  source "${OPENCLAW_ENV}"\n',
    )
script_path.write_text(text, encoding="utf-8")
__OVTEST_PATCH_PY__

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
