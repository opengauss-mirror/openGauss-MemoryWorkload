import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
import yaml

from memory_bench_platform.deployment_lifecycle import deploy, status, undeploy, DeploymentFailed
from memory_bench_platform.deployment_run import run_deployment
from memory_bench_platform.deployment_plugins import deployment_adapter, runtime_issues
from test_deployment_config import fixture_config
from test_deployment_lifecycle import RecordingDocker
from test_ogmemory_plugin import upstream


def configured(tmp_path, provider):
    path, raw = fixture_config(tmp_path)
    raw['combinations'] = raw['combinations'][:1]
    raw['memories']['mem0']['skill'] = provider
    raw['agents']['claw'].pop('config_files')
    source = tmp_path / 'native.json'
    if provider == 'ogmemory':
        package = upstream(tmp_path)
        source.write_text('{}')
    else:
        package = tmp_path / 'node_modules/@mem0/openclaw-mem0'
        (package / 'dist').mkdir(parents=True)
        (package / 'package.json').write_text('{"name":"@mem0/openclaw-mem0","version":"1.2.0"}')
        (package / 'openclaw.plugin.json').write_text('{}')
        (package / 'dist/index.js').write_text('// official fixture')
        source.write_text(json.dumps({'plugins': {'entries': {'openclaw-mem0': {'config': {
            'mode': 'open-source', 'oss': {'vectorStore': {'provider': 'qdrant'}}}}}}}))
    if provider == 'mem0':
        raw.pop('shared_memory_instances')
        raw['combinations'][0].pop('memory_instance')
        raw['combinations'][0]['memory'] = 'mem0'
        raw['memories']['mem0'] = {'skill': 'mem0', 'placement': 'agent', 'client': {'endpoint_env': 'MEMORY_URL'}}
        raw['agents']['claw']['service_endpoints'] = {}
    raw['combinations'][0]['plugin_runtime'] = {'source_config': str(source), 'upstream_plugin': str(package)}
    path.write_text(yaml.safe_dump(raw))
    return path, raw


@pytest.mark.parametrize('provider', ['ogmemory', 'mem0'])
def test_deploy_bind_repeat_and_cleanup(tmp_path, provider):
    path, raw = configured(tmp_path, provider)
    original = Path(raw['combinations'][0]['plugin_runtime']['source_config']).read_bytes()
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    binding = state.plugin_runtimes['claw-common']
    directory = Path(binding['directory'])
    cid = state.containers[binding['group']][binding['service']]
    assert docker.specs[cid]['ports'][0]['published'] > 0
    assert directory.is_relative_to(tmp_path / '.memory-bench')
    assert directory.joinpath('openclaw.json').stat().st_mode & 0o777 == 0o600
    assert not runtime_issues(binding)
    if provider == 'ogmemory':
        config = json.loads(directory.joinpath('openclaw.json').read_text())
        assert config['plugins']['entries']['og-memory-context-engine']['hooks']['allowConversationAccess'] is True
    seen = []
    def invoke(argv, *, env, cwd):
        seen.append(env)
        assert env['OPENCLAW_GATEWAY_URL'] == 'http://127.0.0.1:28080'
        assert env['OPENCLAW_STATE_DIR'] == str(directory / 'state')
        assert env['OGMEM_API_URL' if provider == 'ogmemory' else 'MEM0_GATEWAY_CONTAINER']
        cid = state.containers[binding['group']][binding['service']]
        docker.containers[cid]['State']['StartedAt'] = 'after native phase restart'
        return 0
    for _ in range(2):
        assert run_deployment('suite', tmp_path, None, docker, invoke) == 0
        assert status('suite', tmp_path, docker).ready
    assert len(seen) == 2
    assert Path(raw['combinations'][0]['plugin_runtime']['source_config']).read_bytes() == original
    undeploy('suite', tmp_path, docker)
    assert not directory.exists()
    assert docker.removed_projects


@pytest.mark.parametrize('damage', ['active', 'config', 'stopped', 'unhealthy'])
def test_failed_finalize_is_not_adopted(tmp_path, damage):
    path, _ = configured(tmp_path, 'ogmemory')
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    binding = state.plugin_runtimes['claw-common']
    directory = Path(binding['directory'])
    def invoke(argv, *, env, cwd):
        if damage == 'active':
            (directory / 'phase.json').write_text('{"active":true}')
        elif damage == 'config':
            config = json.loads((directory / 'openclaw.json').read_text())
            config['models'] = {'wrong': 'changed'}
            (directory / 'openclaw.json').write_text(json.dumps(config))
        else:
            cid = state.containers[binding['group']][binding['service']]
            if damage == 'stopped':
                docker.containers[cid]['State']['Running'] = False
            else:
                docker.containers[cid]['State']['Health']['Status'] = 'unhealthy'
        return 0
    assert run_deployment('suite', tmp_path, None, docker, invoke) == 1
    assert not status('suite', tmp_path, docker).ready
    undeploy('suite', tmp_path, docker)


def test_prepare_failure_rolls_back_private_directory(tmp_path):
    path, raw = configured(tmp_path, 'ogmemory')
    (Path(raw['combinations'][0]['plugin_runtime']['upstream_plugin']) / 'openclaw.plugin.json').write_text('{}')
    with pytest.raises(DeploymentFailed):
        deploy(path, tmp_path, RecordingDocker())
    assert not (tmp_path / '.memory-bench/deployments/suite').exists()


def test_core_dispatches_an_unrelated_agent_and_memory(tmp_path, monkeypatch):
    import memory_bench_platform.deployment_plugins as bridge
    root = tmp_path / 'memory_plugins/another-pair'
    root.mkdir(parents=True)
    (root / 'deploy.py').write_text('def prepare(*args): return {}\ndef environment(*args): return {}\ndef issues(*args): return []\n')
    manifest = SimpleNamespace(id='another-pair', agent='another-agent', memory='another-memory',
                               entry=SimpleNamespace(deployer='deploy.py'))
    monkeypatch.setattr(bridge, 'SKILLS_ROOT', tmp_path)
    monkeypatch.setattr(bridge, 'load_all_skills', lambda _: {'memory_plugins': [manifest]})
    script, adapter = deployment_adapter('another-agent', 'another-memory')
    assert script == root / 'deploy.py'
    assert adapter['environment']({}, {}) == {}
    with pytest.raises(ValueError, match='No deployment adapter'):
        deployment_adapter('unknown', 'unknown')


@pytest.mark.parametrize('host_port,url,valid,host_ip', [
    ('29999', 'http://127.0.0.1:29999', True, '127.0.0.1'),
    ('29999', 'http://127.0.0.1:29999', True, '0.0.0.0'),
    ('29999', 'http://127.0.0.1:18789', False, '127.0.0.1'),
    ('29999', 'http://remote.example:29999', False, '127.0.0.1')])
def test_dynamic_gateway_binding(monkeypatch, host_port, url, valid, host_ip):
    script = Path(__file__).resolve().parents[1] / 'skills/memory_plugins/scripts/deployment_openclaw.py'
    helper = runpy.run_path(str(script))
    monkeypatch.setenv('OPENCLAW_GATEWAY_URL', url)
    info = {'NetworkSettings': {'Ports': {'18789/tcp': [{'HostIp': host_ip, 'HostPort': host_port}]}}}
    monkeypatch.setattr(helper['subprocess'], 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps([info])))
    if valid:
        assert helper['validate_gateway_url']({'gateway': {'port': 18789}}, 'dedicated') == url
    else:
        with pytest.raises(ValueError):
            helper['validate_gateway_url']({'gateway': {'port': 18789}}, 'dedicated')


@pytest.mark.parametrize('provider', ['ogmemory', 'mem0'])
def test_inactive_config_without_gateway_ack_is_not_reusable(tmp_path, provider):
    path, _ = configured(tmp_path, provider)
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    binding = state.plugin_runtimes['claw-common']
    phase = Path(binding['directory']) / 'phase.json'
    phase.write_text('{"active":false,"phase":"inactive","gateway_ready":false}')
    assert not status('suite', tmp_path, docker).ready
    phase.write_text('{"active":false,"phase":"inactive","gateway_ready":true}')
    assert status('suite', tmp_path, docker).ready
    undeploy('suite', tmp_path, docker)


def locomo_rows(count):
    return [{"sample_id": str(i),
             "conversation": {"session_1_date_time": "2024-03-15",
                              "session_1": [{"speaker": "Alice", "text": "I prefer tea.", "dia_id": "D1:1"}]},
             "qa": [{"question": "Drink?", "answer": "tea", "category": 1, "evidence": ["D1:1"]}]}
            for i in range(count)]


@pytest.mark.parametrize(("samples", "gateways"), [(1, 1), (3, 2)])
@pytest.mark.parametrize("benchmark", ["locomo", "longmemeval", "beam"])
def test_managed_ogmemory_gateways_share_service(tmp_path, samples, gateways, benchmark):
    path, raw = configured(tmp_path, "ogmemory")
    combo = raw["combinations"][0]
    combo["benchmark"] = benchmark
    combo["execution"] = {"sample_concurrency": 2}
    rows = locomo_rows(samples) if benchmark == "locomo" else [{"sample_id": str(i)} for i in range(samples)]
    if benchmark == "beam":
        rows = [{"conversation_id": str(i),
                 "chat": [{"role": "user", "content": "Remember my preference for tea."}],
                 "probing_questions": {"information_extraction": [
                     {"question": "What drink?", "rubric": ["tea"]}]}}
                for i in range(samples)]
    (tmp_path / combo["data_path"]).write_text(json.dumps(rows))
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    bindings = list(state.plugin_runtimes.values())
    assert len({item["directory"] for item in bindings}) == gateways
    assert len({item["service"] for item in bindings}) == gateways
    assert len({item["group"] for item in bindings}) == 1
    for binding in bindings:
        cid = state.containers[binding["group"]][binding["service"]]
        assert docker.specs[cid]["healthcheck"]["interval"] == "10s"
    assert "memory" in state.containers[bindings[0]["group"]]


def test_managed_gateway_preparation_and_qa_budget(tmp_path, monkeypatch):
    from memory_bench_platform.cli import _execution_options
    from memory_bench_platform import deployment_run

    path, raw = configured(tmp_path, "ogmemory")
    combo = raw["combinations"][0]
    raw.pop("shared_memory_instances")
    combo.pop("memory_instance")
    combo.update(memory="mem0", execution={"sample_concurrency": 2, "qa_concurrency": 2, "judge_concurrency": 4})
    combo["preparation"] = {step: {"enabled": True, "target": "agent", "command": [command]}
                           for step, command in (("seed", "seed"), ("readiness", "ready"), ("warmup", "warmup"))}
    raw["agents"]["claw"]["healthcheck_interval_seconds"] = 90
    (tmp_path / combo["data_path"]).write_text(json.dumps(locomo_rows(2)))
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    ids = {state.containers[b["group"]][b["service"]] for b in state.plugin_runtimes.values()}
    assert len(ids) == 2
    assert all(docker.specs[cid]["healthcheck"]["interval"] == "90s" for cid in ids)
    elapsed = [0]
    def invoke(argv, *, env, cwd):
        monkeypatch.setenv("MEMORY_BENCH_DEPLOYED_SAMPLE_GATEWAYS", env["MEMORY_BENCH_DEPLOYED_SAMPLE_GATEWAYS"])
        spec = _execution_options(SimpleNamespace(memory_integration="agent_plugin", memory_backend="ogmemory",
            sample_concurrency=int(argv[argv.index("--sample-concurrency") + 1]),
            qa_concurrency=int(argv[argv.index("--qa-concurrency") + 1]),
            judge_concurrency=int(argv[argv.index("--judge-concurrency") + 1])))
        assert spec.sample_concurrency == spec.qa_concurrency == 2
        assert spec.judge_concurrency == 4
        for cid in ids:
            docker.containers[cid]["State"]["Health"]["Status"] = "starting"
        return 0
    def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] >= 90:
            for cid in ids:
                docker.containers[cid]["State"]["Health"]["Status"] = "healthy"
    monkeypatch.setattr(deployment_run, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))
    assert run_deployment("suite", tmp_path, None, docker, invoke) == 0
    for command in ("seed", "ready", "warmup"):
        assert {cid for cid, cmd in docker.exec_calls if cmd == [command]} == ids
    assert elapsed[0] == 90


@pytest.mark.parametrize(("data_format", "count"), [("object", 1), ("jsonl", 2)])
def test_beam_gateway_count_uses_scenario_builder(tmp_path, data_format, count):
    path, raw = configured(tmp_path, "ogmemory")
    combo = raw["combinations"][0]
    combo.update(benchmark="beam", execution={"sample_concurrency": 3})
    rows = [{"conversation_id": str(i),
             "chat": [{"role": "user", "content": "I prefer tea."}],
             "probing_questions": {"information_extraction": [
                 {"question": "What drink?", "rubric": ["tea"]}]}}
            for i in range(count)]
    content = json.dumps(rows[0]) if data_format == "object" else "\n".join(map(json.dumps, rows))
    (tmp_path / combo["data_path"]).write_text(content)
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    assert len(state.plugin_runtimes) == count
    undeploy("suite", tmp_path, docker)
