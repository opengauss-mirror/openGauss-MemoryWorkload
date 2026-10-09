from memory_bench_platform.deployment_config import apply_config_overrides, load_deployment, resolve_groups
from memory_bench_platform.deployment_config_files import prepare_instance_configs
from memory_bench_platform.deployment_compose import render_project, required_resources
from test_deployment_config import fixture_config


def test_shared_project_rendering_and_resource_count(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["agents"]["claw"]["resources"] = {"memory_mb": 256}
    raw["agents"]["worker"]["resources"] = {"memory_mb": 256}
    raw["memories"]["mem0"]["resources"] = {"memory_mb": 1024}
    import yaml
    path.write_text(yaml.safe_dump(raw))
    group = resolve_groups(load_deployment(path))[0]
    mounts = prepare_instance_configs(group, tmp_path / "generated")
    compose = render_project(group, "suite", mounts)
    assert sum(name.startswith("memory") for name in compose["services"]) == 1
    assert compose["services"]["agent_claw_common"]["volumes"][0]["read_only"] is True
    assert compose["services"]["agent_claw_common"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert "agent_net_claw_common" in compose["networks"]
    assert "agent_net_worker_common" in compose["networks"]
    assert required_resources((group,)).memory_mb == 1536

