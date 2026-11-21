"""Common dedicated OpenClaw deployment plumbing; memory semantics stay in adapters."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit


def fingerprint(directory, plugin_id, mutable, code_paths=()):
    config = json.loads((directory / "openclaw.json").read_text())
    entry = config["plugins"]["entries"][plugin_id]["config"]
    for key in mutable:
        entry.pop(key, None)
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    for root in code_paths:
        root = Path(root)
        for path in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if path.is_file():
                digest.update(str(path.relative_to(root) if root.is_dir() else path.name).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def service(directory):
    return {"volumes": [{"type": "bind", "source": str(directory), "target": "/runtime"}],
            "labels": {"memory-bench.runtime": str(directory)},
            "user": f"{os.getuid()}:{os.getgid()}",
            "environment": {"HOME": "/runtime", "OPENCLAW_CONFIG_PATH": "/runtime/openclaw.json",
                            "OPENCLAW_STATE_DIR": "/runtime/state"}}


def environment(directory, endpoints):
    config = json.loads((directory / "openclaw.json").read_text())
    return {"OPENCLAW_CONFIG_PATH": str(directory / "openclaw.json"),
            "OPENCLAW_STATE_DIR": str(directory / "state"), "OPENCLAW_TRANSPORT": "http",
            "OPENCLAW_GATEWAY_URL": endpoints["gateway_url"],
            "OPENCLAW_GATEWAY_TOKEN": config["gateway"]["auth"]["token"]}


def validate_gateway_url(config, container):
    url = os.environ.get("OPENCLAW_GATEWAY_URL", "").rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("Gateway URL must be a dedicated loopback HTTP endpoint")
    info = json.loads(subprocess.run(["docker", "inspect", container], check=True,
        capture_output=True, text=True, timeout=15).stdout)[0]
    port = config["gateway"]["port"]
    bindings = info.get("NetworkSettings", {}).get("Ports", {}).get(f"{port}/tcp") or []
    if info.get("HostConfig", {}).get("NetworkMode") == "host":
        valid = parsed.port == port
    else:
        valid = any(b.get("HostIp") in ("127.0.0.1", "0.0.0.0", "") and int(b["HostPort"]) == parsed.port for b in bindings)
    if not valid:
        raise ValueError("Gateway URL does not match its container's loopback port binding")
    return url


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'w', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
    temporary.replace(path)


def base_runtime(spec, context, plugin=None, slot='memory'):
    import secrets
    source = json.loads(Path(spec['source_config']).read_text())
    directory = Path(context['directory'])
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    (directory / 'state').mkdir(mode=0o700)
    defaults = source.get('agents', {}).get('defaults', {})
    config = {'models': source.get('models', {}), 'agents': {'defaults': {
        'model': defaults.get('model', {}), 'workspace': '/runtime/workspace',
        'skipBootstrap': True, 'memorySearch': {'enabled': False},
        'heartbeat': {'every': '0m'}, 'timeoutSeconds': 900}}, 'tools': {'deny': ['*']},
        'gateway': {'mode': 'local', 'bind': 'lan', 'port': context['gateway_port'],
                    'auth': {'mode': 'token', 'token': secrets.token_hex(24)},
                    'reload': {'mode': 'off'}, 'http': {'endpoints': {'responses': {'enabled': True}}}},
        'plugins': {'slots': {'memory': 'none'}}}
    patch = service(directory)
    if plugin:
        upstream = owned_package(Path(spec['upstream_plugin']).resolve(strict=True), directory / "official")
        manifest = json.loads((upstream / 'openclaw.plugin.json').read_text())
        if manifest.get('id') != plugin:
            raise ValueError('Official plugin ID does not match the requested adapter')
        entry = source.get('plugins', {}).get('entries', {}).get(plugin, {}).get('config', {})
        config['plugins'] = {'enabled': True, 'allow': [plugin], 'load': {'paths': ['/official']},
            'slots': {'memory': 'none', slot: plugin}, 'entries': {plugin: {'enabled': True,
            'hooks': {'allowConversationAccess': True, 'allowPromptInjection': True}, 'config': entry}}}
        patch['volumes'].append({'type': 'bind', 'source': str(upstream), 'target': '/official', 'read_only': True})
    save(directory / 'phase.json', {'active': False})
    return directory, config, patch


def file_hashes(upstream):
    upstream = Path(upstream)
    return {p.relative_to(upstream).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(upstream.rglob('*')) if p.is_file() and '.git' not in p.parts}


def phase_issues(directory):
    state = json.loads((directory / 'phase.json').read_text())
    if state.get('active') or state.get('pending'):
        return ['Plugin lifecycle is unfinished']
    if state.get('phase') and (state['phase'] != 'inactive' or not state.get('gateway_ready')):
        return ['Native Gateway has not confirmed the inactive phase']
    return []


def owned_package(upstream, destination):
    """OpenClaw requires plugin ownership to match its UID or root.

    Copy, never chown another user's installation; runtime copies remain owned
    by this deployment and disappear with it. Normal same-user installs mount
    their existing package read-only without a copy.
    """
    import shutil
    upstream = Path(upstream)
    if upstream.stat().st_uid in (0, os.getuid()):
        return upstream
    shutil.copytree(upstream, destination, symlinks=False)
    return Path(destination)
