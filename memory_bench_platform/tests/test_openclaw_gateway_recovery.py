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
    monkeypatch.setenv("EXAMPLE_GATEWAY_CONTAINER", "sample-gateway")
    monkeypatch.setattr(recovery, "wait_gateway", lambda config, container: commands.append("ready"))

    def docker(cmd, **kwargs):
        commands.append(cmd[1])
        if cmd[1] == "inspect":
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
    assert commands == (["inspect", "restart", "ready", "logs"] if owned else ["inspect"])
