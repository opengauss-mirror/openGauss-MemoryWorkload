import pytest
import yaml

from memory_bench_platform.profiling_config import load_profiling_config


def test_profiling_1_strict_schema_and_defaults(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"schema": "profiling/1", "targets": ["memory"]}))
    config = load_profiling_config(path)
    assert config.mode == "best_effort"
    assert config.phases == ["ingest", "drain", "recall"]
    assert config.hardware.stat == "basic"
    assert config.quality_gates.min_counter_running_fraction == 0.90


@pytest.mark.parametrize("patch", [
    {"schema": "profiling/2"}, {"unknown": True}, {"phases": []},
    {"targets": []}, {"quality_gates": {"min_request_join_rate": 1.1}},
    {"targets": ["../outside"]},
    {"command": ["sudo", "perf"]}, {"token": "secret"},
])
def test_invalid_profiling_config_is_rejected(tmp_path, patch):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"schema": "profiling/1", "targets": ["memory"], **patch}))
    with pytest.raises(ValueError):
        load_profiling_config(path)


def test_external_target_identity_is_kept_out_of_workload_config(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"schema": "profiling/1", "targets": ["i"],
        "external_targets": {"i": {"host_pid": 42, "cgroup_path": "/sys/fs/cgroup/test", "cpu_scope": "0-3"}}}))
    config = load_profiling_config(path)
    assert config.external_targets["i"].host_pid == 42


def test_file_trace_paths_load_from_yaml(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump({"schema": "profiling/1", "targets": ["memory"],
        "internal_trace": {"source": "file", "path": str(tmp_path / "trace.log"),
                           "stage_map": str(tmp_path / "map.json")}}))
    config = load_profiling_config(path)
    assert config.internal_trace.path == tmp_path / "trace.log"
