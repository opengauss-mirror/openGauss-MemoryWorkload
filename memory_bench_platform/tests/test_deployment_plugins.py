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
