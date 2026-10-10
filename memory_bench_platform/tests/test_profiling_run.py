import json

import pytest

from memory_bench_platform.profiling_config import ProfilingConfig
from memory_bench_platform.profiling_run import mark_phase_unavailable, prepare_phase_sink


def _config(mode="best_effort"):
    return ProfilingConfig.model_validate({
        "schema": "profiling/1", "mode": mode, "targets": ["memory"],
        "phases": ["ingest", "recall"], "hardware": {"stat": "off"},
        "external_targets": {"memory": {"host_pid": 1,
                                        "cgroup_path": "/sys/fs/cgroup/example"}},
    })


def test_unavailable_runner_has_explicit_collector_status(tmp_path):
    sink = prepare_phase_sink(_config(), tmp_path, "run-1", {})
    mark_phase_unavailable(sink)
    items = json.loads((tmp_path / "profiling" / "collector_status.json").read_text())["items"]
    assert [(item["phase_id"], item["state"], item["reason"]) for item in items] == [
        ("ingest", "skipped", "phase_boundary_unavailable"),
        ("recall", "skipped", "phase_boundary_unavailable")]


def test_required_unavailable_target_fails_before_work(tmp_path):
    with pytest.raises(RuntimeError, match="hardware_off"):
        prepare_phase_sink(_config("required"), tmp_path, "run-1", {})
