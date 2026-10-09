import json
from pathlib import Path

import pytest
import yaml

from memory_bench_platform import cli
from memory_bench_platform import deployment_docker, deployment_run
from test_deployment_config import fixture_config
from test_deployment_lifecycle import RecordingDocker


def test_deploy_status_run_undeploy_commands(tmp_path, monkeypatch, capsys):
    path, raw = fixture_config(tmp_path)
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    monkeypatch.setattr(deployment_docker, "DockerClient", lambda: docker)
    monkeypatch.chdir(tmp_path)
    seen = []
    original_run = deployment_run.run_deployment

    def fake_run(name, cwd, combination, docker):
        return original_run(name, cwd, combination, docker,
                            lambda argv, *, env, cwd: seen.append((argv, env)) or 0)

    monkeypatch.setattr(deployment_run, "run_deployment", fake_run)
    cli.main(["deploy", "--config", str(path), "--agent-config", 'claw-common:log_level="debug"'])
    state = json.loads((tmp_path / ".memory-bench/deployments/suite/state.json").read_text())
    assert state["config_origins"]["mem0-common/agent_claw_common"] == ["log_level"]
    assert "debug" not in json.dumps(state)
    cli.main(["deploy", "status", "--deployment", "suite"])
    assert '"ready": true' in capsys.readouterr().out
    cli.main(["run", "--deployment", "suite", "--combination", "claw-common"])
    assert len(seen) == 1
    generated = tmp_path / ".memory-bench/deployments/suite/mem0-common/configs/agent_claw_common/runtime.json"
    generated.write_text('{"logging":{"level":"warning"}}')
    with pytest.raises(SystemExit):
        cli.main(["deploy", "status", "--deployment", "suite"])
    cli.main(["undeploy", "--deployment", "suite"])
    assert docker.removed_projects == set(state["projects"].values())
    assert not (tmp_path / ".memory-bench/deployments/suite").exists()
