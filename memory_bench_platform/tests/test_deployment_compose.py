import json
import os
import shutil
import subprocess
import yaml

from memory_bench_platform.deployment_config import apply_config_overrides, load_deployment, resolve_groups
from memory_bench_platform.deployment_config_files import prepare_instance_configs
from memory_bench_platform.deployment_compose import render_project, required_resources
from test_deployment_config import fixture_config


def pooled_fixture(tmp_path):
    import yaml
    path, raw = fixture_config(tmp_path)
    raw["agents"]["claw"]["client"]["endpoint_env"] = "OPENCLAW_GATEWAY_URL"
    raw["agents"]["claw"]["state_mounts"] = [{"target": "/home/node/.openclaw"}]
    raw["agents"]["claw"]["resources"] = {"memory_mb": 256}
    raw["gateway_pools"] = {"pool": {"kind": "openclaw_http_pool_v1", "image": {"name": "pool:1"},
                                      "command": ["python", "gateway_pool.py"], "container_port": 29789,
                                      "healthcheck": ["CMD", "true"], "resources": {"memory_mb": 128}}}
    raw["combinations"][0].update(gateway_pool="pool", agent_replicas=3)
    path.write_text(yaml.safe_dump(raw))
    return path, raw


def test_openclaw_ipv4_dns_config_reaches_node_lookup(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw["agents"]["claw"]["environment"] = {
        "MEMORY_BENCH_IPV4_ONLY_HOSTS": "provider.example",
        "NODE_OPTIONS": "--max-old-space-size=256",
    }
    path.write_text(yaml.safe_dump(raw))
    group = resolve_groups(load_deployment(path))[0]
    agent = render_project(group, "suite", {})["services"]["agent_claw_common"]
    other = render_project(group, "suite", {})["services"]["agent_worker_common"]
    mount, = [volume for volume in agent["volumes"] if volume["target"].endswith("ipv4_provider_dns.cjs")]
    assert mount["read_only"] is True
    assert agent["environment"]["NODE_OPTIONS"] == f"--max-old-space-size=256 --require={mount['target']}"
    assert "NODE_OPTIONS" not in other["environment"]

    if shutil.which("node") is None:
        return
    preload = tmp_path / "probe.cjs"
    preload.write_text("""
const dns = require('node:dns');
global.dnsCalls = [];
dns.lookup = (host, options, callback) => {
  global.dnsCalls.push([host, options]);
  callback(null, '127.0.0.1', 4);
};
""")
    probe = """
const dns = require('node:dns');
dns.lookup('provider.example', {}, () => {});
dns.lookup('other.example', {}, () => {});
process.stdout.write(JSON.stringify(global.dnsCalls));
"""
    node_options = agent["environment"]["NODE_OPTIONS"].replace(mount["target"], mount["source"])
    env = {**os.environ, **agent["environment"], "NODE_OPTIONS": f"--require={preload} {node_options}"}
    result = subprocess.run(["node", "-e", probe], env=env, text=True, capture_output=True, check=True)
    calls = json.loads(result.stdout)
    assert calls[0][1]["family"] == 4
    assert "family" not in calls[1][1]


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


def test_pool_renders_isolated_replicas_and_explicit_upstreams(tmp_path):
    path, _ = pooled_fixture(tmp_path)
    group = resolve_groups(apply_config_overrides(load_deployment(path),
                                                   ['claw-common:log_level="debug"'], []))[0]
    mounts = prepare_instance_configs(group, tmp_path / "generated")
    compose = render_project(group, "suite", mounts)
    services = compose["services"]
    names = [f"agent_claw_common_{n}" for n in range(1, 4)]
    assert all(name in services for name in names)
    assert len({mounts[name][0]["source"] for name in names}) == 3
    import json
    from pathlib import Path
    assert all(json.loads(Path(mounts[name][0]["source"]).read_text())["logging"]["level"] == "debug"
               for name in names)
    assert len({services[name]["volumes"][-1]["source"] for name in names}) == 3
    pool = services["gateway_pool_claw_common"]
    assert list(pool["depends_on"]) == names
    assert pool["command"] == ["python", "gateway_pool.py", "--host", "0.0.0.0", "--port", "29789",
                               *[part for name in names for part in ("--upstream", f"http://{name}:8080")],
                               "--expected", "3"]
    assert all("ports" not in services[name] for name in names)
    assert pool["ports"][0]["host_ip"] == "127.0.0.1"
    assert required_resources((group,)).memory_mb == 896
