import json
from pathlib import Path

import pytest
import yaml

from test_deployment_config import fixture_config
from test_deployment_lifecycle import RecordingDocker


def _files(tmp_path):
    config, raw = fixture_config(tmp_path)
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({"schema": "profiling/1", "targets": ["memory"],
                                       "hardware": {"stat": "off"}}), encoding="utf-8")
    return config, profile


def _fake_run(tmp_path, *, return_code=0):
    def run(*args, **kwargs):
        suite = tmp_path / "runs" / "suite-sample"
        suite.mkdir(exist_ok=True)
        (suite / "suite_summary.json").write_text(json.dumps({"combinations": [
            {"run_id": "child-1", "status": "passed" if return_code == 0 else "failed"}]}),
            encoding="utf-8")
        (tmp_path / "runs" / "child-1").mkdir(exist_ok=True)
        return return_code
    return run


def test_experiment_deploy_verify_run_analyze_order(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_experiment as module
    config, profile = _files(tmp_path)
    docker = RecordingDocker()
    calls = []
    real_deploy, real_status = module.deploy, module.status
    monkeypatch.setattr(module, "deploy", lambda *args: calls.append("deploy") or real_deploy(*args))
    monkeypatch.setattr(module, "status", lambda *args: calls.append("verify") or real_status(*args))
    def fake_run(*args, **kwargs):
        calls.append("run")
        return _fake_run(tmp_path)(*args, **kwargs)
    monkeypatch.setattr(module, "run_deployment", fake_run)
    monkeypatch.setattr(module, "analyze_run", lambda *_args: calls.append("analyze"))
    result = module.run_experiment(config, profile, tmp_path, docker)
    assert calls == ["deploy", "verify", "run", "analyze"]
    assert result.status == "completed"
    assert (result.archive_dir / "experiment.json").is_file()


def test_existing_deployment_is_never_cleaned(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_experiment as module
    config, profile = _files(tmp_path)
    docker = RecordingDocker()
    module.deploy(config, tmp_path, docker)
    monkeypatch.setattr(module, "run_deployment", _fake_run(tmp_path))
    monkeypatch.setattr(module, "analyze_run", lambda *_args: None)
    removed = []
    monkeypatch.setattr(module, "undeploy", lambda *args: removed.append(args))
    result = module.run_experiment(config, profile, tmp_path, docker,
                                   cleanup_owned_deployment=True)
    assert result.owned_deployment is False
    assert removed == []


def test_owned_cleanup_only_when_requested(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_experiment as module
    config, profile = _files(tmp_path)
    docker = RecordingDocker()
    monkeypatch.setattr(module, "run_deployment", _fake_run(tmp_path))
    monkeypatch.setattr(module, "analyze_run", lambda *_args: None)
    result = module.run_experiment(config, profile, tmp_path, docker,
                                   cleanup_owned_deployment=True)
    assert result.owned_deployment is True
    assert not (tmp_path / ".memory-bench" / "deployments" / "suite").exists()
    assert (result.archive_dir / "experiment.json").is_file()


def test_run_failure_retains_experiment_archive(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_experiment as module
    config, profile = _files(tmp_path)
    monkeypatch.setattr(module, "run_deployment", _fake_run(tmp_path, return_code=1))
    monkeypatch.setattr(module, "analyze_run", lambda *_args: None)
    result = module.run_experiment(config, profile, tmp_path, RecordingDocker())
    assert result.status == "failed"
    assert json.loads((result.archive_dir / "experiment.json").read_text())["status"] == "failed"


def test_missing_suite_summary_is_not_reported_as_completed(tmp_path, monkeypatch):
    from memory_bench_platform import profiling_experiment as module
    config, profile = _files(tmp_path)
    monkeypatch.setattr(module, "run_deployment", lambda *args, **kwargs: 0)
    with pytest.raises(RuntimeError, match="suite summary"):
        module.run_experiment(config, profile, tmp_path, RecordingDocker())
    archived = list((tmp_path / "runs" / "experiments").glob("*/experiment.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["status"] == "failed"


def test_pressure_test_still_rejected(tmp_path):
    from memory_bench_platform.profiling_experiment import run_experiment
    config, profile = _files(tmp_path)
    with pytest.raises(ValueError, match="pressure-test"):
        run_experiment(config, profile, tmp_path, RecordingDocker(), pressure_test="many")
    assert not (tmp_path / ".memory-bench" / "deployments" / "suite").exists()


def test_unsupported_platform_best_effort_keeps_skipped_hardware(tmp_path):
    from memory_bench_platform.profiling_config import load_profiling_config
    from memory_bench_platform.profiling_run import prepare_phase_sink
    _, profile = _files(tmp_path)
    selected = load_profiling_config(profile)
    selected = selected.model_copy(update={"external_targets": {
        "memory": type("Target", (), {"host_pid": 1,
                                         "cgroup_path": "/sys/fs/cgroup/example",
                                         "cpu_scope": None})()}})
    sink = prepare_phase_sink(selected, tmp_path / "profile-output", "r", {})
    sink.run_phase("ingest", ["memory"], lambda: None)
    status = json.loads((tmp_path / "profile-output" / "profiling" / "collector_status.json").read_text())
    assert status["items"][0]["state"] == "skipped"


def test_experiment_cli_routes_config_and_cleanup(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from memory_bench_platform import cli, deployment_docker, profiling_experiment
    config, profile = _files(tmp_path)
    seen = []
    monkeypatch.setattr(deployment_docker, "DockerClient", lambda: object())
    monkeypatch.setattr(profiling_experiment, "run_experiment", lambda *args, **kwargs:
                        seen.append((args, kwargs)) or SimpleNamespace(archive_dir=tmp_path, status="completed"))
    cli.main(["experiment", "--config", str(config), "--profiling-config", str(profile),
              "--cleanup-owned-deployment"])
    assert seen[0][1]["cleanup_owned_deployment"] is True
    assert capsys.readouterr().out.strip() == str(tmp_path)
