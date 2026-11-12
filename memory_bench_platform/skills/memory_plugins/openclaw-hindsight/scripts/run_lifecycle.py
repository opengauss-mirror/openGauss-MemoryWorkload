"""Official Hindsight OpenClaw 1.2.0. Dedicated serial Gateway, no upstream patches."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

PLUGIN = 'hindsight-openclaw'
VERSION = '1.2.0'


def save(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    with open(temp, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as out:
        os.chmod(temp, 0o600)
        json.dump(value, out, indent=2)
    temp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def operations(config, bank, missing=False):
    cfg = config['plugins']['entries'][PLUGIN]['config']
    headers = {}
    if cfg.get('hindsightApiToken'):
        if not isinstance(cfg['hindsightApiToken'], str):
            raise ValueError('This adapter requires an already resolved API token')
        headers['Authorization'] = 'Bearer ' + cfg['hindsightApiToken']
    rows, offset = [], 0
    while True:
        url = cfg['hindsightApiUrl'].rstrip('/') + '/v1/default/banks/' + bank + '/operations?limit=100&offset=' + str(offset)
        try:
            with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url, headers=headers), timeout=20) as response:
                page = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing and offset == 0:
                return []
            raise
        batch = page.get('operations')
        if page.get('bank_id') != bank or not isinstance(batch, list):
            raise ValueError('Invalid native bank operations response')
        for row in batch:
            if isinstance(row, dict):
                if row.get('operation_id') and row.get('id') and row['operation_id'] != row['id']:
                    raise ValueError('Operation identity mismatch')
                row['operation_id'] = row.get('operation_id') or row.get('id')
            if not isinstance(row, dict) or not row.get('operation_id') or row.get('status') not in ('pending','processing','completed','failed','cancelled'):
                raise ValueError('Invalid native operation')
        rows.extend(batch)
        if len(batch) < 100:
            return rows
        offset += len(batch)


def completed(rows):
    if any(x['status'] in ('failed', 'cancelled') for x in rows):
        raise RuntimeError('Native background operation failed/cancelled')
    return all(x['status'] == 'completed' for x in rows)


def native_receipt(text, phase, bank):
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    failures = ('Auto-recall timed out', 'error recalling', 'auto-recall failed', 'auto-recall error', 'Auto-recall aborted',
                'error retaining messages', 'retain queued', 'outcome=error', 'outcome=queued')
    if any(marker in text for marker in failures):
        raise RuntimeError('Native Hindsight hook failed or queued; see archived logs')
    if phase == 'ingest':
        rows = re.findall(r'perf: agent_end [^\n]*outcome=(\w+) bank=([^\s]+) messages=(\d+)', text)
        if len(rows) > 1:
            raise RuntimeError('Multiple retain receipts')
        if not rows:
            return None
        outcome, actual, count = rows[0]
        if outcome != 'ok' or actual != bank or int(count) == 0:
            raise RuntimeError('Invalid retain receipt')
        return {'retained_messages': int(count)}
    rows = re.findall(r'perf: before_prompt_build [^\n]*results=(\d+)', text)
    if len(rows) > 1:
        raise RuntimeError('Multiple recall receipts')
    if not rows:
        return None
    count = int(rows[0])
    injected = re.findall(r'injecting (\d+) memories into context \(bank: ([^)]+)\)', text)
    if count and injected != [(str(count), bank)]:
        raise RuntimeError('Recall injection receipt mismatch')
    if not count and injected:
        raise RuntimeError('Conflicting zero recall receipt')
    return {'recalled_memories': count}


def gateway_url(config, inspect):
    runner = Path(__file__).resolve().parents[3] / 'agents/openclaw/scripts/run_task.py'
    url = runpy.run_path(str(runner))['_resolve_gateway_url']()
    endpoint = urlsplit(url)
    host = endpoint.hostname
    if (endpoint.scheme != 'http' or host not in ('127.0.0.1', '::1')
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.path or endpoint.query or endpoint.fragment):
        raise ValueError('Gateway URL must be a dedicated loopback HTTP endpoint')
    port = endpoint.port if endpoint.port is not None else 80
    container_port = int(config['gateway']['port'])
    if inspect['HostConfig']['NetworkMode'] == 'host':
        matches = port == container_port
    else:
        ports = inspect['NetworkSettings'].get('Ports') or {}
        wildcard = '0.0.0.0' if host == '127.0.0.1' else '::'
        matches = any(
            binding.get('HostIp') in (host, wildcard) and binding.get('HostPort') == str(port)
            for binding in ports.get(f'{container_port}/tcp') or []
        )
    if not matches:
        raise ValueError('Agent Gateway URL does not match the dedicated container endpoint')
    return url


def gateway(path, config):
    name = os.environ.get('HINDSIGHT_GATEWAY_CONTAINER', '')
    if not name:
        raise ValueError('HINDSIGHT_GATEWAY_CONTAINER is required')
    data = json.loads(subprocess.run(['docker','inspect',name], check=True,capture_output=True,text=True,timeout=15).stdout)[0]
    if (data['Config'].get('Labels') or {}).get('memory-bench.runtime') != str(path.parent):
        raise ValueError('Dedicated Gateway ownership mismatch')
    if not any(Path(m['Source']).resolve() == path.parent and m['Destination']=='/runtime' for m in data['Mounts']):
        raise ValueError('Gateway runtime mount mismatch')
    env = dict(x.split('=',1) for x in data['Config'].get('Env',[]) if '=' in x)
    if env.get('OPENCLAW_CONFIG_PATH')!='/runtime/openclaw.json' or env.get('OPENCLAW_STATE_DIR')!='/runtime/state':
        raise ValueError('Gateway config/state mismatch')
    provenance = json.loads(path.with_name('provenance.json').read_text())
    upstream = Path(provenance['package_path']).resolve(strict=True)
    official = [mount for mount in data['Mounts'] if mount['Destination'] == '/official']
    if (config['plugins'].get('load', {}).get('paths') != ['/official']
            or len(official) != 1 or official[0].get('Type') != 'bind'
            or official[0].get('RW') is not False
            or Path(official[0]['Source']).resolve() != upstream
            or any(mount['Destination'].startswith('/official/') for mount in data['Mounts'])):
        raise ValueError('Official plugin load path must match the verified read-only mount')
    return name, gateway_url(config, data), upstream, provenance


def validate_package(upstream, provenance):
    package = json.loads((upstream / 'package.json').read_text())
    if package['version'] != VERSION:
        raise ValueError('Unsupported official plugin version')
    files = provenance['files']
    required = {'package.json', 'openclaw.plugin.json'}
    scripts = {path.relative_to(upstream).as_posix() for path in (upstream / 'dist').rglob('*.js')}
    extensions = package.get('openclaw', {}).get('extensions') or []
    entries = {Path(path).as_posix() for path in extensions}
    if not entries or not entries <= scripts or not isinstance(files, dict) or not (required | scripts) <= files.keys():
        raise ValueError('Plugin provenance must cover the manifests and runtime JavaScript')
    for relative, expected in files.items():
        target = (upstream / relative).resolve(strict=True)
        if not target.is_relative_to(upstream) or digest(target) != expected:
            raise ValueError('Official plugin changed')


def configure(path, config, state, phase):
    name, url, _, _ = gateway(path, config)
    entry = config['plugins']['entries'][PLUGIN]['config']
    entry.update(bankId=state.get('bank_id','mbp-inactive'),dynamicBankId=False,
                 autoRetain=phase=='ingest',autoRecall=phase=='qa')
    save(path.with_name('openclaw.json'),config)
    save(path,{**state,'phase':phase,'gateway_ready':False})
    subprocess.run(['docker','restart',name],check=True,capture_output=True,timeout=90)
    deadline = time.monotonic()+60
    opener = urllib.request.build_opener(NoRedirect())
    while time.monotonic()<deadline:
        try:
            with opener.open(url + '/health', timeout=2) as r:
                if r.status==200:
                    state.update(phase=phase,gateway_ready=True,config_sha256=digest(path.with_name('openclaw.json')))
                    save(path,state)
                    return
        except (OSError,urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError('Gateway did not become healthy')


def run(request):
    path = Path(os.environ['HINDSIGHT_PLUGIN_RUNTIME']).resolve()/'phase.json'
    owner = str(request.get('runtime_context',{}).get('run_dir') or '')
    if not owner or os.environ.get('OPENCLAW_TRANSPORT')!='http':
        raise ValueError('Dedicated HTTP runtime/run_dir required')
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=json.loads(path.read_text());config=json.loads(path.with_name('openclaw.json').read_text())
        if state.get('active') and state.get('owner')!=owner:
            raise ValueError('Runtime belongs to another run')
        action=request.get('action');inputs=request.get('inputs',{});output={};artifacts=[]
        if action=='validate':
            _, _, upstream, provenance = gateway(path, config)
            validate_package(upstream, provenance)
            entry=config['plugins']['entries'][PLUGIN]
            if any(entry.get('hooks',{}).get(k) is not True for k in ('allowConversationAccess','allowPromptInjection')):
                raise ValueError('Official conversation/prompt hooks must be enabled')
            cfg=entry['config']
            if cfg.get('memoryMode')!='context' or cfg.get('retainEveryNTurns')!=1 or cfg.get('debugPerfTiming') is not True:
                raise ValueError('Native context mode/every-turn retain/perf receipts required')
            if config.get('tools',{}).get('deny')!=['*'] or config['plugins'].get('allow')!=[PLUGIN]:
                raise ValueError('Exclusive automatic-memory runtime required')
            output={'plugin':PLUGIN,'version':VERSION}
        elif action=='prepare':
            scope=inputs.get('scope_id')
            if not scope or state.get('pending'):
                raise ValueError('Fresh scope with no pending request required')
            bank='mbp-'+hashlib.sha256((owner+':'+scope).encode()).hexdigest()[:32]
            if operations(config,bank,missing=True):
                raise ValueError('Bank already used')
            state={'active':True,'owner':owner,'scope_id':scope,'bank_id':bank}
            configure(path,config,state,'ingest');output={'bank_id':bank,'scope_id':scope}
        elif action in ('set_phase','finalize'):
            if state.get('owner')!=owner or state.get('pending'):
                raise ValueError('Cannot switch phase with foreign/unfinished request')
            phase='inactive' if action=='finalize' else inputs.get('phase')
            if phase not in ('ingest','qa','inactive'):
                raise ValueError('Invalid phase')
            if phase!=state.get('phase'):
                configure(path,config,state,phase)
            if action=='finalize':
                state['active']=False
            output={'phase':phase}
        else:
            if not state.get('active'):
                raise ValueError('Prepare first')
            if action=='before_agent':
                req=inputs['agent_request'];meta=req.get('metadata',{})
                phase='ingest' if meta.get('purpose')=='memory_ingest' else 'qa'
                if state.get('pending') or meta.get('scope_id')!=state['scope_id'] or phase!=state['phase']:
                    raise ValueError('Concurrent/replayed or mismatched request')
                if not state.get('gateway_ready') or digest(path.with_name('openclaw.json'))!=state.get('config_sha256'):
                    raise ValueError('Runtime config drift')
                agent_id = meta.get('agent_id') or os.environ.get('OPENCLAW_AGENT_ID')
                if agent_id != 'main':
                    raise ValueError('Only main agent supported')
                gateway(path, config)
                key=str(meta.get('session_key') or '').lower()
                if not key:raise ValueError('Explicit session key required')
                if not key.startswith('agent:'):key='agent:main:'+key
                if not key.startswith('agent:main:'):raise ValueError('Only main agent supported')
                store=path.parent/'state/agents/main/sessions/sessions.json'
                if store.exists() and key in json.loads(store.read_text()):raise ValueError('Session already exists')
                rows=operations(config,state['bank_id'],missing=True)
                if not completed(rows):raise ValueError('Bank still processing')
                state['pending']={'nonce':secrets.token_hex(16),'session_key':key,'phase':phase,'bank_id':state['bank_id'],'since':str(time.time()),'operation_ids':[x['operation_id'] for x in rows]}
                output=state['pending'].copy()
            elif action=='after_agent':
                pending=state.get('pending')
                if not pending or pending!=inputs.get('check_context'):raise ValueError('Receipt identity mismatch')
                result=inputs['agent_result'];handle=result.get('output',{}).get('session_handle',{})
                if result.get('status')!='ok' or handle.get('gateway_session_key')!=pending['session_key'] or not handle.get('session_id'):
                    raise ValueError('Agent call/session failed')
                name, _, _, _ = gateway(path, config);deadline=time.monotonic()+(600 if pending['phase']=='ingest' else 5)
                while time.monotonic()<deadline:
                    log=subprocess.run(['docker','logs','--since',pending['since'],name],capture_output=True,text=True,check=True,timeout=15)
                    text=log.stdout+'\n'+log.stderr
                    proof=native_receipt(text,pending['phase'],pending['bank_id'])
                    rows=operations(config,state['bank_id'],missing=True)
                    new=[x for x in rows if x['operation_id'] not in pending['operation_ids']]
                    if pending['phase']=='qa' and new:raise RuntimeError('QA created memory operations')
                    if proof and completed(rows) and (pending['phase']=='qa' or new):break
                    time.sleep(1)
                else:raise RuntimeError('Native hook or background completion not verified')
                receipt=Path(owner)/'artifacts/memory_plugin/openclaw-hindsight'
                receipt.mkdir(parents=True,exist_ok=True)
                target=receipt/(pending['nonce']+'.json')
                save(target,{**pending,**proof,'session_id':handle['session_id'],'native_operations':rows,'verification':'exclusive_serial_gateway_window_and_native_bank_operations'})
                output={'checked':True,**proof,'receipt':str(target)}
                artifacts=[{'kind':'memory_plugin_receipt','path':str(target)}]
                state['pending']=None
            else:raise ValueError('Unsupported action')
        save(path,state)
        return {'status':'ok','state':'completed','operation':{},'output':output,'error':{},'artifacts':artifacts,'metrics':[]}


if __name__=='__main__':
    try:result=run(json.load(sys.stdin))
    except Exception as exc:
        result={'status':'failed','state':'failed','operation':{},'output':{},'error':{'code':type(exc).__name__,'message':str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__},'artifacts':[],'metrics':[]}
    json.dump(result,sys.stdout)
