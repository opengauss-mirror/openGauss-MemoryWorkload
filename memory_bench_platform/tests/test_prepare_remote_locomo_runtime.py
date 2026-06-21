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
        'cfg.pop("agent_prefix", None)\n'
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
    assert 'OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR}/openclaw.json}"' in updated
    assert 'OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-$RUN_ID}"' in updated
    assert 'OPENVIKING_PORT="${OPENVIKING_PORT:-21933}"' in updated
    assert 'OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-python3}"' in updated
    assert "bootstrap_isolated_runtime()" in updated
    assert 'nohup "${OPENVIKING_PYTHON_BIN}" -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1' in updated
    assert 'export OPENCLAW_STATE_DIR' in updated
    assert 'export OPENCLAW_CONFIG_PATH' in updated
    assert 'nohup openclaw gateway >"${GW_LOG}" 2>&1 &' in updated
    assert '--openclaw-state-dir "${OPENCLAW_STATE_DIR}"' in updated
    assert '--openviking-url "http://127.0.0.1:${OPENVIKING_PORT}"' in updated
    assert "{\n  bootstrap_isolated_runtime\n  backup_and_reset\n" in updated


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
