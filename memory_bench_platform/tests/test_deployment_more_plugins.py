import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest
import yaml
from memory_bench_platform.deployment_lifecycle import deploy, status, undeploy
from memory_bench_platform.deployment_config import load_deployment
from memory_bench_platform.deployment_run import run_deployment
from memory_bench_platform.deployment_plugins import deployment_adapter
from test_deployment_config import fixture_config
from test_deployment_lifecycle import RecordingDocker

SAMPLE = {'sample_id':'s1','conversation':{'speaker_a':'Alice','speaker_b':'Bob',
    'session_1_date_time':'2024-01-01','session_1':[{'speaker':'Alice','text':'I live in Paris.','dia_id':'D1:1'}]},
    'qa':[{'question':'Where does Alice live?','answer':'Paris','category':1,'evidence':['D1:1']}]}


def fixture(tmp_path, provider):
    path, raw = fixture_config(tmp_path)
    raw['combinations']=raw['combinations'][:1]
    raw.pop('shared_memory_instances')
    combo=raw['combinations'][0];combo.pop('memory_instance');combo['memory']='mem0'
    raw['agents']['claw'].pop('config_files')
    raw['memories']['mem0']['skill']=provider
    (tmp_path/'data.json').write_text(json.dumps([SAMPLE]))
    (tmp_path/'native.json').write_text('{}')
    combo['plugin_runtime']={'source_config':'native.json'}
    if provider!='jiuwenmemory':
        package=tmp_path/'official';(package/'dist').mkdir(parents=True)
        pid={'hindsight':'hindsight-openclaw','openviking':'openviking','memos':'memos-local-plugin'}[provider]
        version='2.0.8' if provider=='memos' else '1.2.0'
        (package/'package.json').write_text(json.dumps({'name':'@memtensor/memos-local-plugin' if provider=='memos' else pid,
            'version':version,'openclaw':{'extensions':['dist/index.js']}}))
        (package/'dist/index.js').write_text('// fixture; not a memory implementation')
        (package/'openclaw.plugin.json').write_text(json.dumps({'id':pid,'configSchema':{
            'properties':{k:{'type':'boolean'} for k in ('autoCapture','autoRecall','baseUrl')}}}))
        combo['plugin_runtime']['upstream_plugin']='official'
    if provider=='memos':
        (tmp_path/'memos.yaml').write_text('version: 1\nllm: {}\nembedding: {}\n')
        combo['plugin_runtime']['memory_config']='memos.yaml'
        raw['memories']['mem0']={'skill':'memos','placement':'agent','container_port':18799,
                                  'client':{'endpoint_env':'MEMOS_API_URL'}}
    path.write_text(yaml.safe_dump(raw))
    return path, raw


@pytest.mark.parametrize('provider',['openviking','hindsight','jiuwenmemory','memos'])
def test_real_adapter_prepare_bind_drift_cleanup(tmp_path, provider):
    path, _=fixture(tmp_path,provider);docker=RecordingDocker()
    state=deploy(path,tmp_path,docker)
    binding=state.plugin_runtimes['claw-common'];root=Path(binding['directory'])
    assert (root/'openclaw.json').stat().st_mode & 0o777==0o600
    assert status('suite',tmp_path,docker).ready
    config=json.loads((root/'openclaw.json').read_text())
    seen=[]
    def invoke(argv,*,env,cwd):
        seen.append(env)
        assert env['OPENCLAW_GATEWAY_URL']=='http://127.0.0.1:28080'
        return 0
    assert run_deployment('suite',tmp_path,None,docker,invoke)==0
    if provider=='memos':
        assert 'memory' not in state.containers[binding['group']]
        assert seen[0]['MEMOS_LOCAL_VIEWER_URL']=='http://127.0.0.1:38799'
        assert config['plugins']['slots']['memory']=='memos-local-plugin'
    elif provider=='jiuwenmemory':
        assert seen[0]['JIUWENMEMORY_MCP_URL']=='http://memory:8080/mcp'
        assert config['agents']['list'][0]['tools']['allow']==['jiuwen__memory_add']
        assert config['agents']['list'][1]['tools']['allow']==['jiuwen__memory_search']
    elif provider=='hindsight':
        assert seen[0]['HINDSIGHT_PLUGIN_API_URL']=='http://127.0.0.1:28080'
    else:
        assert seen[0]['OPENVIKING_PLUGIN_API_URL']=='http://127.0.0.1:28080'
    config['models']={'unexpected':'change'};(root/'openclaw.json').write_text(json.dumps(config))
    assert not status('suite',tmp_path,docker).ready
    undeploy('suite',tmp_path,docker)
    assert not root.exists()


def test_memos_accepts_multiple_samples_and_rejects_separate_memory_container(tmp_path):
    path,raw=fixture(tmp_path,'memos')
    (tmp_path/'data.json').write_text(json.dumps([SAMPLE,{**SAMPLE,'sample_id':'s2'}]))
    load_deployment(path)
    raw['memories']['mem0']['placement']='service'
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):load_deployment(path)


def test_memos_compose_overrides_image_healthcheck_interval(tmp_path):
    path, raw = fixture(tmp_path, 'memos')
    raw['agents']['claw']['healthcheck_interval_seconds'] = 5
    path.write_text(yaml.safe_dump(raw))
    docker = RecordingDocker()
    state = deploy(path, tmp_path, docker)
    binding = state.plugin_runtimes['claw-common']
    compose = Path(binding['directory']).parents[1] / 'compose.yaml'
    service = yaml.safe_load(compose.read_text())['services'][binding['service']]
    assert service['healthcheck']['interval'] == '5s'
    undeploy('suite', tmp_path, docker)


def test_hindsight_host_operations_url(monkeypatch):
    script,adapter=deployment_adapter('openclaw','hindsight')
    native=runpy.run_path(str(script.with_name('run_lifecycle.py')))
    seen=[]
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{"bank_id":"sample","operations":[]}'
    monkeypatch.setenv('HINDSIGHT_PLUGIN_API_URL','http://127.0.0.1:32123')
    monkeypatch.setattr(native['urllib'].request,'build_opener',lambda *a:SimpleNamespace(open=lambda req,**kw:seen.append(req.full_url) or Reply()))
    assert native['operations']({'plugins':{'entries':{'hindsight-openclaw':{'config':{'hindsightApiUrl':'http://memory:8000'}}}}},'sample')==[]
    assert seen[0].startswith('http://127.0.0.1:32123/')


def test_openviking_managed_phases_restore_config(tmp_path,monkeypatch):
    path,_=fixture(tmp_path,'openviking');docker=RecordingDocker();state=deploy(path,tmp_path,docker)
    b=state.plugin_runtimes['claw-common'];root=Path(b['directory']);original=json.loads((root/'openclaw.json').read_text())
    script,adapter=deployment_adapter('openclaw','openviking')
    control=runpy.run_path(str(script.with_name('deployment_control.py')))
    native=runpy.run_path(str(script.with_name('run_lifecycle.py')))
    monkeypatch.setenv('OPENVIKING_DEPLOYMENT_RUNTIME',str(root));monkeypatch.setenv('OPENCLAW_CONFIG_PATH',str(root/'openclaw.json'))
    g=control['run'].__globals__;g['gateway']=lambda:'owned';g['restart']=lambda:None
    native['run'].__globals__['_deployment_control']=lambda:control
    def call(action,**inputs):return native['run']({'action':action,'inputs':inputs,'runtime_context':{'run_dir':str(tmp_path/'run')}})
    call('validate');call('prepare',namespace='isolated');call('set_phase',phase='ingest')
    config=json.loads((root/'openclaw.json').read_text())['plugins']['entries']['openviking']['config']
    assert config['autoCapture'] and not config['autoRecall']
    call('set_phase',phase='qa');config=json.loads((root/'openclaw.json').read_text())['plugins']['entries']['openviking']['config']
    assert not config['autoCapture'] and config['autoRecall']
    call('finalize');assert json.loads((root/'openclaw.json').read_text())==original
    assert adapter['issues'](b)==[]
    undeploy('suite',tmp_path,docker)


def test_foreign_owned_plugin_is_copied_without_changing_source(tmp_path, monkeypatch):
    import os
    script,_=deployment_adapter('openclaw','hindsight')
    common=runpy.run_path(str(script.parents[2]/'scripts/deployment_openclaw.py'))
    source=tmp_path/'source';source.mkdir();(source/'code.js').write_text('official code')
    original_stat=Path.stat
    def stat(path,*args,**kwargs):
        result=original_stat(path,*args,**kwargs)
        if path==source:
            values=list(result);values[4]=os.getuid()+12345
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path,'stat',stat)
    copied=common['owned_package'](source,tmp_path/'owned')
    assert copied!=source
    assert (copied/'code.js').read_bytes()==(source/'code.js').read_bytes()
    (copied/'code.js').write_text('changed copy')
    assert (source/'code.js').read_text()=='official code'


@pytest.mark.parametrize("command, accepted", [
    (["gateway", "run"], True),
    (["openclaw", "gateway", "run"], True),
    (["openclaw", "gateway", "status"], False),
])
def test_jiuwen_gateway_command(tmp_path, monkeypatch, command, accepted):
    adapter = Path(__file__).resolve().parents[1] / "skills/memory_plugins/openclaw-jiuwenmemory/scripts/run_lifecycle.py"
    validate = runpy.run_path(str(adapter))["validate_gateway_binding"]
    config = tmp_path / "openclaw.json"
    config.touch()
    state = tmp_path / "state"
    state.mkdir()
    info = {
        "State": {"Running": True, "StartedAt": "2099-01-01T00:00:00Z"},
        "HostConfig": {"NetworkMode": "bridge"},
        "Config": {"Cmd": command, "Labels": {"memory-bench.runtime": str(tmp_path)},
                   "Env": ["OPENCLAW_CONFIG_PATH=/runtime/openclaw.json", "OPENCLAW_STATE_DIR=/runtime/state"]},
        "Mounts": [{"Type": "bind", "Source": str(tmp_path), "Destination": "/runtime"}],
    }
    monkeypatch.setenv("JIUWENMEMORY_DEPLOYMENT_RUNTIME", str(tmp_path))
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: json.dumps([info]))
    if accepted:
        validate("gateway", config, state)
    else:
        with pytest.raises(ValueError, match="Gateway"):
            validate("gateway", config, state)
