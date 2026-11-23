import json
import os
from pathlib import Path

import pytest
import yaml

from memory_bench_platform.deployment_env import deployment_environment, expand_environment
from memory_bench_platform.deployment_config import load_deployment, resolve_groups
from memory_bench_platform.deployment_config_files import prepare_instance_configs
from test_deployment_config import fixture_config


def configured(tmp_path):
    path, raw = fixture_config(tmp_path)
    raw['env_file'] = '.env'
    raw['name'] = '${BENCH_NAME}'
    path.write_text(yaml.safe_dump(raw))
    (tmp_path / '.env').write_text('export BENCH_NAME=dotenv-bench\n')
    return path, raw


def test_dotenv_is_explicit_relative_and_scoped(tmp_path, monkeypatch):
    path, _ = configured(tmp_path)
    monkeypatch.delenv('BENCH_NAME', raising=False)
    monkeypatch.chdir(tmp_path.parent)
    with deployment_environment(path):
        assert load_deployment(path).name == 'dotenv-bench'
    assert 'BENCH_NAME' not in os.environ
    monkeypatch.setenv('BENCH_NAME', 'shell-bench')
    with deployment_environment(path):
        assert load_deployment(path).name == 'shell-bench'
    assert os.environ['BENCH_NAME'] == 'shell-bench'


def test_missing_explicit_file_fails(tmp_path):
    path, _ = configured(tmp_path)
    (tmp_path / '.env').unlink()
    with pytest.raises(ValueError, match='missing env_file'):
        load_deployment(path)


def test_scalar_substitution_never_parses_credentials(monkeypatch):
    key = 'quote"\nnew: field\n$(touch /tmp/never) ${OTHER}'
    monkeypatch.setenv('TOKEN', key)
    assert expand_environment({'apiKey': '${TOKEN}'}) == {'apiKey': key}
    assert expand_environment('$${TOKEN}') == '${TOKEN}'
    assert expand_environment('${UNSET_BENCH_TEST:-default}') == 'default'
    with pytest.raises(ValueError, match='UNSET_BENCH_TEST'):
        expand_environment('${UNSET_BENCH_TEST}')


def test_rendered_service_config_is_private_and_source_unchanged(tmp_path, monkeypatch):
    path, raw = configured(tmp_path)
    source = tmp_path / 'model.json'
    source.write_text('{"api_key":"${MODEL_KEY}"}')
    service = next(iter(raw['memories'].values()))
    service['config_files'] = {'model': {'source': 'model.json', 'target': '/model.json',
                                         'format': 'json', 'expand_env': True}}
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv('MODEL_KEY', 'secret"value')
    with deployment_environment(path):
        group = resolve_groups(load_deployment(path))[0]
        mounts = prepare_instance_configs(group, tmp_path / 'rendered')
    generated = Path(mounts['memory'][0]['source'])
    assert json.loads(generated.read_text())['api_key'] == 'secret"value'
    assert generated.stat().st_mode & 0o777 == 0o600
    assert '${MODEL_KEY}' in source.read_text()


def test_compose_escapes_expanded_values_and_preserves_environment_references(tmp_path, monkeypatch):
    from memory_bench_platform.deployment_compose import compose_document, compose_expectations, render_project

    path, raw = configured(tmp_path)
    agent = raw["agents"]["claw"]
    agent["environment"] = {"LITERAL": "$${OTHER}", "PASSWORD": "${PASSWORD}",
                            "MEMORY_BENCH_IPV4_ONLY_HOSTS": "example.test"}
    agent["environment_from"] = {"FORWARDED": "PASSWORD", "NODE_OPTIONS": "NODE_OPTIONS_SOURCE"}
    agent["command"] = ["echo", "$${OTHER}"]
    agent["healthcheck"] = ["CMD-SHELL", 'test -n "$$PASSWORD"']
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("PASSWORD", "demo$OTHER")
    group, = resolve_groups(load_deployment(path))
    mount = {"type": "bind", "source": str(tmp_path / "$source"), "target": "/config/$file"}
    rendered = render_project(group, "dotenv-bench", {"agent_claw_common": [mount]})
    name = "agent_claw_common"
    document = compose_document(rendered, {name: group.agents["claw-common"]})
    service = document["services"][name]
    assert service["environment"]["LITERAL"] == "$${OTHER}"
    assert service["environment"]["PASSWORD"] == "demo$$OTHER"
    assert service["environment"]["FORWARDED"] == "${PASSWORD}"
    assert service["environment"]["NODE_OPTIONS"].startswith("${NODE_OPTIONS_SOURCE} ")
    assert service["command"] == ["echo", "$${OTHER}"]
    assert service["healthcheck"]["test"] == ["CMD-SHELL", 'test -n "$$PASSWORD"']
    assert compose_expectations(document)["services"] == rendered["services"]
    assert compose_expectations(rendered) == rendered


def test_cli_experiment_needs_no_profiling_and_loads_env(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from memory_bench_platform import cli, profiling_experiment
    path, _ = configured(tmp_path)
    monkeypatch.delenv('BENCH_NAME', raising=False)
    def run(config, profile, *args, **kwargs):
        assert profile is None
        assert os.environ['BENCH_NAME'] == 'dotenv-bench'
        return SimpleNamespace(archive_dir=tmp_path, status='completed')
    monkeypatch.setattr(profiling_experiment, 'run_experiment', run)
    cli.main(['experiment', '--config', str(path)])
    assert 'BENCH_NAME' not in os.environ


def test_environment_drift_blocks_reuse(tmp_path, monkeypatch):
    from memory_bench_platform.deployment_lifecycle import deploy, status
    from test_deployment_lifecycle import RecordingDocker
    path, _ = configured(tmp_path)
    docker = RecordingDocker()
    with deployment_environment(path):
        state = deploy(path, tmp_path, docker)
        assert status(state.name, tmp_path, docker).ready
        monkeypatch.setenv('BENCH_NAME', 'changed-name')
        checked = status(state.name, tmp_path, docker)
        assert not checked.ready
        assert any('environment changed' in item for item in checked.drift)


@pytest.mark.parametrize('summary,expected', [
    ({'status':'passed','run_validity':{'valid':True}}, 0),
    ({'status':'partial','benchmark_score':0,'run_validity':{'valid':True}}, 0),
    ({'status':'failed','run_validity':{'valid':False}}, 1),
    ({'status':'passed','run_validity':{'valid':False}}, 1),
    (None, 1),
])
def test_native_exit_zero_does_not_hide_invalid_evaluation(tmp_path, monkeypatch, summary, expected):
    from types import SimpleNamespace
    from memory_bench_platform import deployment_run as module
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    if summary is not None:
        p = tmp_path / 'runs/r1/reports/summary.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(summary))
    assert module._invoke_native(['run','--run-id','r1'],env={},cwd=tmp_path) == expected


@pytest.mark.parametrize('provider', ['ogmemory', 'mem0', 'openviking', 'hindsight', 'jiuwenmemory', 'memos'])
def test_shipped_examples_prepare_native_runtime(tmp_path, provider, monkeypatch):
    import shutil
    from dotenv import dotenv_values
    from memory_bench_platform.deployment_compose import render_project
    from memory_bench_platform.deployment_plugins import prepare_plugin_runtimes
    from test_deployment_more_plugins import fixture, SAMPLE
    from test_deployment_plugins import configured as plugin_fixture
    fixture_root = tmp_path / 'fixtures'
    fixture_root.mkdir()
    _, raw = (plugin_fixture(fixture_root, provider) if provider in ('ogmemory', 'mem0')
              else fixture(fixture_root, provider))
    example = tmp_path / 'example'
    repo = Path(__file__).resolve().parents[2]
    shutil.copytree(repo / 'examples' / (provider + '-openclaw'), example)
    dataset = tmp_path / 'data.json'
    dataset.write_text(json.dumps([SAMPLE]))
    native = tmp_path / 'backend.yaml'
    native.write_text('{}')
    values = dotenv_values(example / '.env.example', interpolate=False)
    values.update(DATA_PATH=str(dataset), MEMORY_CONFIG_PATH=str(native))
    spec = raw['combinations'][0]['plugin_runtime']
    if spec.get('upstream_plugin'):
        values['PLUGIN_PATH'] = str(fixture_root / spec['upstream_plugin'])
    for key, value in values.items():
        monkeypatch.delenv(key, raising=False)
    (example / '.env').write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
    with deployment_environment(example / 'deployment.yaml'):
        cfg = load_deployment(example / 'deployment.yaml')
        group = resolve_groups(cfg)[0]
        target = tmp_path / 'generated'
        target.mkdir()
        mounts = prepare_instance_configs(group, target / 'configs')
        rendered = render_project(group, cfg.name, mounts, target)
        binding = prepare_plugin_runtimes(group, target, rendered)[provider + '-openclaw']
    output = json.loads((Path(binding['directory']) / 'openclaw.json').read_text())
    assert output['models']['providers']['bench']['apiKey'] == values['LLM_API_KEY']
    assert output['agents']['defaults']['model']['primary'] == 'bench/' + values['LLM_MODEL']
    if provider == 'mem0':
        oss = output['plugins']['entries']['openclaw-mem0']['config']['oss']
        assert oss['embedder']['config']['embeddingDims'] == 1024
    if provider == 'ogmemory':
        native = yaml.safe_load(Path(mounts['memory'][0]['source']).read_text())
        assert native['llm']['api_key'] == '${LLM_API_KEY}'
