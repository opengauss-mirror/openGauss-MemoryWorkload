import json
from pathlib import Path

import yaml

from memory_bench_platform.deployment_config import apply_config_overrides, load_deployment, resolve_groups
from memory_bench_platform.deployment_config_files import prepare_instance_configs
from test_deployment_config import fixture_config


def test_shared_memory_uses_one_copy_and_sources_stay_unchanged(tmp_path):
    path, _ = fixture_config(tmp_path)
    before = (tmp_path / "memory.yaml").read_text()
    config = apply_config_overrides(load_deployment(path),
                                    ['claw-common:log_level="debug"'], ['mem0-common:workers=4'])
    group = resolve_groups(config)[0]
    mounts = prepare_instance_configs(group, tmp_path / "generated")
    assert set(mounts) == {"agent_claw_common", "agent_worker_common", "memory"}
    agent_copy = Path(mounts["agent_claw_common"][0]["source"])
    memory_copy = Path(mounts["memory"][0]["source"])
    assert json.loads(agent_copy.read_text())["logging"]["level"] == "debug"
    assert yaml.safe_load(memory_copy.read_text())["server"]["workers"] == 4
    assert (tmp_path / "memory.yaml").read_text() == before
    assert len(mounts["memory"]) == 1
    assert agent_copy.parent.stat().st_mode & 0o777 == 0o700
    assert agent_copy.stat().st_mode & 0o777 == 0o600
