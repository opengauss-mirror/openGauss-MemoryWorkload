"""Deploy official MemOS Local inside the dedicated OpenClaw container."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import yaml
ROOT = Path(__file__).resolve().parent
C = runpy.run_path(str(ROOT.parent.parent / 'scripts/deployment_openclaw.py'))
MEMORY_PLACEMENT = 'agent'
PLUGIN = 'memos-local-plugin'


def config_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def prepare(spec, context):
    if not spec.get('upstream_plugin') or not spec.get('memory_config') or spec.get('options'):
        raise ValueError('MemOS requires upstream_plugin and memory_config; no extra options')
    if context['memory_port'] != 18799:
        raise ValueError('Official MemOS Local 2.0.8 fixes its HTTP endpoint at container port 18799')
    upstream = Path(spec['upstream_plugin'])
    package = json.loads((upstream / 'package.json').read_text())
    if (package.get('name'), package.get('version')) != ('@memtensor/memos-local-plugin', '2.0.8'):
        raise ValueError('Use official @memtensor/memos-local-plugin 2.0.8')
    directory, config, service = C['base_runtime'](spec, context, PLUGIN)
    upstream = Path(next(m['source'] for m in service['volumes'] if m['target'] == '/official'))
    memory = yaml.safe_load(Path(spec['memory_config']).read_text())
    if not isinstance(memory, dict):
        raise ValueError('MemOS memory_config must contain a YAML mapping')
    memory.setdefault('algorithm', {}).setdefault('lightweightMemory', {})['enabled'] = True
    memory['viewer'] = {'bindHost': '0.0.0.0', 'openOnFirstTurn': False}
    memory['hub'] = {'enabled': False}
    memory['telemetry'] = {'enabled': False}
    # JSON is valid YAML; retain the same atomic 0600 writer for model credentials.
    C['save'](directory / 'memos/config.yaml', memory)
    config['plugins']['entries'][PLUGIN]['config'] = {'memory_add': {'enabled': False}}
    C['save'](directory / 'openclaw.json', config)
    final = copy.deepcopy(config)
    final['tools'] = {'allow': ['memos_search','memos_get','memos_timeline','read'], 'fs': {'workspaceOnly': True}}
    final['agents']['defaults']['workspace'] = '/runtime/qa-workspace'
    final['agents']['list'] = [{'id': 'main', 'workspace': '/runtime/qa-workspace'}]
    info = {'version': '2.0.8', 'plugin_path': str(upstream), 'plugin_hashes': C['file_hashes'](upstream),
            'ingest_recall': 'native_enabled', 'serial_samples': True,
            'memos_config_sha256': hashlib.sha256((directory / 'memos/config.yaml').read_bytes()).hexdigest()}
    C['save'](directory / 'runtime.json', info)
    service['environment']['MEMOS_HOME'] = '/runtime/memos'
    return {'service': service, 'metadata': {'initial_config': config_digest(config),
            'finished_config': config_digest(final), 'runtime_identity': config_digest(info)}}


def environment(binding, endpoints):
    directory = Path(binding['directory'])
    if json.loads((directory / 'phase.json').read_text()).get('owner'):
        raise ValueError('MemOS Local requires a fresh deployment per run; undeploy and deploy again')
    return {**C['environment'](directory, endpoints), 'MEMOS_LOCAL_RUNTIME_DIR': str(directory),
            'MEMOS_LOCAL_GATEWAY_CONTAINER': endpoints['container'], 'MEMOS_LOCAL_VIEWER_URL': endpoints['memory_url']}


def issues(binding):
    directory = Path(binding['directory'])
    info = json.loads((directory / 'runtime.json').read_text())
    state = json.loads((directory / 'phase.json').read_text())
    config = json.loads((directory / 'openclaw.json').read_text())
    result = []
    if config_digest(info) != binding['metadata']['runtime_identity']:
        result.append('MemOS runtime identity changed')
    if hashlib.sha256((directory / 'memos/config.yaml').read_bytes()).hexdigest() != info['memos_config_sha256']:
        result.append('MemOS algorithm/model configuration changed')
    if C['file_hashes'](Path(info['plugin_path'])) != info['plugin_hashes']:
        result.append('Official MemOS package changed')
    if state.get('pending') or (state.get('owner') and state.get('phase') != 'finished'):
        result.append('MemOS lifecycle is unfinished')
    expected = 'finished_config' if state.get('phase') == 'finished' else 'initial_config'
    if config_digest(config) != binding['metadata'][expected]:
        result.append('MemOS Gateway configuration was not restored')
    return result
