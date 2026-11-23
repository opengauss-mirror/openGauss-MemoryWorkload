"""Dedicated OpenClaw MCP deployment; uses no substitute memory plugin."""
import hashlib
import json
from pathlib import Path
import runpy
ROOT = Path(__file__).resolve().parent
C = runpy.run_path(str(ROOT.parent.parent / 'scripts/deployment_openclaw.py'))


def digest(directory):
    return hashlib.sha256((directory / 'openclaw.json').read_bytes()).hexdigest()


def prepare(spec, context):
    options = spec.get('options', {})
    if spec.get('upstream_plugin') or set(options) - {'mcp_path'}:
        raise ValueError('JiuwenMemory uses builtin MCP; only mcp_path is configurable')
    path = options.get('mcp_path', '/mcp')
    if not isinstance(path, str) or not path.startswith('/') or any(c in path for c in ('?', '#', '\\')) or path.startswith('//'):
        raise ValueError('mcp_path must be an absolute HTTP path')
    directory, config, service = C['base_runtime'](spec, context)
    source = json.loads(Path(spec['source_config']).read_text())
    server = source.get('mcp', {}).get('servers', {}).get('jiuwen', {})
    config['mcp'] = {'servers': {'jiuwen': {**server, 'url': context['memory_url'] + path,
        'transport': 'streamable-http', 'toolFilter': {'include': ['memory_add', 'memory_search']}}}}
    config['tools'] = {'allow': ['jiuwen__memory_add', 'jiuwen__memory_search']}
    config['agents']['list'] = [
        {'id': 'jiuwen-ingest', 'tools': {'allow': ['jiuwen__memory_add']}},
        {'id': 'main', 'tools': {'allow': ['jiuwen__memory_search']}}]
    C['save'](directory / 'openclaw.json', config)
    return {'service': service, 'metadata': {'fingerprint': digest(directory)}}


def environment(binding, endpoints):
    directory = Path(binding['directory'])
    config = json.loads((directory / 'openclaw.json').read_text())
    return {**C['environment'](directory, endpoints),
            'JIUWEN_MCP_GATEWAY_CONTAINER': endpoints['container'],
            'JIUWENMEMORY_DEPLOYMENT_RUNTIME': str(directory),
            'JIUWENMEMORY_MCP_URL': config['mcp']['servers']['jiuwen']['url']}


def issues(binding):
    return [] if digest(Path(binding['directory'])) == binding['metadata']['fingerprint'] else ['JiuwenMemory Gateway configuration changed']
