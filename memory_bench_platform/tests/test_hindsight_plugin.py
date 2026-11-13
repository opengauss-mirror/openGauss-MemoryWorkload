import importlib.util
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('hindsight_plugin',Path(__file__).parents[1]/'skills/memory_plugins/openclaw-hindsight/scripts/run_lifecycle.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.mark.parametrize('count',[0,3])
def test_native_recall(count):
    text=f'perf: before_prompt_build hook_total=30ms recall_main=20ms source=fresh results={count}'
    if count:text=f'injecting {count} memories into context (bank: bank1)\n'+text
    assert m.native_receipt(text,'qa','bank1')['recalled_memories']==count

@pytest.mark.parametrize('text',[
 'Auto-recall timed out after 10000ms',
 'perf: agent_end hook_total=1ms retain=1ms outcome=queued bank=bank1 messages=2',
 'perf: agent_end hook_total=1ms retain=1ms outcome=ok bank=other messages=2',
 'perf: before_prompt_build results=0\nperf: before_prompt_build results=0',
 'injecting 1 memories into context (bank: other)\nperf: before_prompt_build results=1',
])
def test_reject_failed_foreign_duplicate(text):
    with pytest.raises(RuntimeError):m.native_receipt(text,'ingest' if 'agent_end' in text else 'qa','bank1')

def test_retain_and_missing():
    assert m.native_receipt('perf: agent_end hook_total=2ms retain=1ms outcome=ok bank=bank1 messages=2','ingest','bank1')=={'retained_messages':2}
    assert m.native_receipt('HTTP 200','ingest','bank1') is None

@pytest.mark.parametrize('state',['failed','cancelled'])
def test_background_failure(state):
    with pytest.raises(RuntimeError):m.completed([{'status':state}])

def test_background_pending():
    assert not m.completed([{'status':'processing'}])
    assert m.completed([{'status':'completed'}])

def test_no_redirect():
    assert m.NoRedirect().redirect_request(None,None,302,'',{},'https://other') is None

def test_native_operation_list_id_alias_and_pagination(monkeypatch):
    import io,json
    replies=[{'bank_id':'bank1','operations':[{'id':str(i),'status':'completed'} for i in range(100)]},
             {'bank_id':'bank1','operations':[{'id':'last','status':'processing'}]}]
    calls=[]
    class Opener:
        def open(self,req,timeout):
            calls.append(req.full_url)
            return io.BytesIO(json.dumps(replies.pop(0)).encode())
    monkeypatch.setattr(m.urllib.request,'build_opener',lambda *args:Opener())
    cfg={'plugins':{'entries':{m.PLUGIN:{'config':{'hindsightApiUrl':'http://localhost:8888'}}}}}
    rows=m.operations(cfg,'bank1')
    assert rows[-1]['operation_id']=='last' and len(rows)==101
    assert 'offset=100' in calls[-1] and not m.completed(rows)


def test_operation_list_rejects_foreign_bank(monkeypatch):
    import io
    class Opener:
        def open(self,*args,**kwargs):return io.BytesIO(b'{"bank_id":"other","operations":[]}')
    monkeypatch.setattr(m.urllib.request,'build_opener',lambda *args:Opener())
    cfg={'plugins':{'entries':{m.PLUGIN:{'config':{'hindsightApiUrl':'http://localhost:8888'}}}}}
    with pytest.raises(ValueError):m.operations(cfg,'bank1')


@pytest.fixture
def dedicated_gateway(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    package = tmp_path / 'package'
    (package / 'dist').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({
        'version': m.VERSION, 'openclaw': {'extensions': ['./dist/index.js']},
    }))
    (package / 'openclaw.plugin.json').write_text('{}')
    (package / 'dist/index.js').write_text('export {};')
    (package / 'dist/helper.js').write_text('export const helper = 1;')
    provenance = {'package_path': str(package), 'files': {
        f.relative_to(package).as_posix(): m.digest(f)
        for f in package.rglob('*') if f.is_file()
    }}
    m.save(runtime / 'provenance.json', provenance)
    config = {'gateway': {'port': 18789}, 'tools': {'deny': ['*']}, 'plugins': {
        'allow': [m.PLUGIN], 'load': {'paths': ['/official']},
        'entries': {m.PLUGIN: {'hooks': {'allowConversationAccess': True, 'allowPromptInjection': True},
                             'config': {'memoryMode': 'context', 'retainEveryNTurns': 1, 'debugPerfTiming': True}}},
    }}
    m.save(runtime / 'openclaw.json', config)
    m.save(runtime / 'phase.json', {'active': False})
    data = {
        'Config': {'Labels': {'memory-bench.runtime': str(runtime)}, 'Env': [
            'OPENCLAW_CONFIG_PATH=/runtime/openclaw.json', 'OPENCLAW_STATE_DIR=/runtime/state',
        ]},
        'Mounts': [{'Source': str(runtime), 'Destination': '/runtime'},
                   {'Source': str(package), 'Destination': '/official', 'Type': 'bind', 'RW': False}],
        'HostConfig': {'NetworkMode': 'bridge'},
        'NetworkSettings': {'Ports': {'18789/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '28789'}]}},
    }
    calls = []

    def docker(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps([data]) if command[1] == 'inspect' else '')

    monkeypatch.setattr(m.subprocess, 'run', docker)
    monkeypatch.setenv('HINDSIGHT_PLUGIN_RUNTIME', str(runtime))
    monkeypatch.setenv('HINDSIGHT_GATEWAY_CONTAINER', 'dedicated')
    monkeypatch.setenv('OPENCLAW_TRANSPORT', 'http')
    monkeypatch.setenv('OPENCLAW_AGENT_ID', 'main')
    monkeypatch.setenv('OPENCLAW_GATEWAY_URL', 'http://127.0.0.1:28789')
    monkeypatch.delenv('OPENCLAW_GATEWAY_BASE_URL', raising=False)
    monkeypatch.delenv('OPENCLAW_GATEWAY_PORT', raising=False)
    return runtime, config, data, provenance, calls


@pytest.mark.parametrize('mode,env_var,port', [
    ('bridge', 'OPENCLAW_GATEWAY_URL', 28789),
    ('bridge', 'OPENCLAW_GATEWAY_BASE_URL', 28789),
    ('host', 'OPENCLAW_GATEWAY_PORT', 18789),
])
def test_health_uses_agent_endpoint(dedicated_gateway, monkeypatch, mode, env_var, port):
    from types import SimpleNamespace
    runtime, config, data, _, calls = dedicated_gateway
    data['HostConfig']['NetworkMode'] = mode
    monkeypatch.delenv('OPENCLAW_GATEWAY_URL')
    url = f'http://127.0.0.1:{port}'
    monkeypatch.setenv(env_var, str(port) if env_var.endswith('_PORT') else url + '/')
    checked = []

    class Health:
        def open(self, url, timeout):
            checked.append(url)
            from contextlib import nullcontext
            return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda *a: Health())
    m.configure(runtime / 'phase.json', config, {}, 'ingest')
    assert checked == [url + '/health']
    assert calls == [['docker', 'inspect', 'dedicated'], ['docker', 'restart', 'dedicated']]


def test_wrong_gateway_is_rejected_before_agent(dedicated_gateway, monkeypatch):
    import json
    runtime, _, _, _, calls = dedicated_gateway
    phase = runtime / 'phase.json'
    m.save(phase, {'active': True, 'owner': 'run', 'scope_id': 'scope', 'phase': 'ingest',
                   'gateway_ready': True, 'config_sha256': m.digest(runtime / 'openclaw.json')})
    monkeypatch.setenv('OPENCLAW_GATEWAY_URL', 'http://127.0.0.1:18789')
    with pytest.raises(ValueError, match='does not match'):
        m.run({'action': 'before_agent', 'runtime_context': {'run_dir': 'run'},
               'inputs': {'agent_request': {'metadata': {'purpose': 'memory_ingest', 'scope_id': 'scope'}}}})
    assert calls == [['docker', 'inspect', 'dedicated']]
    assert 'pending' not in json.loads(phase.read_text())


@pytest.mark.parametrize('fault', [None, 'wrong_source', 'writable', 'load_path', 'nested_mount',
                                  'empty_hashes', 'missing_dependency', 'changed_file'])
def test_validation_binds_loaded_plugin_and_hashes(dedicated_gateway, fault):
    runtime, config, data, provenance, _ = dedicated_gateway
    official = data['Mounts'][1]
    if fault == 'wrong_source':
        official['Source'] = str(runtime)
    elif fault == 'writable':
        official['RW'] = True
    elif fault == 'load_path':
        config['plugins']['load']['paths'] = ['/other']
        m.save(runtime / 'openclaw.json', config)
    elif fault == 'nested_mount':
        data['Mounts'].append({'Destination': '/official/dist'})
    elif fault == 'empty_hashes':
        provenance['files'].clear()
    elif fault == 'missing_dependency':
        del provenance['files']['dist/helper.js']
    elif fault == 'changed_file':
        (Path(provenance['package_path']) / 'dist/index.js').write_text('export const changed = 1;')
    m.save(runtime / 'provenance.json', provenance)
    request = {'action': 'validate', 'runtime_context': {'run_dir': 'run'}}
    if fault is None:
        assert m.run(request)['output']['version'] == m.VERSION
    else:
        with pytest.raises(ValueError):
            m.run(request)


@pytest.mark.parametrize('metadata_agent,env_agent,allowed', [
    ('other', 'main', False),
    (None, 'other', False),
    ('main', 'other', True),
    (None, 'main', True),
    (None, None, False),
])
def test_before_agent_validates_selected_agent(dedicated_gateway, monkeypatch, metadata_agent, env_agent, allowed):
    import json

    runtime, _, _, _, calls = dedicated_gateway
    phase = runtime / 'phase.json'
    m.save(phase, {'active': True, 'owner': 'run', 'scope_id': 'scope', 'phase': 'ingest',
                   'gateway_ready': True, 'config_sha256': m.digest(runtime / 'openclaw.json'),
                   'bank_id': 'bank1'})
    if env_agent is None:
        monkeypatch.delenv('OPENCLAW_AGENT_ID')
    else:
        monkeypatch.setenv('OPENCLAW_AGENT_ID', env_agent)
    monkeypatch.setattr(m, 'operations', lambda *args, **kwargs: [])
    metadata = {'purpose': 'memory_ingest', 'scope_id': 'scope', 'session_key': 'history'}
    if metadata_agent is not None:
        metadata['agent_id'] = metadata_agent
    request = {'action': 'before_agent', 'runtime_context': {'run_dir': 'run'},
               'inputs': {'agent_request': {'metadata': metadata}}}
    if allowed:
        assert m.run(request)['output']['session_key'] == 'agent:main:history'
        assert 'pending' in json.loads(phase.read_text())
    else:
        with pytest.raises(ValueError, match='Only main agent supported'):
            m.run(request)
        assert 'pending' not in json.loads(phase.read_text())
        assert not calls
