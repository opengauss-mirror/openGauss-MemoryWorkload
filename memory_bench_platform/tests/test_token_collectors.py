import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_bench_platform import token_collectors as collectors, token_usage as tokens


def context(tmp_path, source='native', backend='custom'):
    return {'run_id': 'test', 'run_dir': str(tmp_path), 'memory_backend': backend,
            'memory_integration': 'agent_plugin', 'memory_token_source': source}


def test_registered_collector_receives_full_lifecycle_without_runner_changes(tmp_path, monkeypatch):
    calls = []
    def start(ctx):
        calls.append('start')
        return 42
    def observe(payload):
        calls.append('observe')
        tokens.record(payload['usage'], component='memory', source='custom_native', record_id='request')
    def finish(ctx, state):
        assert state == 42
        calls.append('finish')
    monkeypatch.setattr(collectors, 'NATIVE_COLLECTORS', {})
    collectors.register_native('custom', collectors.Collector('custom_native', start, finish, observe))
    ctx = context(tmp_path)
    tokens.initialize(ctx)
    run = collectors.start(ctx)
    with tokens.scope(ctx):
        collectors.observe({'usage': {'prompt_tokens': 8, 'completion_tokens': 2}})
    collectors.finish(run)
    assert calls == ['start', 'observe', 'finish']
    assert tokens.rebuild(tmp_path)['observed_memory_llm_tokens'] == 10
    with pytest.raises(ValueError):
        collectors.register_native('custom', collectors.Collector('other'))


@pytest.mark.parametrize('source,total', [('native', 100), ('model_proxy', 20), ('off', None), ('invalid', None)])
def test_memory_source_is_exclusive_and_does_not_filter_agent(tmp_path, source, total):
    ctx = context(tmp_path, source)
    tokens.initialize(ctx)
    with tokens.scope(ctx):
        tokens.record({'prompt_tokens': 90, 'completion_tokens': 10}, component='memory',
                      source='openviking_task', granularity='task', record_id='native')
        tokens.record({'prompt_tokens': 15, 'completion_tokens': 5}, component='memory',
                      source='model_proxy', record_id='proxy')
        tokens.record({'prompt_tokens': 2, 'completion_tokens': 1}, component='agent',
                      source='agent_response', phase='qa', record_id='agent')
    report = tokens.rebuild(tmp_path)
    assert report['observed_memory_llm_tokens'] == total
    assert report['observed_agent_qa_tokens'] == 3
    assert report['observed_system_llm_tokens'] == (total or 0) + 3
    shards = list((tmp_path/'records/token-usage/shards').glob('*.jsonl'))
    rows = [json.loads(line) for p in shards for line in p.read_text().splitlines()]
    assert len(rows) == 3  # Suppressed source remains evidence, not another charge.


def test_proxy_selection_does_not_start_native_counter_or_fallback(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail('Native collector must not run under proxy policy')
    monkeypatch.setitem(collectors.NATIVE_COLLECTORS, 'ogmemory', collectors.Collector('native', forbidden, forbidden, forbidden))
    monkeypatch.delenv('MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR', raising=False)
    ctx = context(tmp_path, 'model_proxy', 'ogmemory')
    tokens.initialize(ctx)
    run = collectors.start(ctx)
    with tokens.scope(ctx):
        collectors.observe({'usage': {'prompt_tokens': 100}})
    collectors.finish(run)
    report = tokens.rebuild(tmp_path)
    assert report['observed_memory_llm_tokens'] is None
    assert report['diagnostic_count'] > 0


def test_native_ov_task_uses_registered_operation_hook(tmp_path):
    ctx = context(tmp_path, backend='openviking')
    tokens.initialize(ctx)
    with tokens.scope(ctx):
        collectors.observe({'output': {'task_id': 't', 'task_result': {'token_usage': {
            'llm': {'prompt_tokens': 10, 'completion_tokens': 2}, 'embedding': {'total_tokens': 3}}}}})
    report = tokens.rebuild(tmp_path)
    assert report['observed_memory_llm_tokens'] == 12
    assert report['observed_memory_embedding_tokens'] == 3


def test_collector_errors_are_diagnostics_and_disabled_starts_nothing(tmp_path, monkeypatch):
    def broken(*args):
        raise RuntimeError('should never escape accounting')
    monkeypatch.setitem(collectors.NATIVE_COLLECTORS, 'custom', collectors.Collector('broken', broken, broken, broken))
    ctx = context(tmp_path)
    tokens.initialize(ctx)
    run = collectors.start(ctx)
    with tokens.scope(ctx):
        collectors.observe({})
    collectors.finish(run)
    assert tokens.rebuild(tmp_path)['diagnostic_count'] == 3
    monkeypatch.setenv('MEMORY_BENCH_TOKEN_USAGE', '0')
    assert collectors.start(ctx) is None


def test_runtime_context_freezes_source_selection(tmp_path, monkeypatch):
    runtime = SimpleNamespace(run_id='r', run_dir=str(tmp_path), memory_id='mem0', memory_integration='backend_direct')
    monkeypatch.setenv('MEMORY_BENCH_MEMORY_TOKEN_SOURCE', 'model_proxy')
    ctx = tokens.context(runtime)
    monkeypatch.setenv('MEMORY_BENCH_MEMORY_TOKEN_SOURCE', 'native')
    assert tokens.memory_source(ctx) == 'model_proxy'
