import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest
spec = importlib.util.spec_from_file_location('memos_local', Path(__file__).parents[1] / 'skills/memory_plugins/openclaw-memos-local/scripts/run_lifecycle.py')
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
LINE = 'memos.onTurnStart returned hits=0 durationMs=3 contextChars=0 injected=no truncated=no softTimedOut=no'
def test_zero_recall():
    assert a.recall_receipt(LINE)['hits']==0
    assert a.recall_receipt('pending') is None
@pytest.mark.parametrize('text',[LINE+'\n'+LINE,LINE.replace('softTimedOut=no','softTimedOut=yes'),LINE+' memos.onTurnStart.failed',LINE+' memos.onTurnStart.soft_timeout',LINE+' closeSession.flush_failed'])
def test_invalid_recall(text):
    with pytest.raises(RuntimeError):a.recall_receipt(text)
def test_capture_identity(tmp_path):
    p=tmp_path/'db.sqlite'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE api_logs(id INTEGER PRIMARY KEY,tool_name TEXT,input_json TEXT,output_json TEXT,success INTEGER)')
        db.execute('CREATE TABLE embedding_retry_queue(status TEXT)')
        db.executemany('INSERT INTO embedding_retry_queue VALUES (?)',[('succeeded',),('failed',)])
        for i,key in enumerate(['other','agent:main:explicit:x'],1):db.execute('INSERT INTO api_logs VALUES(?,?,?,?,1)',(i,'memory_add',json.dumps({'sessionId':'openclaw::main::'+key}),'{}'))
    assert a.capture_receipt(p,0,'agent:main:explicit:x')['id']==2
    assert a.capture_receipt(p,2,'agent:main:explicit:x') is None
    assert a.db_snapshot(p)['retry_count']==1
    with sqlite3.connect(p) as db:db.execute('UPDATE api_logs SET success=0 WHERE id=2')
    with pytest.raises(RuntimeError):a.capture_receipt(p,0,'agent:main:explicit:x')
def test_session():
    assert a.session_key({'metadata':{'session_id':'Abc'}})=='agent:main:explicit:abc'
    with pytest.raises(ValueError):a.session_key({'metadata':{'agent_id':'other','session_id':'x'}})
def test_delayed_qa_receipt_and_read_only(tmp_path,monkeypatch):
    before={'counts':{'traces':1},'add_id':1,'retry_count':0}
    pending={'nonce':'n','session_key':'agent:main:q','phase':'qa','log_since':1,'before':before}
    a.save(tmp_path/'phase.json',{'owner':str(tmp_path),'scope_id':'s','phase':'qa','pending':pending})
    monkeypatch.setattr(a,'audit_session',lambda *args:{'qa_valid':True})
    logs=iter(['',LINE+' memos.agent_end.skipped'])
    monkeypatch.setattr(a,'container_logs',lambda *args:next(logs))
    monkeypatch.setattr(a,'db_snapshot',lambda *args:before)
    monkeypatch.setattr(a.time,'sleep',lambda n:None)
    request={'action':'after_agent','runtime_context':{'run_dir':str(tmp_path)},'inputs':{'check_context':pending,'agent_result':{'status':'ok','output':{'session_handle':{'session_id':'uuid','gateway_session_key':'agent:main:q'}}}}}
    result=a.dispatch(request,tmp_path,{'container':'test'}, {})
    assert result['output']['checked']
    assert json.loads((tmp_path/'phase.json').read_text())['pending'] is None
    a.save(tmp_path/'phase.json',{'owner':str(tmp_path),'phase':'qa','pending':pending})
    monkeypatch.setattr(a,'container_logs',lambda *args:LINE+' memos.agent_end.skipped')
    monkeypatch.setattr(a,'db_snapshot',lambda *args:{**before,'add_id':2})
    with pytest.raises(RuntimeError,match='Memory write'):a.dispatch(request,tmp_path,{'container':'test'}, {})
    assert json.loads((tmp_path/'phase.json').read_text())['pending']==pending


def test_captured_trace_ownership(tmp_path):
    path = tmp_path / 'memos.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE traces(id TEXT,session_id TEXT)')
        db.execute('INSERT INTO traces VALUES (?,?)', ('trace-1','openclaw::main::agent:main:ingest'))
    capture = {'output': {'details': [{'action': 'stored', 'traceId': 'trace-1'}]}}
    assert a.captured_trace_id(path,capture,'agent:main:ingest') == 'trace-1'
    with pytest.raises(RuntimeError,match='does not belong'):
        a.captured_trace_id(path,capture,'agent:main:other')
    with pytest.raises(RuntimeError,match='exactly one'):
        a.captured_trace_id(path,{'output': {'details': []}},'agent:main:ingest')


def test_prepare_does_not_install_companion(tmp_path, monkeypatch):
    monkeypatch.setattr(a, 'configure', lambda *args: None)
    req = {'action': 'validate', 'runtime_context': {'run_dir': str(tmp_path/'run')}, 'inputs': {}}
    a.dispatch(req,tmp_path,{}, {})
    req.update(action='prepare', inputs={'scope_id':'scope'})
    a.dispatch(req,tmp_path,{}, {})
    assert json.loads((tmp_path/'phase.json').read_text())['trace_ids']==[]
    assert not (tmp_path/'workload-detail-tool').exists()
    assert (tmp_path/'run/artifacts/memory_plugin/openclaw-memos-local/detail-tool-policy.json').exists()


def test_file_receipt_fallback_without_duplicate_recall(monkeypatch):
    from types import SimpleNamespace
    calls=[]
    def execute(args, **kwargs):
        calls.append(args)
        if args[1]=='logs':return SimpleNamespace(stdout=LINE,stderr='',returncode=0)
        assert args[1]=='exec'
        return SimpleNamespace(stdout=LINE+'\nmemos.agent_end.skipped\n',stderr='',returncode=0)
    monkeypatch.setattr(a.subprocess,'run',execute)
    text=a.container_logs('dedicated',123)
    assert a.recall_receipt(text)['hits']==0
    assert 'memos.agent_end.skipped' in text
    assert len(calls)==2


def test_console_receipt_does_not_need_file_fallback(monkeypatch):
    from types import SimpleNamespace
    def execute(args, **kwargs):
        assert args[1]=='logs'
        return SimpleNamespace(stdout=LINE+' memos.agent_end.skipped',stderr='',returncode=0)
    monkeypatch.setattr(a.subprocess,'run',execute)
    assert 'memos.agent_end.skipped' in a.container_logs('dedicated',123)

FILES={a.QA_WORKSPACE+'/history/tr_1.txt':{'trace_id':'tr_1'}}
def call(cid,name,path=None):
    return {'message':{'role':'assistant','content':[{'type':'toolCall','id':cid,'name':name,'arguments':{'path':path} if path else {}}]}}
def result(cid,name,error=False):
    return {'message':{'role':'toolResult','toolCallId':cid,'toolName':name,'isError':error,'details':{'hits':[{'refKind':'trace','refId':'tr_1'}]}}}

@pytest.mark.parametrize('order,valid', [('srSR',True),('SRsr',False),('sSrR',False),('sS',False),('SR',False)])
def test_read_authorization_at_call_time(order,valid):
    events={'s':call('s','memos_search'),'r':result('s','memos_search'),
            'S':call('r','read','history/tr_1.txt'),'R':result('r','read')}
    assert a.audit_reads([events[x] for x in order],FILES)['qa_valid']==valid

@pytest.mark.parametrize('path,error,valid',[
    ('/runtime/outside.txt',True,True),('/runtime/outside.txt',False,False),
    ('history/../../outside.txt',False,False),('history/./tr_1.txt',False,True),
    ('AGENTS.md',False,True),('history/tr_other.txt',False,False)])
def test_path_and_failed_attempts(path,error,valid):
    records=[call('s','memos_search'),result('s','memos_search'),call('r','read',path),result('r','read',error)]
    assert a.audit_reads(records,FILES)['qa_valid']==valid

def test_failed_search_never_authorizes():
    rows=[call('s','memos_search'),result('s','memos_search',True),call('r','read','history/tr_1.txt'),result('r','read')]
    assert not a.audit_reads(rows,FILES)['qa_valid']

def test_corrupt_tool_sequence_rejected():
    with pytest.raises(ValueError):a.audit_reads([result('unknown','read')],FILES)
    c=call('r','read','history/tr_1.txt')
    with pytest.raises(ValueError):a.audit_reads([c,c],FILES)
    with pytest.raises(ValueError):a.audit_reads([c,result('r','memos_search')],FILES)
    with pytest.raises(ValueError):a.audit_reads([c,result('r','read'),result('r','read')],FILES)

def export_fixture(tmp_path,monkeypatch):
    import io
    monkeypatch.setattr(a.urllib.request,'urlopen',lambda *args,**kw:io.BytesIO(json.dumps({'id':'tr_1','userText':'original history'}).encode()))
    state={'owner':str(tmp_path/'run'),'scope_id':'s','trace_ids':['tr_1']}
    a.export_history(tmp_path,{'viewer_url':'http://127.0.0.1:18799'},state)
    return state

def test_native_export_integrity(tmp_path,monkeypatch):
    state=export_fixture(tmp_path,monkeypatch)
    assert a.verify_exports(tmp_path,state)['files'][a.QA_WORKSPACE+'/history/tr_1.txt']['chars']==16
    target=tmp_path/'qa-workspace/history/tr_1.txt'
    assert target.read_text()=='original history'
    target.chmod(0o644)
    with pytest.raises(ValueError):a.verify_exports(tmp_path,state)
    target.chmod(0o444)
    (tmp_path/'qa-workspace/history').chmod(0o755)
    target.unlink();target.symlink_to(tmp_path/'qa-workspace/AGENTS.md')
    with pytest.raises(ValueError):a.verify_exports(tmp_path,state)

def test_real_session_index_and_cross_qa_isolation(tmp_path,monkeypatch):
    state=export_fixture(tmp_path,monkeypatch)
    d=tmp_path/'state/agents/main/sessions';d.mkdir(parents=True)
    a.save(d/'sessions.json',{'agent:main:q':{'sessionId':'uuid'}})
    rows=[call('r','read','history/tr_1.txt'),result('r','read')]
    (d/'uuid.jsonl').write_text('\n'.join(json.dumps(x) for x in rows))
    assert not a.audit_session(tmp_path,state,{'session_key':'agent:main:q'},{'session_id':'uuid'})['qa_valid']
    with pytest.raises(ValueError,match='index mismatch'):
        a.audit_session(tmp_path,state,{'session_key':'agent:main:other'},{'session_id':'uuid'})

def test_invalid_qa_preserves_proof_and_releases_only_qa_lease(tmp_path,monkeypatch):
    before={'counts':{'traces':1},'add_id':1,'retry_count':0}
    pending={'nonce':'n','session_key':'agent:main:q','phase':'qa','log_since':1,'before':before}
    a.save(tmp_path/'phase.json',{'owner':str(tmp_path),'scope_id':'s','phase':'qa','pending':pending})
    monkeypatch.setattr(a,'container_logs',lambda *args:LINE+' memos.agent_end.skipped')
    monkeypatch.setattr(a,'db_snapshot',lambda *args:before)
    monkeypatch.setattr(a,'audit_session',lambda *args:{'qa_valid':False,'unauthorized_successful_reads':1})
    request={'action':'after_agent','runtime_context':{'run_dir':str(tmp_path)},'inputs':{'check_context':pending,'agent_result':{'status':'ok','output':{'session_handle':{'session_id':'uuid','gateway_session_key':'agent:main:q'}}}}}
    response=a.dispatch(request,tmp_path,{'container':'test'}, {})
    assert response['status']=='failed' and response['error']['type']=='InvalidQARead'
    assert response['output']['qa_valid'] is False
    assert json.loads(Path(response['output']['receipt']).read_text())['read_audit']['unauthorized_successful_reads']==1
    assert json.loads((tmp_path/'phase.json').read_text())['pending'] is None

def test_framework_keeps_invalid_qa_answer_and_tokens(tmp_path):
    from memory_bench_platform import token_usage
    from memory_bench_platform.protocol import MemoryPluginTaskOutput, StepRecord, WorkflowRuntimeContext
    from memory_bench_platform.workflow_operators import _execute_agent
    transcript=tmp_path/'session.jsonl'
    step=StepRecord(step_id='qa',case_id='case',name='answer',operator_kind='agent',inputs={
        'messages':[{'role':'user','content':'question'}],
        'metadata':{'session_jsonl':str(transcript),'purpose':'memory_qa'}})
    context=WorkflowRuntimeContext(run_id='r',run_dir=str(tmp_path),benchmark_id='locomo',agent_id='openclaw',
        memory_integration='agent_plugin',memory_plugin_id='openclaw-memos-local',
        run_contract={'memory_plugin_runtime':{'actions':['before_agent','after_agent']}})
    def agent(*args):
        transcript.write_text(json.dumps({'id':'one','message':{'role':'assistant','usage':{
            'input':10,'output':5,'cacheRead':2,'cacheWrite':0}}})+'\n')
        return {'status':'ok','turns':[{'text':'retained original answer'}]}
    def plugin(_id,req):
        if req.action=='before_agent':return MemoryPluginTaskOutput(status='ok',state='completed')
        return MemoryPluginTaskOutput(status='failed',state='failed',output={'qa_valid':False},
                                     error={'type':'InvalidQARead','message':'QA invalid: history read audit failed'})
    with token_usage.scope({'run_dir':str(tmp_path),'run_id':'r','memory_backend':'memos'}):
        output=_execute_agent(step,'openclaw',agent,context,plugin)
    assert output['status']=='failed'
    assert output['agent_answer']=='retained original answer'
    assert output['plugin_checks']['after_agent']['output']['qa_valid'] is False
    assert token_usage.rebuild(tmp_path)['observed_agent_qa_tokens']==17


@pytest.mark.parametrize('console,file_log', [
    ('memos.agent_end.skipped', LINE),
    (LINE, LINE+'\nmemos.agent_end.skipped'),
])
def test_either_missing_console_receipt_reads_native_file(monkeypatch, console, file_log):
    from types import SimpleNamespace
    calls = []
    def execute(args, **kwargs):
        calls.append(args[1])
        return SimpleNamespace(stdout=console if args[1]=='logs' else file_log, stderr='', returncode=0)
    monkeypatch.setattr(a.subprocess, 'run', execute)
    text = a.container_logs('dedicated',123)
    assert calls == ['logs', 'exec']
    assert a.recall_receipt(text)['hits'] == 0
    assert 'memos.agent_end.skipped' in text


def failed_qa_fixture(tmp_path, monkeypatch):
    before = {'counts': {'traces': 1}, 'add_id': 1, 'retry_count': 0}
    pending = {'nonce': 'n', 'session_key': 'agent:main:q1', 'phase': 'qa', 'log_since': 1, 'before': before}
    state = {'owner': str(tmp_path), 'scope_id': 's', 'phase': 'qa', 'pending': pending}
    a.save(tmp_path/'phase.json', state)
    monkeypatch.setattr(a, 'db_snapshot', lambda *args: dict(before))
    monkeypatch.setattr(a, 'verify_exports', lambda *args: {})
    request = {'action': 'after_agent', 'runtime_context': {'run_dir': str(tmp_path)}, 'inputs': {
        'check_context': pending, 'agent_result': {'status': 'failed', 'output': {}}}}
    return request, before


@pytest.mark.parametrize('failure', ['agent', 'recall', 'timeout'])
def test_failed_qa_restarts_before_release_and_next_qa_runs(tmp_path, monkeypatch, failure):
    request, before = failed_qa_fixture(tmp_path, monkeypatch)
    if failure != 'agent':
        request['inputs']['agent_result'] = {'status': 'ok', 'output': {'session_handle': {
            'session_id': 'uuid', 'gateway_session_key': 'agent:main:q1'}}}
        monkeypatch.setattr(a, 'container_logs', lambda *args: 'memos.onTurnStart.soft_timeout')
    restarts = []
    def restart(root, info, config, capture):
        assert capture is False
        assert json.loads((root/'phase.json').read_text())['pending'] is not None
        assert (root/'artifacts/memory_plugin/openclaw-memos-local/n-failure.json').exists()
        restarts.append(True)
    monkeypatch.setattr(a, 'configure', restart)
    info = {'container': 'test', 'completion_timeout_seconds': 0 if failure == 'timeout' else 1}
    with pytest.raises((ValueError, RuntimeError, TimeoutError)):
        a.dispatch(request, tmp_path, info, {})
    assert restarts == [True]
    state = json.loads((tmp_path/'phase.json').read_text())
    assert state['pending'] is None
    proof = json.loads((tmp_path/'artifacts/memory_plugin/openclaw-memos-local/n-failure.json').read_text())
    assert proof['qa_valid'] is False and proof['recovery'] == 'gateway_restarted_read_only'
    req = {'action':'before_agent', 'runtime_context':{'run_dir':str(tmp_path)}, 'inputs':{'agent_request':{
        'metadata':{'scope_id':'s', 'purpose':'memory_qa', 'session_key':'agent:main:q1'}}}}
    with pytest.raises(ValueError, match='Session already exists'):
        a.dispatch(req,tmp_path,info,{})
    req['inputs']['agent_request']['metadata']['session_key'] = 'agent:main:q2'
    assert a.dispatch(req,tmp_path,info,{})['status'] == 'ok'


@pytest.mark.parametrize('failure', ['restart', 'late_write', 'exports'])
def test_failed_qa_cleanup_failure_keeps_pending(tmp_path, monkeypatch, failure):
    request, before = failed_qa_fixture(tmp_path, monkeypatch)
    def restart(*args):
        if failure == 'restart':
            raise TimeoutError('Gateway not ready')
        if failure == 'late_write':
            monkeypatch.setattr(a, 'db_snapshot', lambda *args: {**before, 'add_id': 2})
        if failure == 'exports':
            monkeypatch.setattr(a, 'verify_exports', lambda *args: (_ for _ in ()).throw(ValueError('changed')))
    monkeypatch.setattr(a, 'configure', restart)
    with pytest.raises(ValueError, match='Agent failed'):
        a.dispatch(request,tmp_path,{}, {})
    assert json.loads((tmp_path/'phase.json').read_text())['pending'] is not None
    proof = json.loads((tmp_path/'artifacts/memory_plugin/openclaw-memos-local/n-failure.json').read_text())
    assert proof['recovery'] == 'blocked'


@pytest.mark.parametrize('mismatch', ['owner', 'nonce', 'phase', 'session', 'failed_session'])
def test_failed_qa_cleanup_rejects_unrelated_callbacks(tmp_path, monkeypatch, mismatch):
    request, _ = failed_qa_fixture(tmp_path, monkeypatch)
    if mismatch == 'owner': request['runtime_context']['run_dir'] = str(tmp_path/'other')
    if mismatch == 'nonce': request['inputs']['check_context'] = {}
    if mismatch == 'phase':
        state=json.loads((tmp_path/'phase.json').read_text());state['phase']='ingest';a.save(tmp_path/'phase.json',state)
    if mismatch in ('session', 'failed_session'):
        request['inputs']['agent_result']={'status':'ok' if mismatch == 'session' else 'failed','output':{'session_handle':{'session_id':'uuid','gateway_session_key':'other'}}}
    monkeypatch.setattr(a, 'configure', lambda *args: pytest.fail('must not restart unrelated request'))
    with pytest.raises(ValueError): a.dispatch(request,tmp_path,{}, {})
    assert json.loads((tmp_path/'phase.json').read_text())['pending'] is not None


@pytest.mark.parametrize('agents', [[],
    [{'id':'main','workspace':'/runtime/old-workspace','model':'unchanged'}],
    [{'id':'other','default':True,'workspace':'/runtime/other'}],
])
def test_configure_binds_actual_main_workspace(tmp_path,monkeypatch,agents):
    import io
    import copy
    config={'plugins':{'entries':{a.PLUGIN:{}},'load':{}},
            'agents':{'defaults':{},'list':copy.deepcopy(agents)}}
    def response(*args,**kwargs):
        r=io.BytesIO(json.dumps({'ok':True,'service':a.PLUGIN}).encode());r.status=200;return r
    monkeypatch.setattr(a.subprocess,'run',lambda *args,**kw:None)
    monkeypatch.setattr(a.urllib.request,'urlopen',response)
    a.configure(tmp_path,{'container':'test','gateway_url':'http://mock','viewer_url':'http://mock'},config,False)
    saved=json.loads((tmp_path/'openclaw.json').read_text())
    assert saved['agents']['defaults']['workspace']==a.QA_WORKSPACE
    main=next(x for x in saved['agents']['list'] if x['id']=='main')
    assert main['workspace']==a.QA_WORKSPACE
    if agents and agents[0]['id']=='other':assert saved['agents']['list'][0]==agents[0]
    if agents and agents[0]['id']=='main':assert main['model']=='unchanged'


def test_real_http_error_runs_cleanup_and_next_qa_preserves_usage(tmp_path,monkeypatch):
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from memory_bench_platform import token_usage
    from memory_bench_platform.integration import run_agent_task
    from memory_bench_platform.protocol import MemoryPluginTaskOutput, StepRecord, WorkflowRuntimeContext
    from memory_bench_platform.workflow_operators import _execute_agent
    transcript=tmp_path/'failed-session.jsonl'
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            transcript.write_text(json.dumps({'id':'failed-msg','message':{'role':'assistant','usage':{
                'input':10,'output':5,'cacheRead':2,'cacheWrite':0}}})+'\n')
            self.send_response(502);self.end_headers();self.wfile.write(b'{"error":"test upstream unavailable"}')
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    a.save(tmp_path/'phase.json',{'owner':str(tmp_path),'scope_id':'s','phase':'qa','pending':None})
    monkeypatch.setattr(a,'load_runtime',lambda:(tmp_path,{'container':'test'},{}))
    monkeypatch.setattr(a,'db_snapshot',lambda *args:{'counts':{'traces':1},'add_id':1,'retry_count':0})
    monkeypatch.setattr(a,'verify_exports',lambda *args:{})
    restarted=[]
    monkeypatch.setattr(a,'configure',lambda *args:restarted.append(args[-1]))
    monkeypatch.setattr(a,'container_logs',lambda *args:LINE+' memos.agent_end.skipped')
    monkeypatch.setattr(a,'audit_session',lambda *args:{'qa_valid':True})
    monkeypatch.setenv('OPENCLAW_TRANSPORT','http')
    monkeypatch.setenv('OPENCLAW_GATEWAY_URL',f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('OPENCLAW_GATEWAY_TOKEN','test-only')
    actions=[]
    def plugin(_id, req):
        actions.append(req.action)
        try:result=a.run(req.model_dump(mode='json'))
        except Exception as exc:result={'status':'failed','state':'failed','error':{'type':type(exc).__name__,'message':str(exc)}}
        return MemoryPluginTaskOutput.model_validate(result)
    ctx=WorkflowRuntimeContext(run_id='r',run_dir=str(tmp_path),benchmark_id='locomo',agent_id='openclaw',
        memory_integration='agent_plugin',memory_plugin_id='openclaw-memos-local',
        run_contract={'memory_plugin_runtime':{'actions':['before_agent','after_agent']}})
    def qa(key):return StepRecord(step_id=key,case_id=key,name='answer',operator_kind='agent',inputs={
        'messages':[{'role':'user','content':'test'}],'metadata':{'agent_id':'main','scope_id':'s',
        'purpose':'memory_qa','session_key':'agent:main:'+key,'session_jsonl':str(transcript)}})
    try:
        with token_usage.scope({'run_dir':str(tmp_path),'run_id':'r','memory_backend':'memos'}):
            with pytest.raises(subprocess.CalledProcessError):
                _execute_agent(qa('q1'),'openclaw',run_agent_task,ctx,plugin)
        assert actions==['before_agent','after_agent'] and restarted==[False]
        assert json.loads((tmp_path/'phase.json').read_text())['pending'] is None
        assert token_usage.rebuild(tmp_path)['observed_agent_qa_tokens']==17
        calls=[]
        def answer(*args):
            calls.append(True)
            return {'status':'ok','turns':[{'text':'answer'}],'output':{'session_handle':{
                'session_id':'uuid2','gateway_session_key':'agent:main:q2'}}}
        assert _execute_agent(qa('q2'),'openclaw',answer,ctx,plugin)['status']=='ok'
        assert calls==[True]
        failures=list((tmp_path/'artifacts/memory_plugin/openclaw-memos-local').glob('*-failure.json'))
        assert len(failures)==1 and json.loads(failures[0].read_text())['qa_valid'] is False
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)
