from types import SimpleNamespace
import json

import pytest

from skills.agents.openclaw.scripts import gateway_recovery as recovery


def test_quarantine_requires_matching_plugin_and_operation():
    line = ("[context-engine] owner=plugin:example-engine failed during assemble: failure; "
            "quarantining it for this process")
    assert recovery.engine_quarantined(line, "example-engine", "assemble")
    assert not recovery.engine_quarantined(line, "another-engine", "assemble")
    assert not recovery.engine_quarantined(line, "example-engine", "compact")


@pytest.mark.parametrize("owned,engine,expected", [
    (False, "example-engine", "label"),
    (True, "example-engine-extension", "did not confirm"),
    (True, "example-engine", None),
])
@pytest.mark.parametrize("duration", ["; 0.9s", ""])
def test_restart_requires_owned_gateway_and_exact_plugin(tmp_path, monkeypatch, owned, engine, expected, duration):
    commands = []
    now = [0.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setenv("EXAMPLE_GATEWAY_CONTAINER", "sample-gateway")
    monkeypatch.setattr(recovery, "wait_gateway", lambda config, container: commands.append("ready"))

    def docker(cmd, **kwargs):
        commands.append(cmd[1])
        if cmd[1] == "inspect":
            if "--format" in cmd:
                return SimpleNamespace(returncode=0, stdout="true", stderr="")
            info = {"Config": {"Labels": {"memory-bench.runtime": str(tmp_path) if owned else "other"}},
                    "Mounts": [{"Source": str(tmp_path)}]}
            output = json.dumps([info])
        elif cmd[1] == "logs":
            assert cmd[2] == "--since" and float(cmd[3]) > 0
            output = f"[gateway] http server listening (2 plugins: other-plugin, {engine}{duration})"
        else:
            assert cmd == ["docker", "restart", "sample-gateway"]
            output = ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(recovery.subprocess, "run", docker)
    if expected:
        with pytest.raises((ValueError, RuntimeError), match=expected):
            recovery.restart_dedicated_gateway(tmp_path, "EXAMPLE_GATEWAY_CONTAINER", {}, expected_plugin="example-engine")
    else:
        recovery.restart_dedicated_gateway(tmp_path, "EXAMPLE_GATEWAY_CONTAINER", {}, expected_plugin="example-engine")
    assert commands[:4] == (["inspect", "restart", "ready", "logs"] if owned else ["inspect"])
    if owned:
        record = json.loads((tmp_path / "gateway-readiness.json").read_text())
        assert record["confirmed"] is (expected is None)
        assert record["attempts"] == (1 if expected is None else 30)


@pytest.mark.parametrize("exit_early", [False, True])
def test_plugin_confirmation_retries_delayed_logs(tmp_path, monkeypatch, exit_early):
    monkeypatch.setattr(recovery, "dedicated_container", lambda *args: "sample")
    monkeypatch.setattr(recovery, "wait_gateway", lambda *args: None)
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: None)
    attempts = []
    restarts = []

    def docker(cmd, **kwargs):
        output = ""
        if cmd[1] == "restart":
            restarts.append(cmd)
        elif cmd[1] == "logs":
            attempts.append(cmd[3])
            if len(attempts) == 2:
                output = "http server listening (1 plugin: engine; 1s)"
        else:
            output = "false" if exit_early else "true"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(recovery.subprocess, "run", docker)
    if exit_early:
        with pytest.raises(RuntimeError, match="exited"):
            recovery.restart_dedicated_gateway(tmp_path, "ENV", {}, expected_plugin="engine")
    else:
        recovery.restart_dedicated_gateway(tmp_path, "ENV", {}, expected_plugin="engine")
    assert len(restarts) == 1
    assert len(attempts) == (1 if exit_early else 2)
    assert len(set(attempts)) == 1
