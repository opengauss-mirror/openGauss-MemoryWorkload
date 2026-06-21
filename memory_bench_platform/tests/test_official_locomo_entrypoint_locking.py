from pathlib import Path


def test_official_locomo_small_uses_shared_remote_lock_and_process_guard():
    script = Path("/mnt/d/code/Agent/test/tools/test_entrypoints/run_official_locomo_small.sh")
    text = script.read_text(encoding="utf-8")

    assert 'LOCK_FILE="\\$LOCK_DIR/locomo_eval.lock"' in text
    assert 'pgrep -af \\"phase_a_off.py\\"' in text
    assert 'RUN_CONFLICT:' in text
    assert 'REMOTE_RUNTIME_LOCK_FILE="${REMOTE_LOCK_DIR}/official_small_runtime.lock"' in text
    assert 'acquire_remote_runtime_lock' in text
    assert 'release_remote_runtime_lock' in text
    assert 'OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-state-${RUN_ID}}"' in text
    assert 'OPENVIKING_INSTANCE_DIR="${OPENVIKING_INSTANCE_DIR:-/tmp/openviking-${RUN_ID}}"' in text
    assert 'EXPECTED_OPENVIKING_VERSION="${MEMORY_BENCH_EXPECTED_OPENVIKING_VERSION:-}"' in text
    assert 'EXPECTED_OPENCLAW_VERSION="${MEMORY_BENCH_EXPECTED_OPENCLAW_VERSION:-}"' in text
    assert 'OPENVIKING_INTROSPECT_PYTHON_BIN="${OPENVIKING_INTROSPECT_PYTHON_BIN:-}"' in text
    assert 'OpenViking runtime version mismatch' in text
    assert 'OpenClaw runtime version mismatch' in text
