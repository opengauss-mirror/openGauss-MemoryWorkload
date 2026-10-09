import json

import yaml

from memory_bench_platform.deployment_lifecycle import deploy
from memory_bench_platform.deployment_run import run_deployment
from test_deployment_config import fixture_config
from test_deployment_lifecycle import RecordingDocker


def test_run_deployment_routes_each_combination_and_summarizes(tmp_path, monkeypatch):
    path, raw = fixture_config(tmp_path)
    for service in [*raw["agents"].values(), *raw["memories"].values()]:
        service.pop("service_endpoints", None)
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    deploy(path, tmp_path, docker)
    monkeypatch.setenv("MEMORY_URL", "http://wrong.invalid")
    seen = []

    def invoke(argv, *, env, cwd):
        seen.append((argv, env.copy(), cwd))
        return 0

    rc = run_deployment("suite", tmp_path, None, docker, invoke)
    assert rc == 0
    assert len(seen) == 2
    assert seen[0][1]["MEMORY_URL"] == "http://127.0.0.1:28080"
    assert seen[0][1]["SERVICE_URL"] == "http://127.0.0.1:28080"
    assert "WORKER_URL" not in seen[0][1]
    assert seen[1][1]["WORKER_URL"] == "http://127.0.0.1:28080"
    assert "SERVICE_URL" not in seen[1][1]
    summaries = list((tmp_path / "runs").glob("suite-*/suite_summary.json"))
    assert len(summaries) == 1
    assert len(json.loads(summaries[0].read_text())["combinations"]) == 2

