from pathlib import Path


def test_official_locomo_small_uses_shared_remote_lock_and_process_guard():
    script = Path("/mnt/d/code/Agent/test/tools/test_entrypoints/run_official_locomo_small.sh")
    text = script.read_text(encoding="utf-8")

    assert 'LOCK_FILE="\\$LOCK_DIR/locomo_eval.lock"' in text
    assert 'LOCK_PID=\\$(cat "\\$LOCK_FILE" 2>/dev/null || true)' in text
    assert 'kill -0 "\\${LOCK_PID}" 2>/dev/null' in text
    assert 'LOCK_CMD=\\$(ps -p "\\${LOCK_PID}" -o args= 2>/dev/null || true)' in text
    assert 'grep -F "phase_a_off.py"' in text
    assert 'rm -f "\\$LOCK_FILE"' in text
    assert 'pgrep -af \\"phase_a_off.py\\"' in text
    assert 'RUN_CONFLICT:' in text
    assert 'REMOTE_RUNTIME_LOCK_FILE="${REMOTE_LOCK_DIR}/official_small_runtime.lock"' in text
    assert 'acquire_remote_runtime_lock' in text
    assert 'release_remote_runtime_lock' in text
    assert 'pid=\\$(cat \\"${REMOTE_RUNTIME_LOCK_FILE}\\" 2>/dev/null || true)' in text
    assert 'grep -F \\"run_official_locomo_small.sh\\"' in text
    assert 'OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-${RUN_ID}}"' in text
    assert 'OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-${RUN_ID}}"' in text
    assert 'EXPECTED_OPENVIKING_VERSION="${MEMORY_BENCH_EXPECTED_OPENVIKING_VERSION:-}"' in text
    assert 'EXPECTED_OPENCLAW_VERSION="${MEMORY_BENCH_EXPECTED_OPENCLAW_VERSION:-}"' in text
    assert 'OPENVIKING_INTROSPECT_PYTHON_BIN="${OPENVIKING_INTROSPECT_PYTHON_BIN:-}"' in text
    assert 'PLATFORM_RUNS_ROOT="${PLATFORM_RUNS_ROOT:-${WORKSPACE_ROOT}/memory_bench_platform/runs}"' in text
    assert 'PLATFORM_IMPORT_ENABLED="${PLATFORM_IMPORT_ENABLED:-true}"' in text
    assert 'remote_container_port_is_free()' in text
    assert 'resolve_remote_free_port()' in text
    assert 'run_clean_small_in_container.sh' in text
    assert 'export LOCK_FILE="/tmp/locomo-openclaw-benchmark-${RUN_ID}.lock"' in text
    assert 'isolate_user_scope_by_agent' in text
    assert '--no-isolate-user-scope-by-agent' in text
    assert 'failed to provision OpenViking user key' in text
    assert 'plugin_api_key' in text
    assert 'isolate_user_scope_by_agent\\\\\\":true' in text
    assert 'isolate_agent_scope_by_user\\\\\\":true' in text
    assert 'keeping root API key with explicit tenant headers' in text
    assert 'cfg["agent_prefix"] = account_id' in text
    assert 'cfg["apiKey"] = str(cfg.get("apiKey") or user_key or "")' in text
    assert '"plugin_api_key": "preserved"' in text
    assert 'call_pattern = re.compile(' in text
    assert 'agent_id=ov_agent_id' in text
    assert 'account_root=Path(DEFAULT_OV_DATA_ROOT)' in text
    assert 'OPENCLAW_GATEWAY_PORT="$(resolve_remote_free_port "${OPENCLAW_GATEWAY_PORT}" 28999)"' in text
    assert 'OPENVIKING_PORT="$(resolve_remote_free_port "${OPENVIKING_PORT}" 21999)"' in text
    assert 'OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-${OPENVIKING_INTROSPECT_PYTHON_BIN}}"' in text
    assert text.index('"/root/.openviking/venv-0.3.24/bin/python"') < text.index('"/root/.openviking/venv/bin/python"')
    assert 'local normalized_expected_openviking="${EXPECTED_OPENVIKING_VERSION#v}"' in text
    assert 'actual_ov="${actual_ov#v}"' in text
    assert 'expected_ov="${expected_ov#v}"' in text
    assert 'OpenViking runtime version mismatch' in text
    assert 'OpenClaw runtime version mismatch' in text
    assert 'import_official_locomo_run.py' in text
    assert 'python3 -m memory_bench_platform.cli analyze-run --run-dir "${PLATFORM_RUN_DIR}"' in text
    assert 'timing_report_html=${LOCAL_OUTPUT_DIR}/reports/timing_report.html' in text
