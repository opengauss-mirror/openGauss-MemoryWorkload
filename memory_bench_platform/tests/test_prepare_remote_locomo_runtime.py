import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "tools" / "test_entrypoints" / "prepare_remote_locomo_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_remote_locomo_runtime", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_inject_openclaw_provider_config_backfills_minimax_models_when_provider_exists():
    config = {
        "models": {
            "providers": {
                "minimax": {
                    "apiKey": "existing-key",
                    "baseUrl": "https://api.minimaxi.com/v1",
                    "api": "openai-completions",
                    "models": [],
                }
            }
        }
    }

    MODULE._inject_openclaw_provider_config(config, "minimax/MiniMax-M3", auth_profiles=None)

    provider = config["models"]["providers"]["minimax"]
    assert provider["apiKey"] == "existing-key"
    assert provider["models"]
    assert provider["models"][0]["id"] == "MiniMax-M3"


def test_enable_benchmark_plugin_diagnostics_is_idempotent():
    shell_text = (
        'cfg["userId"] = user_id\n'
        'cfg["accountId"] = account_id\n'
    )

    updated = MODULE._enable_benchmark_plugin_diagnostics(shell_text)
    updated_twice = MODULE._enable_benchmark_plugin_diagnostics(updated)

    assert 'cfg["emitStandardDiagnostics"] = True\n' in updated
    assert 'cfg["logFindRequests"] = True\n' in updated
    assert updated == updated_twice


def test_enable_dedicated_gateway_port_injects_port_override_and_base_url():
    shell_text = (
        'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\n'
        "sync_openclaw_auth_profiles() {\n"
        '    if curl -fsS http://127.0.0.1:18789/health >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then\n'
        '    --output-dir "${OUTPUT_DIR}"\n'
        "  sync_openclaw_auth_profiles\n"
    )

    updated = MODULE._enable_dedicated_gateway_port(shell_text)

    assert 'OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-28789}"' in updated
    assert "set_openclaw_gateway_port()" in updated
    assert '"http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/health"' in updated
    assert '--base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"' in updated
    assert "  set_openclaw_gateway_port\n  sync_openclaw_auth_profiles\n" in updated


def test_enable_dedicated_gateway_port_is_idempotent():
    shell_text = (
        'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\n'
        "sync_openclaw_auth_profiles() {\n"
        '    if curl -fsS http://127.0.0.1:18789/health >/tmp/"${RUN_ID}"_gw_health.json 2>/dev/null; then\n'
        '    --output-dir "${OUTPUT_DIR}"\n'
        "  sync_openclaw_auth_profiles\n"
    )

    updated = MODULE._enable_dedicated_gateway_port(shell_text)
    updated_twice = MODULE._enable_dedicated_gateway_port(updated)

    assert updated == updated_twice
    assert updated.count('--base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"') == 1
    assert updated.count("set_openclaw_gateway_port()") == 1
    assert updated.count("  set_openclaw_gateway_port\n  sync_openclaw_auth_profiles\n") == 1


def test_openclaw_openviking_client_diag_injects_request_error_and_identity_fastpath():
    source_text = (
        '  private resolveEffectiveAgentId(agentId?: string): string {\n'
        '    const explicit = agentId?.trim();\n'
        '    if (explicit) {\n'
        '      return explicit;\n'
        '    }\n'
        '    const prefix = this.defaultAgentId.trim();\n'
        '    return prefix ? `${prefix}_main` : "main";\n'
        '  }\n'
        '      if (!response.ok || payload.status === "error") {\n'
        '        const code = payload.error?.code ? ` [${payload.error.code}]` : "";\n'
        '        const message = payload.error?.message ?? `HTTP ${response.status}`;\n'
        '        throw new Error(`OpenViking request failed${code}: ${message}`);\n'
        '      }\n'
        '    const fallback: RuntimeIdentity = { userId: "default", agentId: effectiveAgentId };\n'
        '    try {\n'
        '      const status = await this.request<{ user?: unknown }>("/api/v1/system/status", {}, agentId);\n'
    )

    updated = MODULE._ensure_openclaw_openviking_client_diag(source_text)
    updated_twice = MODULE._ensure_openclaw_openviking_client_diag(updated)

    assert "openviking: request error ${path}" in updated
    assert "hasApiKey: Boolean(tenantHeaders.apiKey)" in updated
    assert "this.isolateUserScopeByAgent" in updated
    assert 'return `${accountId}_${explicit}`;' in updated
    assert 'const configuredUserId = this.userId.trim();' in updated
    assert 'const identity: RuntimeIdentity = { userId: configuredUserId, agentId: effectiveAgentId };' in updated
    assert updated == updated_twice


def test_enable_isolated_runtime_injects_state_dir_and_openviking_isolation():
    shell_text = (
        'RUN_ID="${RUN_ID:-${MODE}_small_$(date +%Y%m%d_%H%M%S)}"\n'
        'OV_CONF_PATH="${OV_CONF_PATH:-/root/.openviking/ov.conf}"\n'
        'OV_DATA_DIR="${OV_DATA_DIR:-/root/.openviking/data}"\n'
        'OPENCLAW_AGENT_DIR="${OPENCLAW_AGENT_DIR:-/root/.openclaw/agents/locomo-eval}"\n'
        'OPENCLAW_MAIN_AGENT_DIR="${OPENCLAW_MAIN_AGENT_DIR:-/root/.openclaw/agents/main/agent}"\n'
        'OPENCLAW_ENV="${OPENCLAW_ENV:-/root/.openclaw/openviking.env}"\n'
        'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\n'
        'mkdir -p "${OUTPUT_DIR}"\n\n'
        'backup_and_reset() {\n'
        '  pkill -f \'phase_a_off.py\' || true\n'
        '  pkill -f \'openclaw-gateway\' || true\n'
        '  pkill -f \'python3 -m openviking.server.bootstrap --host 127.0.0.1 --port 1933 --workers 1\' || true\n'
        '  sleep 2\n\n'
        '  rm -rf "${OV_DATA_DIR:?}/"*\n'
        '  mkdir -p "${OV_DATA_DIR}"\n'
        '}\n'
        'sync_openclaw_plugin_config() {\n'
        '  python3 - "${OPENCLAW_CONFIG_PATH}" "${OV_USER_ID}" "${OV_ACCOUNT_ID}" "${ISOLATE_USER_SCOPE_BY_AGENT}" "${ISOLATE_AGENT_SCOPE_BY_USER}" <<\'PY\'\n'
        'isolate_agent_scope_by_user = sys.argv[5].lower() == "true"\n'
        'cfg["userId"] = user_id\n'
        'PY\n'
        '}\n'
        'start_services() {\n'
        '  nohup python3 -m openviking.server.bootstrap --host 127.0.0.1 --port 1933 --workers 1 >"${OV_LOG}" 2>&1 &\n'
        '  source "${OPENCLAW_ENV}"\n'
        '  nohup openclaw gateway >"${GW_LOG}" 2>&1 &\n'
        '}\n'
        'run_phase_a() {\n'
        '  local -a args=(\n'
        '    python3 benchmark/locomo/openclaw/phase_a_off.py\n'
        '    "${DATA_PATH}"\n'
        '    --output-dir "${OUTPUT_DIR}"\n'
        '  )\n'
        '}\n'
        '{\n  backup_and_reset\n'
    )

    updated = MODULE._enable_isolated_runtime(shell_text)

    assert 'LOCOMO_EVAL_MODEL="${LOCOMO_EVAL_MODEL:-}"' in updated
    assert 'OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-$RUN_ID}"' in updated
    assert 'OPENCLAW_HOME_DIR="${OPENCLAW_HOME_DIR:-/tmp/openclaw-home-$RUN_ID}"' in updated
    assert 'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"' in updated
    assert 'OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-$RUN_ID}"' in updated
    assert 'OPENVIKING_PORT="${OPENVIKING_PORT:-21933}"' in updated
    assert 'OPENVIKING_AGFS_PORT="${OPENVIKING_AGFS_PORT:-21833}"' in updated
    assert 'OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-python3}"' in updated
    assert "bootstrap_isolated_runtime()" in updated
    assert 'rm -rf "${OPENCLAW_HOME_DIR}"' in updated
    assert 'ln -s "${OPENCLAW_STATE_DIR}" "${OPENCLAW_HOME_DIR}/.openclaw"' in updated
    assert 'base_config.pop("stateDir", None)' in updated
    assert 'config_path.write_text(json.dumps(base_config, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")' in updated
    assert 'plugin_manifest = extensions_dst / "openclaw.plugin.json"' in updated
    assert 'properties.setdefault("agent_prefix", {"type": "string"})' in updated
    assert 'cfg.setdefault("storage", {}).setdefault("agfs", {})["port"] = agfs_port' in updated
    assert 'target_conf.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")' in updated
    assert 'base_config["stateDir"] = str(state_dir)' not in updated
    assert '(cd /tmp && nohup "${OPENVIKING_PYTHON_BIN}" -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1' in updated
    assert 'export OPENCLAW_STATE_DIR' in updated
    assert 'export OPENCLAW_CONFIG_PATH' in updated
    assert 'nohup env HOME="${OPENCLAW_HOME_DIR}" OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" openclaw gateway >"${GW_LOG}" 2>&1 &' in updated
    assert "__OVTEST_PLUGIN_USERKEY__" in updated
    assert 'cfg["apiKey"] = str(cfg.get("apiKey") or user_key or "")' in updated
    assert 'cfg["agent_prefix"] = account_id' not in updated
    assert 'cfg.pop("agent_prefix", None)' in updated
    assert 'cfg.pop("accountId", None)' not in updated
    assert 'cfg.pop("userId", None)' not in updated
    assert 'cfg.pop("isolateUserScopeByAgent", None)' not in updated
    assert 'cfg.pop("isolateAgentScopeByUser", None)' not in updated
    assert '"plugin_api_key": "preserved"' in updated
    assert 'cfg["isolateUserScopeByAgent"] = isolate_user_scope_by_agent' in updated
    assert 'cfg["isolateAgentScopeByUser"] = isolate_agent_scope_by_user' in updated
    assert '--openclaw-state-dir "${OPENCLAW_STATE_DIR}"' in updated
    assert '--openviking-url "http://127.0.0.1:${OPENVIKING_PORT}"' in updated
    assert "{\n  bootstrap_isolated_runtime\n  backup_and_reset\n" in updated


def test_enable_isolated_runtime_is_idempotent_and_does_not_duplicate_flags():
    shell_text = (
        'RUN_ID="${RUN_ID:-${MODE}_small_$(date +%Y%m%d_%H%M%S)}"\n'
        'OV_CONF_PATH="${OV_CONF_PATH:-/root/.openviking/ov.conf}"\n'
        'OV_DATA_DIR="${OV_DATA_DIR:-/root/.openviking/data}"\n'
        'OPENCLAW_AGENT_DIR="${OPENCLAW_AGENT_DIR:-/root/.openclaw/agents/locomo-eval}"\n'
        'OPENCLAW_MAIN_AGENT_DIR="${OPENCLAW_MAIN_AGENT_DIR:-/root/.openclaw/agents/main/agent}"\n'
        'OPENCLAW_ENV="${OPENCLAW_ENV:-/root/.openclaw/openviking.env}"\n'
        'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/root/.openclaw/openclaw.json}"\n'
        'mkdir -p "${OUTPUT_DIR}"\n\n'
        'backup_and_reset() {\n'
        '  pkill -f \'phase_a_off.py\' || true\n'
        '  pkill -f \'openclaw-gateway\' || true\n'
        '  pkill -f \'python3 -m openviking.server.bootstrap --host 127.0.0.1 --port 1933 --workers 1\' || true\n'
        '  sleep 2\n\n'
        '  rm -rf "${OV_DATA_DIR:?}/"*\n'
        '  mkdir -p "${OV_DATA_DIR}"\n'
        '}\n'
        'sync_openclaw_plugin_config() {\n'
        '  python3 - "${OPENCLAW_CONFIG_PATH}" "${OV_USER_ID}" "${OV_ACCOUNT_ID}" "${ISOLATE_USER_SCOPE_BY_AGENT}" "${ISOLATE_AGENT_SCOPE_BY_USER}" <<\'PY\'\n'
        'isolate_agent_scope_by_user = sys.argv[5].lower() == "true"\n'
        'cfg["userId"] = user_id\n'
        'PY\n'
        '}\n'
        'start_services() {\n'
        '  nohup python3 -m openviking.server.bootstrap --host 127.0.0.1 --port 1933 --workers 1 >"${OV_LOG}" 2>&1 &\n'
        '  source "${OPENCLAW_ENV}"\n'
        '  nohup openclaw gateway >"${GW_LOG}" 2>&1 &\n'
        '}\n'
        'run_phase_a() {\n'
        '  local -a args=(\n'
        '    python3 benchmark/locomo/openclaw/phase_a_off.py\n'
        '    "${DATA_PATH}"\n'
        '    --output-dir "${OUTPUT_DIR}"\n'
        '  )\n'
        '}\n'
        '{\n  backup_and_reset\n'
    )

    updated = MODULE._enable_isolated_runtime(shell_text)
    updated_twice = MODULE._enable_isolated_runtime(updated)

    assert updated == updated_twice
    assert updated.count("--openclaw-state-dir") == 1
    assert updated.count("--openviking-url") == 1
    assert updated.count('--base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"') == 1
    assert updated.count("OPENVIKING_AGFS_PORT") >= 1
    assert updated.count("bootstrap_isolated_runtime()") == 1
    assert updated.count("  bootstrap_isolated_runtime\n  backup_and_reset\n") == 1


def test_enable_isolated_runtime_normalizes_previously_polluted_shell():
    shell_text = (
        'RUN_ID="${RUN_ID:-${MODE}_small_$(date +%Y%m%d_%H%M%S)}"\n'
        'LOCOMO_EVAL_MODEL="${LOCOMO_EVAL_MODEL:-}"\n'
        'OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-$RUN_ID}"\n'
        'OPENCLAW_HOME_DIR="${OPENCLAW_HOME_DIR:-/tmp/openclaw-home-$RUN_ID}"\n'
        'OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-28789}"\n'
        'OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-$RUN_ID}"\n'
        'OPENVIKING_PORT="${OPENVIKING_PORT:-21933}"\n'
        'BASE_OV_CONF_PATH="${BASE_OV_CONF_PATH:-/root/.openviking/ov.conf}"\n'
        'OV_CONF_PATH="${OV_CONF_PATH:-${OPENVIKING_INSTANCE_DIR}/ov.conf}"\n'
        'OV_DATA_DIR="${OV_DATA_DIR:-${OPENVIKING_INSTANCE_DIR}/data}"\n'
        'BASE_OPENCLAW_STATE_DIR="${BASE_OPENCLAW_STATE_DIR:-/root/.openclaw}"\n'
        'OPENCLAW_AGENT_DIR="${OPENCLAW_AGENT_DIR:-${OPENCLAW_STATE_DIR}/agents/locomo-eval}"\n'
        'OPENCLAW_MAIN_AGENT_DIR="${OPENCLAW_MAIN_AGENT_DIR:-${OPENCLAW_STATE_DIR}/agents/main/agent}"\n'
        'OPENCLAW_ENV="${OPENCLAW_ENV:-${OPENCLAW_STATE_DIR}/openviking.env}"\n'
        'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"\n'
        'OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-python3}"\n'
        'export OPENVIKING_CONFIG_FILE="${OV_CONF_PATH}"\n'
        'export OPENCLAW_STATE_DIR\n'
        'export OPENCLAW_CONFIG_PATH\n'
        'base_config["stateDir"] = str(state_dir)\n'
        'config_path.write_text(json.dumps(base_config, ensure_ascii=False, indent=2) + "\n"\n, encoding="utf-8")\n'
        'target_conf.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"\n, encoding="utf-8")\n'
        'start_services() {\n'
        '  source "${OPENCLAW_ENV}"\n'
        '  nohup openclaw gateway >"${GW_LOG}" 2>&1 &\n'
        '}\n'
        'run_phase_a() {\n'
        '  local -a args=(\n'
        '    --output-dir "${OUTPUT_DIR}"\n'
        '    --base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"\n'
        '    --base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"\n'
        '    --base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"\n'
        '  )\n'
        '}\n'
        '{\n'
        '  set_openclaw_gateway_port\n'
        '  set_openclaw_gateway_port\n'
        '  set_openclaw_gateway_port\n'
        '}\n'
    )

    updated = MODULE._enable_isolated_runtime(shell_text)

    assert updated.count('--base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"') == 1
    assert updated.count('--openviking-url "http://127.0.0.1:${OPENVIKING_PORT}"') == 1
    assert updated.count('--openclaw-state-dir "${OPENCLAW_STATE_DIR}"') == 1
    assert updated.count("  set_openclaw_gateway_port\n") == 1
    assert 'nohup env HOME="${OPENCLAW_HOME_DIR}" OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" openclaw gateway >"${GW_LOG}" 2>&1 &' in updated
    assert 'base_config["stateDir"] = str(state_dir)' not in updated
    assert 'config_path.write_text(json.dumps(base_config, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")' in updated
    assert 'target_conf.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")' in updated


def test_enable_isolated_runtime_normalizes_duplicated_openviking_plugin_lines():
    shell_text = (
        'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"\n'
        'sync_openclaw_plugin_config() {\n'
        '  python3 - "${OPENCLAW_CONFIG_PATH}" "${OV_USER_ID}" "${OV_ACCOUNT_ID}" "${ISOLATE_USER_SCOPE_BY_AGENT}" "${ISOLATE_AGENT_SCOPE_BY_USER}" "${OPENVIKING_PORT}" <<\'PY\'\n'
        'openviking_port = int(sys.argv[6])\n'
        'openviking_port = int(sys.argv[6])\n'
        'cfg["baseUrl"] = f"http://127.0.0.1:{openviking_port}"\n'
        'cfg["baseUrl"] = f"http://127.0.0.1:{openviking_port}"\n'
        'cfg["userId"] = user_id\n'
        'PY\n'
        '}\n'
        'run_phase_a() {\n'
        '  local -a args=(\n'
        '    --output-dir "${OUTPUT_DIR}"\n'
        '    --base-url "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}"\n'
        '  )\n'
        '}\n'
    )

    updated = MODULE._enable_isolated_runtime(shell_text)

    assert updated.count('openviking_port = int(sys.argv[6])') == 1
    assert updated.count('cfg["baseUrl"] = f"http://127.0.0.1:{openviking_port}"') == 1
    assert updated.count('--openviking-url "http://127.0.0.1:${OPENVIKING_PORT}"') == 1
    assert updated.count('--openclaw-state-dir "${OPENCLAW_STATE_DIR}"') == 1


def test_enable_isolated_runtime_normalizes_isolated_python3_bootstrap():
    shell_text = (
        'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"\n'
        'start_services() {\n'
        '  nohup python3 -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1 >"${OV_LOG}" 2>&1 &\n'
        '}\n'
    )

    updated = MODULE._enable_isolated_runtime(shell_text)

    assert 'OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-python3}"' in updated
    assert '(cd /tmp && nohup "${OPENVIKING_PYTHON_BIN}" -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1 >"${OV_LOG}" 2>&1 &)' in updated
    assert 'nohup python3 -m openviking.server.bootstrap --config "${OV_CONF_PATH}"' not in updated


def test_enable_isolated_runtime_normalizes_wm_mode_for_openviking_0324():
    shell_text = (
        'OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-python3}"\n'
        'set_wm_mode() {\n'
        '  python3 - "${OV_CONF_PATH}" "${MODE}" <<\'PY\'\n'
        'import json\n'
        'import sys\n'
        'from pathlib import Path\n'
        '\n'
        'conf_path = Path(sys.argv[1])\n'
        'mode = sys.argv[2]\n'
        'data = json.loads(conf_path.read_text(encoding="utf-8"))\n'
        'data.setdefault("memory", {})["wm_v2_preprocess_enabled"] = (mode == "on")\n'
        'conf_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")\n'
        'print(json.dumps(data["memory"], ensure_ascii=False))\n'
        'PY\n'
        '}\n'
    )

    updated = MODULE._enable_isolated_runtime(shell_text)

    assert '"${OPENVIKING_PYTHON_BIN}" - "${OV_CONF_PATH}" "${MODE}"' in updated
    assert 'if ov_version.startswith("0.3.24"):' in updated
    assert 'memory.pop("wm_v2_preprocess_enabled", None)' in updated


def test_ensure_openclaw_auto_recall_query_extract_uses_question_suffix():
    source = """
export function prepareRecallQuery(rawText: string): PreparedRecallQuery {
  const sanitized = sanitizeUserTextForCapture(rawText).trim();
  const originalChars = sanitized.length;

  if (!sanitized) {
    return {
      query: "",
      truncated: false,
      originalChars: 0,
      finalChars: 0,
    };
  }

  const query =
    sanitized.length > RECALL_QUERY_MAX_CHARS
      ? sanitized.slice(0, RECALL_QUERY_MAX_CHARS).trim()
      : sanitized;

  return {
    query,
    truncated: sanitized.length > RECALL_QUERY_MAX_CHARS,
    originalChars,
    finalChars: query.length,
  };
}
"""

    updated = MODULE._ensure_openclaw_auto_recall_query_extract(source)

    assert 'const questionMarker = "Question:";' in updated
    assert "const extracted = sanitized.slice(markerIndex + questionMarker.length).trim();" in updated


def test_ensure_openclaw_auto_recall_query_extract_is_idempotent():
    source = """
export function prepareRecallQuery(rawText: string): PreparedRecallQuery {
  const sanitized = sanitizeUserTextForCapture(rawText).trim();
  const originalChars = sanitized.length;

  if (!sanitized) {
    return {
      query: "",
      truncated: false,
      originalChars: 0,
      finalChars: 0,
    };
  }

  const query =
    sanitized.length > RECALL_QUERY_MAX_CHARS
      ? sanitized.slice(0, RECALL_QUERY_MAX_CHARS).trim()
      : sanitized;

  return {
    query,
    truncated: sanitized.length > RECALL_QUERY_MAX_CHARS,
    originalChars,
    finalChars: query.length,
  };
}
"""

    updated = MODULE._ensure_openclaw_auto_recall_query_extract(source)
    updated_twice = MODULE._ensure_openclaw_auto_recall_query_extract(updated)

    assert updated == updated_twice


def test_prepare_remote_runtime_uses_dynamic_openviking_site_packages_patterns():
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert 'glob.glob("/root/.openviking/venv*/lib/python*/site-packages/openviking/session/memory/session_extract_context_provider.py")' in source
    assert 'glob.glob("/root/.openviking/venv*/lib64/python*/site-packages/openviking/session/compressor_v2.py")' in source
    assert 'venv-0.3.24/lib/python3.11/site-packages/openviking/session/compressor_v2.py' not in source


def test_normalize_remote_python_source_preserves_triple_quoted_literal_indentation():
    source = (
        "        import base64\n"
        "        import glob\n"
        "        block = '''line0\n"
        "        keep-this-indent\n"
        "'''\n"
        "        print(block)\n"
    )

    updated = MODULE._normalize_remote_python_source(source)

    assert updated.splitlines()[0] == "import base64"
    assert updated.splitlines()[1] == "import glob"
    assert "        keep-this-indent" in updated
    assert updated.splitlines()[-1] == "print(block)"
