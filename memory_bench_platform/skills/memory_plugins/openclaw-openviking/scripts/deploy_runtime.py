"""Deploy official OpenViking context engine with the existing native lifecycle."""
import json
from pathlib import Path
import runpy
ROOT = Path(__file__).resolve().parent
C = runpy.run_path(str(ROOT.parent.parent / 'scripts/deployment_openclaw.py'))
PLUGIN = 'openviking'


def digest(directory):
    provenance = json.loads((directory / 'provenance.json').read_text())
    return C['fingerprint'](directory, PLUGIN, (), [Path(provenance['package_path'])])


def prepare(spec, context):
    if not spec.get('upstream_plugin') or spec.get('options'):
        raise ValueError('OpenViking requires upstream_plugin and no extra options')
    upstream = Path(spec['upstream_plugin'])
    schema = json.loads((upstream / 'openclaw.plugin.json').read_text())
    props = schema.get('configSchema', {}).get('properties', {})
    if any(key not in props for key in ('autoCapture','autoRecall','baseUrl')):
        raise ValueError('Official OpenViking plugin must expose capture/recall switches and baseUrl')
    directory, config, service = C['base_runtime'](spec, context, PLUGIN, 'contextEngine')
    upstream = Path(next(m['source'] for m in service['volumes'] if m['target'] == '/official'))
    config['plugins']['entries'][PLUGIN]['config'].update(mode='remote', baseUrl=context['memory_url'],
        autoCapture=False, autoRecall=False, captureMode='semantic', commitTokenThreshold=100000,
        commitKeepRecentCount=0)
    C['save'](directory / 'openclaw.json', config)
    C['save'](directory / 'provenance.json', {'package_path': str(upstream)})
    return {'service': service, 'metadata': {'fingerprint': digest(directory)}}


def environment(binding, endpoints):
    directory = Path(binding['directory'])
    config = json.loads((directory / 'openclaw.json').read_text())['plugins']['entries'][PLUGIN]['config']
    return {**C['environment'](directory, endpoints), 'OPENVIKING_DEPLOYMENT_RUNTIME': str(directory),
            'OPENVIKING_GATEWAY_CONTAINER': endpoints['container'],
            'OPENVIKING_PLUGIN_API_URL': endpoints['memory_url'],
            'OPENVIKING_PLUGIN_API_KEY': config.get('apiKey', '')}


def issues(binding):
    directory = Path(binding['directory'])
    result = C['phase_issues'](directory)
    if digest(directory) != binding['metadata']['fingerprint']:
        result.append('OpenViking configuration or official package changed')
    return result
