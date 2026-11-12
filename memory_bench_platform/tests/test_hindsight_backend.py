import importlib.util
import io
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hindsight_runner', ROOT/'skills/memories/hindsight/scripts/run_operation.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def invoke(action, inputs, replies, env=None):
    calls = []
    def opener(req, timeout):
        calls.append(req)
        return io.BytesIO(json.dumps(dict(replies[len(calls)-1], **({'bank_id':runner.identifier('run:sample'), 'success':True} if action=='ingest' else {'bank_id':runner.identifier('run:sample'), 'operation_id':'native-task'} if action=='status' else {}))).encode())
    result = runner.run_operation({'action': action, 'task_id': 'task', 'inputs': {'scope_id': 'run:sample', **inputs}}, environ=env or {}, urlopen=opener)
    return result, calls


def op():
    return {'scope_id': 'run:sample', 'bank_id': runner.identifier('run:sample'), 'task_id': 'native-task', 'session_id': 'doc'}


def test_ingest_preserves_content_timestamp_and_native_id():
    result, calls = invoke('ingest', {'content': 'Alice: I live in Paris.', 'event_id': 'session1', 'timestamp': '1:56 pm on 8 May, 2023'}, [{'async': True, 'operation_id': 'native-task'}], {'HINDSIGHT_API_KEY': 'secret'})
    assert result['state'] == 'accepted'
    body = json.loads(calls[0].data)
    assert body['items'][0]['content'] == 'Alice: I live in Paris.'
    assert body['items'][0]['timestamp'] == '2023-05-08T13:56:00+00:00'
    assert result['operation']['task_id'] == 'native-task'
    assert calls[0].get_header('Authorization') == 'Bearer secret'


@pytest.mark.parametrize('state,expected', [('pending','running'),('processing','running'),('failed','failed'),('cancelled','failed')])
def test_native_task_state(state, expected):
    result, calls = invoke('status', {'operation': op()}, [{'status': state}])
    assert result['state'] == expected and len(calls) == 1


@pytest.mark.parametrize('state,expected', [('processing','running'),('completed','completed'),('failed','failed'),('cancelled','failed')])
def test_waits_for_consolidation(state, expected):
    result, _ = invoke('status', {'operation': op()}, [{'status':'completed'}, {'operations':[{'status':state,'task_type':'consolidation'}]}])
    assert result['state'] == expected


def test_operation_list_pagination():
    result, calls = invoke('status', {'operation':op()}, [{'status':'completed'}, {'operations':[{'status':'completed'}]*100}, {'operations':[{'status':'processing'}]}])
    assert result['state'] == 'running' and 'offset=100' in calls[-1].full_url


def test_scope_mismatch_rejected_before_http():
    result, calls = invoke('status', {'operation':{**op(),'bank_id':'other'}}, [])
    assert result['state'] == 'failed' and not calls


@pytest.mark.parametrize('reply', [{}, {'async':False}, {'async':True,'operation_id':None}])
def test_invalid_retain_not_success(reply):
    result, _ = invoke('ingest', {'content':'history'}, [reply])
    assert result['state'] == 'failed'


def test_empty_recall_and_ranked_limit():
    result, _ = invoke('recall', {'query':'Where?'}, [{'results':[]}])
    assert result['state']=='completed' and result['output']['evidence_text']==''
    result, calls = invoke('recall', {'query':'Where?','node_limit':1}, [{'results':[{'text':'Paris'},{'text':'London'}]}])
    assert result['output']['evidence_text']=='Paris'
    assert json.loads(calls[0].data)['max_tokens']==4096


@pytest.mark.parametrize('reply', [{}, {'results':None}, {'results':[{}]}, {'results':[{'text':''}]}])
def test_malformed_recall_is_failure(reply):
    result, _ = invoke('recall', {'query':'Where?'}, [reply])
    assert result['state']=='failed'


@pytest.mark.parametrize('limit', [0, -1, None, True, 1.5, 1001])
def test_invalid_limit_no_request(limit):
    result, calls = invoke('recall', {'query':'Where?', 'node_limit':limit}, [])
    assert result['state']=='failed' and not calls


def test_no_redirect_or_write_retry():
    assert runner.NoRedirect().redirect_request(None,None,302,'',{},'https://other') is None
    calls=[]
    def fail(*a, **kw):
        calls.append(1)
        raise RuntimeError('secret')
    result=runner.run_operation({'action':'ingest','task_id':'x','inputs':{'scope_id':'a','content':'history'}},environ={},urlopen=fail)
    assert len(calls)==1 and 'secret' not in json.dumps(result)


def test_framework_composes_ingest_wait_without_flush():
    from memory_bench_platform.benchmark_scenario import BenchmarkScenario, RunBinding
    from memory_bench_platform.composer import compose_run_plan
    from memory_bench_platform.integration import get_memory_manifest
    scenario=BenchmarkScenario.model_validate(json.loads((ROOT/'tests/golden/multi_checkpoint_scenario.json').read_text()))
    plan=compose_run_plan(scenario,RunBinding(benchmark_id=scenario.benchmark_id,agent_id='openclaw',memory_id='hindsight',memory_integration='backend_direct',run_id='test'),{'memory':get_memory_manifest('hindsight').capabilities})
    setup=[s for s in plan['steps'] if s['case_id'].endswith('stage-1-setup')]
    assert [s['operator_kind'] for s in setup]==['memory','poll']
    assert all(s['depends_on']==[setup[i-1]['step_id']] for i,s in enumerate(setup) if i)


def test_evidence_uses_real_paragraph_separator():
    result, _ = invoke('recall', {'query':'Where?'}, [{'results':[{'text':'Paris'},{'text':'London'}]}])
    assert result['output']['evidence_text']=='Paris\n\nLondon'
