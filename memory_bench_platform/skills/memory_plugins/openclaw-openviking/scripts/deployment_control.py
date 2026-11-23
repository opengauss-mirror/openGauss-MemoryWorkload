"""Docker binding for the existing OpenViking native lifecycle (no memory implementation)."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import time
import urllib.request
C = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/deployment_openclaw.py'))


def directory():
    root = Path(os.environ['OPENVIKING_DEPLOYMENT_RUNTIME']).resolve()
    if Path(os.environ['OPENCLAW_CONFIG_PATH']).resolve() != root / 'openclaw.json':
        raise ValueError('OpenViking runtime config mismatch')
    return root


def config_action(action, key, value=None):
    path = directory() / 'openclaw.json'
    config = json.loads(path.read_text())
    owner = config
    parts = key.split('.')
    for part in parts[:-1]:
        owner = owner[part]
    if action == 'get':
        if parts[-1] not in owner:
            raise RuntimeError('Config path not found')
        return owner[parts[-1]]
    if action == 'set':
        owner[parts[-1]] = value
    else:
        owner.pop(parts[-1], None)
    C['save'](path, config)


def gateway():
    root = directory()
    name = os.environ['OPENVIKING_GATEWAY_CONTAINER']
    info = json.loads(subprocess.check_output(['docker','inspect',name],text=True,timeout=15))[0]
    env = dict(v.split('=',1) for v in info['Config'].get('Env',[]) if '=' in v)
    if ((info['Config'].get('Labels') or {}).get('memory-bench.runtime') != str(root)
            or env.get('OPENCLAW_CONFIG_PATH') != '/runtime/openclaw.json'
            or env.get('OPENCLAW_STATE_DIR') != '/runtime/state'
            or not any(Path(m['Source']).resolve() == root and m['Destination']=='/runtime' for m in info['Mounts'])):
        raise ValueError('Dedicated OpenViking Gateway ownership mismatch')
    C['validate_gateway_url'](json.loads((root/'openclaw.json').read_text()),name)
    return name


def restart():
    name = gateway()
    subprocess.run(['docker','restart',name],check=True,capture_output=True,timeout=90)
    deadline = time.monotonic()+90
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen(os.environ['OPENCLAW_GATEWAY_URL']+'/readyz',timeout=2) as r:
                if r.status==200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise TimeoutError('OpenViking Gateway did not load the phase configuration')


def run(request, native):
    import fcntl
    root = directory()
    with (root/'deployment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        phase = root/'phase.json'
        state = json.loads(phase.read_text())
        owner = str(request.get('runtime_context',{}).get('run_dir') or '')
        if not owner or (state.get('active') and state.get('owner')!=owner):
            raise ValueError('Dedicated runtime is owned by another run or run_dir is missing')
        action = request['action']
        gateway()
        if action in ('prepare','set_phase','enter_qa','finalize'):
            C['save'](phase,{'active':True,'owner':owner,'gateway_ready':False})
        result = native(request)
        if action in ('prepare','set_phase','enter_qa','finalize'):
            if action == 'finalize' and not result.get('output',{}).get('restored'):
                raise ValueError('OpenViking original configuration was not restored')
            restart()
            C['save'](phase,{'active':action!='finalize','owner':owner,'gateway_ready':True,
                            'phase':'inactive' if action=='finalize' else action})
        return result
