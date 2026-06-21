from pathlib import Path


def test_official_locomo_small_uses_shared_remote_lock_and_process_guard():
    script = Path("/mnt/d/code/Agent/test/tools/test_entrypoints/run_official_locomo_small.sh")
    text = script.read_text(encoding="utf-8")

    assert 'LOCK_FILE="\\$LOCK_DIR/locomo_eval.lock"' in text
    assert 'pgrep -af \\"phase_a_off.py\\"' in text
    assert 'RUN_CONFLICT:' in text
