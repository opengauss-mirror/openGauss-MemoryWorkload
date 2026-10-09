import json

import pytest
import yaml

from memory_bench_platform.deployment_config import (
    apply_config_overrides,
    load_deployment,
    resolve_groups,
)


def fixture_config(tmp_path):
    (tmp_path / "agent.json").write_text('{"logging":{"level":"info"}}')
    (tmp_path / "memory.yaml").write_text("server:\n  workers: 2\n")
    (tmp_path / "data.json").write_text("[]")
    service = {
        "image": {"name": "example/service:1"},
        "container_port": 8080,
        "healthcheck": ["CMD", "true"],
        "verify": {"command": ["true"], "expected_version": "1"},
        "client": {"endpoint_env": "SERVICE_URL"},
    }
    agents = {
        "claw": {**service, "skill": "openclaw", "service_endpoints": {"MEMORY_URL": "memory"},
                 "config_files": {"runtime": {"source": "./agent.json", "target": "/runtime/agent.json", "format": "json",
                                              "parameters": {"log_level": {"pointer": "/logging/level", "type": "string"}}}}},
        "worker": {**service, "skill": "openclaw", "client": {"endpoint_env": "WORKER_URL"},
                   "service_endpoints": {"MEMORY_URL": "memory"}},
    }
    memories = {
        "mem0": {**service, "skill": "mem0", "client": {"endpoint_env": "MEMORY_URL"},
                 "config_files": {"runtime": {"source": "./memory.yaml", "target": "/runtime/memory.yaml", "format": "yaml",
                                              "parameters": {"workers": {"pointer": "/server/workers", "type": "integer"}}}}},
    }
    raw = {"schema": "deployment/1", "name": "suite", "agents": agents, "memories": memories,
           "shared_memory_instances": {"mem0-common": {"memory": "mem0"}},
           "combinations": [
               {"id": "claw-common", "benchmark": "locomo", "agent": "claw", "memory_instance": "mem0-common",
                "memory_integration": "agent_plugin", "data_path": "./data.json"},
               {"id": "worker-common", "benchmark": "locomo", "agent": "worker", "memory_instance": "mem0-common",
                "memory_integration": "agent_plugin", "data_path": "./data.json"},
           ]}
    path = tmp_path / "deployment.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path, raw


def test_shared_memory_group_is_resolved_once(tmp_path):
    path, _ = fixture_config(tmp_path)
    groups = resolve_groups(load_deployment(path))
    assert len(groups) == 1
    assert groups[0].combination_ids == ("claw-common", "worker-common")
    assert groups[0].shared_memory_instance_id == "mem0-common"
    assert groups[0].memory_template.skill == "mem0"


def test_typed_config_overrides_and_rejections(tmp_path):
    path, _ = fixture_config(tmp_path)
    config = load_deployment(path)
    changed = apply_config_overrides(config, ['claw-common:log_level="debug"'], ['mem0-common:workers=4'])
    assert changed.agent_config_overrides["claw-common"]["log_level"] == "debug"
    assert changed.memory_config_overrides["mem0-common"]["workers"] == 4
    for agent_items, memory_items in [
        (["missing:log_level=\"debug\""], []),
        (["claw-common:workers=4"], []),
        (["claw-common:log_level=4"], []),
        (["claw-common:log_level=no-json"], []),
        (["claw-common:log_level=\"a\"", "claw-common:log_level=\"b\""], []),
        ([], ["claw-common:workers=4"]),
    ]:
        with pytest.raises(ValueError):
            apply_config_overrides(config, agent_items, memory_items)


def test_invalid_references_and_config_paths_fail(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["combinations"][0]["mock"] = "absent"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="mock"):
        load_deployment(path)
    raw["combinations"][0].pop("mock")
    raw["agents"]["claw"]["config_files"]["runtime"]["parameters"]["log_level"]["pointer"] = "/missing"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="pointer"):
        load_deployment(path)


def test_unknown_yaml_field_and_duplicate_combination_rejected(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["unexpected"] = True
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_deployment(path)
    raw.pop("unexpected")
    raw["combinations"].append(raw["combinations"][0])
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="duplicate"):
        load_deployment(path)
