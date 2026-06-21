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
