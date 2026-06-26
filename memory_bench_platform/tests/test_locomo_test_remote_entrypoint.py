from pathlib import Path


def test_locomo_test_remote_entrypoint_keeps_health_check_enabled():
    script = Path("/mnt/d/code/Agent/test/tools/test_entrypoints/run_locomo_test_remote.sh")
    text = script.read_text(encoding="utf-8")

    assert 'LOCOMO_TEST_CONFIG="${LOCOMO_TEST_CONFIG:-openviking-small-stable.toml}"' in text
    assert 'OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-${RUN_ID}}"' in text
    assert 'OPENCLAW_HOME_DIR="${OPENCLAW_HOME_DIR:-/tmp/openclaw-home-${RUN_ID}}"' in text
    assert 'OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-$(python3 -c' in text
    assert 'OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-${RUN_ID}}"' in text
    assert 'OPENVIKING_PORT="${OPENVIKING_PORT:-$(python3 -c' in text
    assert 'OV_CONF_PATH="${OV_CONF_PATH:-${OPENVIKING_INSTANCE_DIR}/ov.conf}"' in text
    assert 'OV_DATA_DIR="${OV_DATA_DIR:-${OPENVIKING_INSTANCE_DIR}/data}"' in text
    assert 'OPENVIKING_PYTHON_BIN="${OPENVIKING_PYTHON_BIN:-/root/.openviking/venv-0.3.24/bin/python}"' in text
    assert 'python3 -m locomo_test.bootstrap_remote_runtime \\' in text
    assert '--base-state-dir /root/.openclaw \\' in text
    assert '--base-ov-conf "${OV_CONF_PATH}" \\' in text
    assert '--runtime-config-src "${REMOTE_ROOT}/configs/${LOCOMO_TEST_CONFIG}" \\' in text
    assert '--runtime-config-dst "${REMOTE_ROOT}/configs/${LOCOMO_TEST_CONFIG%.toml}-runtime.toml" \\' in text
    assert 'nohup "${OPENVIKING_PYTHON_BIN}" -m openviking.server.bootstrap --config "${OV_CONF_PATH}" --host 127.0.0.1 --port "${OPENVIKING_PORT}" --workers 1 >"${OV_LOG}" 2>&1 &' in text
    assert 'curl -fsS "http://127.0.0.1:${OPENVIKING_PORT}/health"' in text
    assert 'nohup env HOME="${OPENCLAW_HOME_DIR}" OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR}" OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" OPENVIKING_BASE_URL="${OPENVIKING_BASE_URL:-http://127.0.0.1:${OPENVIKING_PORT}}" OPENVIKING_API_KEY="${OPENVIKING_API_KEY:-}" openclaw gateway >"${GW_LOG}" 2>&1 &' in text
    assert 'curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/health"' in text
    assert 'python3 -m locomo_test.cli run "configs/${LOCOMO_TEST_CONFIG%.toml}-runtime.toml"' in text
    assert "--skip health_check" not in text
