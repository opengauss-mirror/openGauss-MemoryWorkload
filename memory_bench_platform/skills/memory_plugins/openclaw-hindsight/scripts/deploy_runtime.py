"""Dedicated deployment for the unmodified official Hindsight plugin."""
import json
from pathlib import Path
import runpy
ROOT = Path(__file__).resolve().parent
C = runpy.run_path(str(ROOT.parent.parent / 'scripts/deployment_openclaw.py'))
PLUGIN = 'hindsight-openclaw'


def digest(directory):
    provenance = json.loads((directory / 'provenance.json').read_text())
    return C['fingerprint'](directory, PLUGIN, ('bankId',), [Path(provenance['package_path'])])


def prepare(spec, context):
    if spec.get('options') or not spec.get('upstream_plugin'):
        raise ValueError('Hindsight requires upstream_plugin and no extra options')
    directory, config, service = C['base_runtime'](spec, context, PLUGIN)
    upstream = Path(next(m['source'] for m in service['volumes'] if m['target'] == '/official'))
    provenance = {'package_path': str(upstream), 'files': C['file_hashes'](upstream)}
    runpy.run_path(str(ROOT / 'run_lifecycle.py'))['validate_package'](upstream, provenance)
    config['plugins']['entries'][PLUGIN]['config'].update(
        hindsightApiUrl=context['memory_url'], bankId='mbp-inactive', dynamicBankId=False,
        memoryMode='context', retainEveryNTurns=1, debugPerfTiming=True, autoRetain=False, autoRecall=False)
    C['save'](directory / 'openclaw.json', config)
    C['save'](directory / 'provenance.json', provenance)
    return {'service': service, 'metadata': {'fingerprint': digest(directory)}}


def environment(binding, endpoints):
    directory = Path(binding['directory'])
    return {**C['environment'](directory, endpoints), 'HINDSIGHT_PLUGIN_RUNTIME': str(directory),
            'HINDSIGHT_GATEWAY_CONTAINER': endpoints['container'], 'HINDSIGHT_PLUGIN_API_URL': endpoints['memory_url']}


def issues(binding):
    directory = Path(binding['directory'])
    result = C['phase_issues'](directory)
    if digest(directory) != binding['metadata']['fingerprint']:
        result.append('Hindsight configuration or official package changed')
    return result
