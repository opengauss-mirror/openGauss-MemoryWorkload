from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from memory_bench_platform import token_proxy_audit as audit, token_usage as tokens
from memory_bench_platform.token_proxy import SSE_FRAME_END, make_server


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def relay(tmp_path, monkeypatch, request):
    backend = getattr(request, 'param', 'mem0')
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path, dict(self.headers), body))
            req = json.loads(body)
            scenario = req.get('model', 'ok')
            usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                     'prompt_tokens_details': {'cached_tokens': 40}}
            payload = {'id': 'same-provider-id', 'choices': [{'message': {'content': 'PRIVATE_RESULT'}}], 'usage': usage}
            if self.path.endswith('embeddings'):
                payload = {'data': [{'index': 0, 'embedding': [0.1, 0.2]}],
                           'usage': {'prompt_tokens': 7, 'total_tokens': 7}}
            if scenario == 'missing': payload.pop('usage')
            if scenario == 'failed': payload = {'error': {'message': 'PRIVATE_UPSTREAM_ERROR'}}
            if scenario == 'stream':
                data = b'data: '+json.dumps({'usage': usage}).encode()+b'\n\n'
                data += b'data: '+json.dumps({'usage': usage}).encode()+b'\n\ndata: [DONE]\n\n'
            else:
                data = json.dumps(payload).encode()
            self.send_response(429 if scenario == 'failed' else 200)
            self.send_header('Content-Type', 'text/event-stream' if scenario == 'stream' else 'application/json')
            self.send_header('Content-Length', str(len(data) + (20 if scenario == 'truncated' else 0)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

    with running(ThreadingHTTPServer(('127.0.0.1', 0), Upstream)) as upstream:
        base = f'http://127.0.0.1:{upstream.server_port}/provider/v1'
        config = {'memory_backend': backend, 'llm_base_url': base, 'embedding_base_url': base, 'request_timeout_seconds': 2}
        with running(make_server(config, tmp_path/'audit')) as server:
            monkeypatch.setenv('MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE', '1')
            monkeypatch.setenv('MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR', str(tmp_path/'audit'))
            monkeypatch.setenv('MEMORY_BENCH_TOKEN_USAGE', '1')
            monkeypatch.setenv('MEMORY_BENCH_MEMORY_TOKEN_SOURCE', 'model_proxy')
            ctx = {'run_dir': str(tmp_path/'run'), 'run_id': 'test', 'memory_backend': backend,
                   'memory_token_source': 'model_proxy',
                   'memory_integration': 'backend_direct'}
            tokens.initialize(ctx)
            yield server, ctx, received


def request(server, model='ok', endpoint='chat/completions'):
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'PRIVATE_PROMPT'}],
                       'temperature': 0.6, 'input': 'PRIVATE_EMBEDDING'}).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/{endpoint}', data=body,
                                 headers={'Authorization': 'Bearer PRIVATE_API_KEY', 'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), body


def summary(ctx):
    value = tokens.rebuild(ctx['run_dir'])
    records = [json.loads(l) for l in (Path(ctx['run_dir'])/'records/token_usage.jsonl').read_text().splitlines()]
    return value, records


@pytest.mark.parametrize('mode', ['backend_direct', 'agent_plugin'])
@pytest.mark.parametrize('relay', ['mem0', 'openviking', 'ogmemory', 'custom-backend'], indirect=True)
def test_real_http_routes_usage_cache_dedup_and_isolation(relay, mode):
    server, ctx, received = relay
    ctx['memory_integration'] = mode
    tokens.initialize(ctx)
    request(server)  # Preflight is outside the run window.
    start = audit.begin(ctx)
    for _ in range(2):
        status, body, sent = request(server)
        assert status == 200 and json.loads(body)['choices'][0]['message']['content'] == 'PRIVATE_RESULT'
        assert received[-1][0] == '/provider/v1/chat/completions'
        assert received[-1][1]['Authorization'] == 'Bearer PRIVATE_API_KEY'
        assert received[-1][2] == sent
    request(server, endpoint='embeddings')
    with tokens.scope(ctx):
        tokens.record({'prompt_tokens': 5, 'completion_tokens': 1}, component='agent', phase='qa', record_id='agent1')
        tokens.record({'prompt_tokens': 999, 'completion_tokens': 999}, component='evaluator', phase='judge')
    audit.finish(ctx, start)
    a, records = summary(ctx)
    assert a['observed_memory_llm_tokens'] == 240
    assert a['observed_memory_embedding_tokens'] == 7
    assert a['observed_system_llm_tokens'] == 246  # Embeddings and Judge excluded.
    assert a['cost_categories']['memory_llm']['cache_read_tokens'] == 80
    assert a['source_coverage']['memory_model_proxy'] == 'partial'
    assert len(records) == 4
    assert len({r['record_id'] for r in records}) == 4  # Same provider ID, separate real attempts.
    assert all(r['phase'] == 'unknown' for r in records if r['component'] == 'memory')
    audit.finish(ctx, start)  # Re-import is idempotent, not new model calls.
    b, _ = summary(ctx)
    assert b['observed_system_llm_tokens'] == a['observed_system_llm_tokens']
    first = (Path(ctx['run_dir'])/'reports/token_summary.json').read_bytes()
    summary(ctx)
    assert (Path(ctx['run_dir'])/'reports/token_summary.json').read_bytes() == first
    for p in list(server.audit.root.rglob('*.json*')) + list(Path(ctx['run_dir']).rglob('*.json*')):
        assert 'PRIVATE_' not in p.read_text()


@pytest.mark.parametrize('scenario,expected_status,expected_total', [
    ('failed', 429, None), ('truncated', 502, None), ('missing', 200, None), ('stream', 200, 120)])
def test_errors_missing_and_stream_snapshots(relay, scenario, expected_status, expected_total):
    server, ctx, received = relay
    start = audit.begin(ctx)
    status, _, _ = request(server, model=scenario)
    assert status == expected_status
    # SSE audit completes at upstream EOF, not when the final data frame is seen.
    with server.audit.lock: pass
    audit.finish(ctx, start)
    value, rows = summary(ctx)
    assert len(received) == 1  # Relay never retries.
    assert value['observed_memory_llm_tokens'] == expected_total
    assert len(rows) == 1
    if scenario in {'failed', 'truncated'}:
        assert rows[0]['outcome'] == 'failed'
    if scenario == 'truncated':
        assert rows[0]['raw_usage'] == {}  # Even a JSON-looking prefix must not count.


def test_inflight_is_unknown_and_prior_inflight_refuses_window(relay):
    server, ctx, _ = relay
    common = {'request_id': 'pending', 'model_kind': 'llm', 'model': 'test', 'endpoint': '/v1/chat/completions'}
    start = audit.begin(ctx)
    server.audit.emit(event='started', **common)
    audit.finish(ctx, start)
    value, rows = summary(ctx)
    assert value['observed_memory_llm_tokens'] is None
    assert rows[0]['outcome'] == 'started'
    assert audit.begin(ctx) is None
    server.audit.emit(event='finished', **common, usage={'prompt_tokens': 2, 'completion_tokens': 1}, outcome='succeeded', http_status=200)
    audit.finish(ctx, start)
    assert summary(ctx)[0]['observed_memory_llm_tokens'] == 3


def test_reset_generation_disabled_and_wrong_backend_are_not_imported(relay, monkeypatch):
    server, ctx, _ = relay
    start = audit.begin(ctx)
    request(server)
    manifest_path = server.audit.root/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['proxy_id'] = 'changed'
    manifest_path.write_text(json.dumps(manifest))
    audit.finish(ctx, start)
    assert not summary(ctx)[1]
    monkeypatch.setenv('MEMORY_BENCH_TOKEN_USAGE', '0')
    assert audit.begin(ctx) is None
    monkeypatch.setenv('MEMORY_BENCH_TOKEN_USAGE', '1')
    assert audit.begin({**ctx, 'memory_backend': 'openviking'}) is None


def test_ledger_failure_does_not_change_provider_response(relay, monkeypatch):
    server, _, received = relay
    monkeypatch.setattr(server.audit, 'events', server.audit.root/'missing'/'requests.jsonl')
    status, payload, _ = request(server)
    assert status == 200 and json.loads(payload)['usage']['total_tokens'] == 120
    assert len(received) == 1


def test_concurrent_requests_are_separate(relay):
    from concurrent.futures import ThreadPoolExecutor
    server, ctx, received = relay
    start = audit.begin(ctx)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: request(server)[0], range(8)))
    assert outcomes == [200]*8
    audit.finish(ctx, start)
    value, rows = summary(ctx)
    assert value['observed_memory_llm_tokens'] == 960
    assert len(rows) == len(received) == 8


def test_one_proxy_cannot_be_charged_to_two_runs(relay):
    server, ctx, _ = relay
    other = {**ctx, 'run_id': 'another', 'run_dir': str(Path(ctx['run_dir']).with_name('another'))}
    tokens.initialize(other)
    start = audit.begin(ctx)
    assert start is not None
    assert audit.begin(other) is None
    request(server)
    audit.finish(other, start)  # Even a copied cursor cannot assign foreign usage.
    assert summary(other)[0]['observed_memory_llm_tokens'] is None
    audit.finish(ctx, start)
    assert summary(ctx)[0]['observed_memory_llm_tokens'] == 120
    assert audit.begin(other) is None  # Finished audits are not reused either.


@pytest.mark.parametrize('endpoint,terminal_kind,newline', [
    ('chat/completions', 'done', b'\n'),
    ('responses', 'response.completed', b'\n'),
    ('responses', 'response.completed', b'\r\n'),
])
def test_stream_terminal_waits_for_audit_before_client_can_end_run(
        tmp_path, monkeypatch, endpoint, terminal_kind, newline):
    upstream_sent_terminal = threading.Event()
    release_upstream_eof = threading.Event()
    client_saw_terminal = threading.Event()
    client_saw_usage = threading.Event()
    client_errors = []

    usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
    usage_frame = b'data: ' + json.dumps({'usage': usage}).encode() + b'\n\n'
    if terminal_kind == 'done':
        terminal_frame = b'data: [DONE]\n\n'
    else:
        terminal_frame = (b'data: ' + json.dumps({
            'type': 'response.completed', 'response': {'usage': usage}
        }).encode() + b'\n\n')

    if newline == b'\r\n':
        # Valid SSE: concatenate data lines with a newline to form the JSON.
        usage_frame = usage_frame.replace(b'\n', newline)
        terminal_frame = (b'data: {"type":"response.completed",\r\n'
                          b'data: "response":{"usage":' + json.dumps(usage).encode() + b'}}\r\n\r\n')

    class DelayedEOFUpstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(usage_frame)
            self.wfile.flush()
            # Split the terminal frame across writes to exercise the SSE parser.
            midpoint = (terminal_frame.index(b"\r\n") + 1 if newline == b"\r\n"
                        else len(terminal_frame) // 2)
            self.wfile.write(terminal_frame[:midpoint])
            self.wfile.flush()
            self.wfile.write(terminal_frame[midpoint:])
            self.wfile.flush()
            upstream_sent_terminal.set()
            release_upstream_eof.wait(timeout=5)
            self.close_connection = True

    with running(ThreadingHTTPServer(('127.0.0.1', 0), DelayedEOFUpstream)) as upstream:
        base = f'http://127.0.0.1:{upstream.server_port}/v1'
        config = {'memory_backend': 'mem0', 'llm_base_url': base,
                  'embedding_base_url': base, 'request_timeout_seconds': 5}
        with running(make_server(config, tmp_path/'audit')) as server:
            monkeypatch.setenv('MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE', '1')
            monkeypatch.setenv('MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR', str(tmp_path/'audit'))
            monkeypatch.setenv('MEMORY_BENCH_TOKEN_USAGE', '1')
            monkeypatch.setenv('MEMORY_BENCH_MEMORY_TOKEN_SOURCE', 'model_proxy')
            ctx = {'run_dir': str(tmp_path/'run'), 'run_id': 'terminal-race',
                   'memory_backend': 'mem0', 'memory_token_source': 'model_proxy',
                   'memory_integration': 'backend_direct'}
            tokens.initialize(ctx)
            start = audit.begin(ctx)
            body = json.dumps({'model': 'stream', 'stream': True}).encode()
            req = urllib.request.Request(
                f'http://127.0.0.1:{server.server_port}/v1/{endpoint}',
                data=body, headers={'Content-Type': 'application/json'})

            def read_until_terminal():
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data_lines = []
                        for line in response:
                            if b'"prompt_tokens": 100' in line:
                                client_saw_usage.set()
                            if line.startswith(b'data:'):
                                data_lines.append(line[5:].strip())
                            elif not line.strip() and data_lines:
                                data = b'\n'.join(data_lines)
                                data_lines = []
                                if data == b'[DONE]' or json.loads(data).get('type') == 'response.completed':
                                    client_saw_terminal.set()
                                    return
                except Exception as exc:
                    client_errors.append(exc)
                    client_saw_terminal.set()

            client = threading.Thread(target=read_until_terminal, daemon=True)
            client.start()
            try:
                assert upstream_sent_terminal.wait(timeout=3)
                assert client_saw_usage.wait(timeout=3)  # Nonterminal frames still stream.
                assert not client_saw_terminal.wait(timeout=0.1)
            finally:
                release_upstream_eof.set()
            assert client_saw_terminal.wait(timeout=3)
            client.join(timeout=3)
            assert not client_errors
            # The client ends the run as soon as it sees the terminal event.
            audit.finish(ctx, start)
            value, rows = summary(ctx)
            assert value['observed_memory_llm_tokens'] == 120
            assert len(rows) == 1 and rows[0]['outcome'] == 'succeeded'
            window = json.loads(
                (Path(ctx['run_dir'])/'records/token-usage/model-proxy-window.json').read_text())
            assert window['unfinished'] == 0


def test_crlf_frame_boundary_stays_atomic_across_chunk_splits():
    frame = b'data: {"type":"response.completed",\r\ndata: "response":{}}\r\n\r\n'
    for end in range(1, len(frame)):
        assert SSE_FRAME_END.search(frame[:end]) is None
    match = SSE_FRAME_END.search(frame)
    assert match is not None
    assert match.group() == b"\r\n\r\n"
    assert match.end() == len(frame)
