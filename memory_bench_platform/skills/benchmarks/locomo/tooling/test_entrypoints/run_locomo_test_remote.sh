#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="${SSH_HOST:-jcp@123.60.114.206}"
SSH_PORT="${SSH_PORT:-10008}"
REMOTE_CONTAINER="${REMOTE_CONTAINER:-jcp-dev}"
REMOTE_ROOT="${REMOTE_ROOT:-/tmp/locomo_test}"
REMOTE_LOCK_DIR="${REMOTE_LOCK_DIR:-/tmp/locomo-entrypoint-locks}"
RUN_ID="${RUN_ID:-locomo_test_remote_$(date +%Y%m%d_%H%M%S)}"
OUTPUT_DIR="${OUTPUT_DIR:-}"
LOCAL_OUTPUT_ROOT="${LOCAL_OUTPUT_ROOT:-/tmp/locomo_test_output}"
if [ -n "${OUTPUT_DIR}" ]; then
  LOCAL_OUTPUT_DIR="${OUTPUT_DIR}"
  LOCAL_OUTPUT_ROOT="$(dirname "${OUTPUT_DIR}")"
else
  LOCAL_OUTPUT_DIR="${LOCAL_OUTPUT_ROOT}/${RUN_ID}"
fi
REMOTE_OUTPUT_DIR="/tmp/locomo_test_output/${RUN_ID}"
REMOTE_MONITOR_DIR="${REMOTE_OUTPUT_DIR}/monitor"
REMOTE_PID_FILE="${REMOTE_MONITOR_DIR}/target_pids.json"
LOCOMO_TEST_CONFIG="${LOCOMO_TEST_CONFIG:-openviking-small-stable.toml}"
OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-${RUN_ID}}"
OPENCLAW_HOME_DIR="${OPENCLAW_HOME_DIR:-/tmp/openclaw-home-${RUN_ID}}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"
OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-}"
OPENCLAW_ENV="${OPENCLAW_ENV:-${OPENCLAW_STATE_DIR}/openviking.env}"
OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-${RUN_ID}}"
OPENVIKING_PORT="${OPENVIKING_PORT:-}"
OV_CONF_PATH="${OV_CONF_PATH:-${OPENVIKING_INSTANCE_DIR}/ov.conf}"
OV_DATA_DIR="${OV_DATA_DIR:-${OPENVIKING_INSTANCE_DIR}/data}"
OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-/root/.openviking/venv-0.3.24/bin/python}"
OPENVIKING_VLM_TIMEOUT_SECONDS="${OPENVIKING_VLM_TIMEOUT_SECONDS:-300}"
OV_LOG="${OV_LOG:-/tmp/${RUN_ID}_openviking.log}"
GW_LOG="${GW_LOG:-/tmp/${RUN_ID}_openclaw_gateway.log}"
PLUGIN_USER_KEY_FILE="/tmp/${RUN_ID}_plugin_user_key.txt"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

pick_remote_free_port() {
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} python3 - <<'PY'
import socket

sock = socket.socket()
sock.bind(('127.0.0.1', 0))
print(sock.getsockname()[1])
sock.close()
PY"
}

if [ -z "${OPENVIKING_PORT}" ]; then
  OPENVIKING_PORT="$(pick_remote_free_port)"
fi
if [ -z "${OPENCLAW_GATEWAY_PORT}" ]; then
  OPENCLAW_GATEWAY_PORT="$(pick_remote_free_port)"
fi

TMP_TAR="$(mktemp)"
trap 'rm -f "${TMP_TAR}"' EXIT

python3 "${SCRIPT_DIR}/build_remote_runtime_bundle.py" --output "${TMP_TAR}"
cat "${TMP_TAR}" | ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} bash -lc 'rm -rf ${REMOTE_ROOT} && mkdir -p /tmp && tar xzf - -C /tmp'"

ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec -i ${REMOTE_CONTAINER} bash -s" <<INNER
set -euo pipefail

LOCK_DIR="${REMOTE_LOCK_DIR}"
mkdir -p "\$LOCK_DIR"
LOCK_FILE="\$LOCK_DIR/locomo_test_remote.lock"
if [ -f "\$LOCK_FILE" ]; then
  existing_pid="$(cat "\$LOCK_FILE" 2>/dev/null || true)"
  if [ -n "\$existing_pid" ] && ps -p "\$existing_pid" >/dev/null 2>&1; then
    echo "LOCKED:\$LOCK_FILE" >&2
    exit 2
  fi
  rm -f "\$LOCK_FILE"
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
printf '{}\n' > "${REMOTE_PID_FILE}"

(
python3 - "${REMOTE_MONITOR_DIR}" "${REMOTE_PID_FILE}" <<'PY'
from __future__ import annotations

import csv
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

out_dir = Path(sys.argv[1])
pid_file = Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
cpu_path = out_dir / "cpu_status.csv"
mem_path = out_dir / "mem_status.csv"
disk_path = out_dir / "disk_status.csv"
net_path = out_dir / "net_status.csv"
with cpu_path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerow(["timestamp", "summary_util_user", "summary_util_sys", "summary_util_idle"])
with mem_path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerow(["timestamp", "mem_free_mb", "mem_used_mb"])
with disk_path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerow(["timestamp", "read_bw_mb", "write_bw_mb", "disk_bw_mb", "disk_free_mb"])
with net_path.open("w", encoding="utf-8", newline="") as handle:
    csv.writer(handle).writerow(["timestamp", "recv_pcks_rate", "sent_pcks_rate", "recv_bytes_rate", "sent_bytes_rate"])

running = True
last_cpu = None
last_io = None
last_net = None
last_ts = None
clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
cpu_count = os.cpu_count() or 1

def _stop(signum, frame):
    del signum, frame
    global running
    running = False

signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

def _load_target_roots() -> list[int]:
    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    roots = []
    if isinstance(payload, dict):
        for value in payload.values():
            try:
                pid = int(value)
            except Exception:
                continue
            if pid > 0 and Path(f"/proc/{pid}").exists():
                roots.append(pid)
    return roots

def _proc_stat(pid: int) -> tuple[int, int] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except Exception:
        return None
    end = text.rfind(")")
    if end < 0:
        return None
    rest = text[end + 2 :].split()
    if len(rest) < 13:
        return None
    try:
        ppid = int(rest[1])
        utime = int(rest[11])
        stime = int(rest[12])
    except Exception:
        return None
    return ppid, utime + stime

def _resolve_tree_pids(roots: Iterable[int]) -> list[int]:
    root_set = {int(pid) for pid in roots if int(pid) > 0}
    if not root_set:
        return []
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        stat = _proc_stat(int(entry.name))
        if stat is None:
            continue
        ppid, _ = stat
        children.setdefault(ppid, []).append(int(entry.name))
    resolved = set(root_set)
    stack = list(root_set)
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in resolved:
                resolved.add(child)
                stack.append(child)
    return sorted(resolved)

def _read_process_cpu_ticks(pids: Iterable[int]) -> tuple[int, int]:
    user_ticks = 0
    sys_ticks = 0
    for pid in pids:
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except Exception:
            continue
        end = text.rfind(")")
        if end < 0:
            continue
        rest = text[end + 2 :].split()
        if len(rest) < 13:
            continue
        try:
            user_ticks += int(rest[11])
            sys_ticks += int(rest[12])
        except Exception:
            continue
    return user_ticks, sys_ticks

def _read_process_rss_mb(pids: Iterable[int]) -> tuple[float, float]:
    used_mb = 0.0
    for pid in pids:
        try:
            for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    used_mb += float(line.split(":", 1)[1].strip().split()[0]) / 1024.0
                    break
        except Exception:
            continue
    return 0.0, round(used_mb, 2)

def _read_process_io_bytes(pids: Iterable[int]) -> tuple[int, int]:
    read_bytes = 0
    write_bytes = 0
    for pid in pids:
        try:
            rows = Path(f"/proc/{pid}/io").read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for row in rows:
            if row.startswith("read_bytes:"):
                read_bytes += int(row.split(":", 1)[1].strip())
            elif row.startswith("write_bytes:"):
                write_bytes += int(row.split(":", 1)[1].strip())
    return read_bytes, write_bytes

def _read_net():
    for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
        if ":" not in line:
            continue
        iface, payload = line.split(":", 1)
        if iface.strip() != "lo":
            continue
        parts = payload.split()
        if len(parts) < 16:
            break
        return float(parts[1]), float(parts[9]), float(parts[0]), float(parts[8])
    return 0.0, 0.0, 0.0, 0.0

while running:
    roots = _load_target_roots()
    pids = _resolve_tree_pids(roots)
    ts = datetime.now().isoformat()
    now = time.monotonic()
    user_ticks, sys_ticks = _read_process_cpu_ticks(pids)
    free_mb, used_mb = _read_process_rss_mb(pids)
    read_bytes, write_bytes = _read_process_io_bytes(pids)
    recv_pcks, sent_pcks, recv_bytes, sent_bytes = _read_net()

    user = 0.0
    system = 0.0
    idle = 100.0
    read_bw_mb = 0.0
    write_bw_mb = 0.0
    recv_pcks_rate = 0.0
    sent_pcks_rate = 0.0
    recv_bytes_rate = 0.0
    sent_bytes_rate = 0.0
    if last_ts is not None:
        elapsed = max(0.001, now - last_ts)
        if last_cpu is not None:
            delta_user = max(0, user_ticks - last_cpu[0])
            delta_sys = max(0, sys_ticks - last_cpu[1])
            total_cpu_pct = (delta_user + delta_sys) / (elapsed * clk_tck * cpu_count) * 100.0
            user = round(delta_user / (elapsed * clk_tck * cpu_count) * 100.0, 2)
            system = round(delta_sys / (elapsed * clk_tck * cpu_count) * 100.0, 2)
            idle = round(max(0.0, 100.0 - total_cpu_pct), 2)
        if last_io is not None:
            read_bw_mb = round(max(0, read_bytes - last_io[0]) / elapsed / (1024.0 * 1024.0), 4)
            write_bw_mb = round(max(0, write_bytes - last_io[1]) / elapsed / (1024.0 * 1024.0), 4)
        if last_net is not None:
            recv_pcks_rate = round(max(0.0, recv_pcks - last_net[0]) / elapsed, 4)
            sent_pcks_rate = round(max(0.0, sent_pcks - last_net[1]) / elapsed, 4)
            recv_bytes_rate = round(max(0.0, recv_bytes - last_net[2]) / elapsed, 4)
            sent_bytes_rate = round(max(0.0, sent_bytes - last_net[3]) / elapsed, 4)
    last_ts = now
    last_cpu = (user_ticks, sys_ticks)
    last_io = (read_bytes, write_bytes)
    last_net = (recv_pcks, sent_pcks, recv_bytes, sent_bytes)

    with cpu_path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([ts, user, system, idle])
    with mem_path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([ts, free_mb, used_mb])
    with disk_path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([ts, read_bw_mb, write_bw_mb, round(read_bw_mb + write_bw_mb, 4), 0.0])
    with net_path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([ts, recv_pcks_rate, sent_pcks_rate, recv_bytes_rate, sent_bytes_rate])
    time.sleep(1.0)
PY
) &
MONITOR_PID=\$!

write_pid_registry() {
  python3 - "${REMOTE_PID_FILE}" "\$1" "\$2" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = int(sys.argv[3])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    payload = {}
if not isinstance(payload, dict):
    payload = {}
payload[key] = value
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

python3 - "${REMOTE_ROOT}/configs/${LOCOMO_TEST_CONFIG}" "${RUN_ID}" "${OV_CONF_PATH}" "${OV_DATA_DIR}" "${OPENVIKING_PORT}" "${OPENVIKING_VLM_TIMEOUT_SECONDS}" <<'PY'
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
vlm_timeout_seconds = float(sys.argv[6])

runtime_cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
runtime_user = str(runtime_cfg.get("general", {}).get("user", "eval-1"))
payload = {
    "account_id": f"acct-{run_id}",
    "user_id": runtime_user,
}
base_ov_conf = Path("/root/.openviking/ov.conf")
ov_conf = json.loads(base_ov_conf.read_text(encoding="utf-8"))
env_toml_path = config_path.parent / "env.toml"
env_cfg = tomllib.loads(env_toml_path.read_text(encoding="utf-8")) if env_toml_path.exists() else {}
llm_cfg = env_cfg.get("llm", {}) if isinstance(env_cfg, dict) else {}
chat_cfg = llm_cfg.get("chat", {}) if isinstance(llm_cfg, dict) else {}
embedding_cfg = llm_cfg.get("embedding", {}) if isinstance(llm_cfg, dict) else {}
ov_conf.setdefault("server", {})["port"] = ov_port
ov_conf.setdefault("storage", {})["workspace"] = str(ov_data_dir)
ov_conf.setdefault("memory", {}).pop("wm_v2_preprocess_enabled", None)
ov_conf["vlm"] = {
    "provider": "openai_compatible",
    "api_key": str(chat_cfg.get("api_key", "")),
    "model": str(chat_cfg.get("model", "gpt-5.4-mini")),
    "api_base": str(chat_cfg.get("base_url", "https://codex.jemmy.icu/v1")),
    "temperature": 0.1,
    "max_retries": 3,
    "timeout": vlm_timeout_seconds,
}
ov_conf["embedding"] = {
    "dense": {
        "backend": "openai",
        "provider": "openai",
        "api_key": str(embedding_cfg.get("api_key", "dummy")),
        "model": str(embedding_cfg.get("model", "Qwen/Qwen3-Embedding-0.6B")),
        "api_base": str(embedding_cfg.get("base_url", "http://127.0.0.1:18080/v1")),
        "dimension": int(embedding_cfg.get("dimension", 1024) or 1024),
        "input": "text",
    }
}
ov_conf_path.parent.mkdir(parents=True, exist_ok=True)
ov_data_dir.mkdir(parents=True, exist_ok=True)
ov_conf_path.write_text(json.dumps(ov_conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
PY

nohup "${OPENVIKING_PYTHON_BIN}" -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1 >"${OV_LOG}" 2>&1 &
OPENVIKING_PID=\$!
write_pid_registry openviking "\${OPENVIKING_PID}"
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

PYTHONPATH="/tmp/locomo_test:/tmp/memory_bench_platform" python3 -m locomo_test.bootstrap_remote_runtime \
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
PLUGIN_USER_KEY="$(
python3 - "${OPENVIKING_PORT}" "${RUN_ID}" <<'PY'
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request

port = int(sys.argv[1])
run_id = sys.argv[2]
root_key = os.environ.get("OPENVIKING_ROOT_API_KEY") or os.environ.get("OPENVIKING_API_KEY", "")
account_id = os.environ.get("OPENVIKING_ACCOUNT_ID", "")
user_id = os.environ.get("OPENVIKING_USER_ID", "")

if not (port and root_key and account_id and user_id):
    raise SystemExit(0)

base = f"http://127.0.0.1:{port}"
headers = {"Content-Type": "application/json", "X-API-Key": root_key}

def post(path: str, payload: dict) -> dict:
    req = request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(body or "{}")
        except Exception:
            return {"status": "error", "error": {"message": body or str(exc)}}
    except Exception as exc:
        return {"status": "error", "error": {"message": str(exc)}}

last = {}
for _ in range(15):
    post(
        "/api/v1/admin/accounts",
        {
            "account_id": account_id,
            "admin_user_id": f"{user_id}-admin",
            "isolate_user_scope_by_agent": True,
            "isolate_agent_scope_by_user": True,
        },
    )
    last = post(f"/api/v1/admin/accounts/{account_id}/users", {"user_id": user_id, "role": "user"})
    user_key = ((last.get("result") or {}).get("user_key")) or ""
    if user_key:
        Path(f"/tmp/{run_id}_user_create_resp.json").write_text(
            json.dumps(last, ensure_ascii=False),
            encoding="utf-8",
        )
        print(user_key)
        raise SystemExit(0)
    time.sleep(2)

Path(f"/tmp/{run_id}_user_create_resp.json").write_text(
    json.dumps(last, ensure_ascii=False),
    encoding="utf-8",
)
PY
)"
printf '%s' "\$PLUGIN_USER_KEY" > "${PLUGIN_USER_KEY_FILE}"
python3 - "${OPENCLAW_CONFIG_PATH}" "\$PLUGIN_USER_KEY" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
user_key = sys.argv[2]
data = json.loads(config_path.read_text(encoding="utf-8"))
cfg = data.setdefault("plugins", {}).setdefault("entries", {}).setdefault("openviking", {}).setdefault("config", {})
cfg["apiKey"] = str(user_key or cfg.get("apiKey") or "")
cfg.pop("agent_prefix", None)
cfg["isolateUserScopeByAgent"] = True
cfg["isolateAgentScopeByUser"] = True
cfg["emitStandardDiagnostics"] = True
cfg["logFindRequests"] = True
config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({
    "plugin_api_key": "set_user_key" if user_key else "preserved",
    "has_user_key": bool(user_key),
    "accountId": cfg.get("accountId"),
    "userId": cfg.get("userId"),
}, ensure_ascii=False))
PY
nohup env HOME="${OPENCLAW_HOME_DIR}" OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT="true" OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER="true" openclaw gateway >"${GW_LOG}" 2>&1 &
OPENCLAW_GATEWAY_PID=\$!
write_pid_registry openclaw_gateway "\${OPENCLAW_GATEWAY_PID}"
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

PYTHONPATH="/tmp/locomo_test:/tmp/memory_bench_platform" python3 -m locomo_test.cli run "configs/${LOCOMO_TEST_CONFIG%.toml}-runtime.toml" &
LOCOMO_RUNNER_PID=\$!
write_pid_registry locomo_runner "\${LOCOMO_RUNNER_PID}"
wait "\${LOCOMO_RUNNER_PID}"
INNER

mkdir -p "${LOCAL_OUTPUT_ROOT}"
ssh -p "${SSH_PORT}" "${SSH_HOST}" "docker exec ${REMOTE_CONTAINER} bash -lc 'tar czf - -C /tmp/locomo_test_output ${RUN_ID}'" \
  | tar xzf - -C "${LOCAL_OUTPUT_ROOT}"

if [ -d "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" ] && [ "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" != "${LOCAL_OUTPUT_DIR}" ]; then
  rm -rf "${LOCAL_OUTPUT_DIR}"
  mv "${LOCAL_OUTPUT_ROOT}/${RUN_ID}" "${LOCAL_OUTPUT_DIR}"
fi
