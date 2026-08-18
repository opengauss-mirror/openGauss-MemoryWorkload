from pathlib import Path

import pytest

from locomo_test.config import Config
from locomo_test.pipeline import _lock_path, ensure_run_lock, release_run_lock


def test_lock_path_defaults_to_output_dir(tmp_path: Path):
    cfg = Config(name="demo")
    lock_path = _lock_path(cfg, str(tmp_path / "out"))
    assert lock_path == tmp_path / "out" / ".run.lock"


def test_lock_path_can_use_configured_lock_dir(tmp_path: Path):
    cfg = Config(name="demo", run_lock_dir=str(tmp_path / "locks"))
    lock_path = _lock_path(cfg, str(tmp_path / "out"))
    assert lock_path == tmp_path / "locks" / "demo.lock"


def test_ensure_run_lock_rejects_existing_lock(tmp_path: Path):
    lock_path = tmp_path / "demo.lock"
    ensure_run_lock(lock_path)
    with pytest.raises(RuntimeError, match="existing run lock"):
        ensure_run_lock(lock_path)
    release_run_lock(lock_path)
