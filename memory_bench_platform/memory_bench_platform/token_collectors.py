"""One lifecycle and extension point for memory usage collectors.

Collectors produce evidence; token_usage owns normalization, source selection,
deduplication and reports. Agent usage remains captured at the Agent boundary.
"""
from dataclasses import dataclass
from typing import Any, Callable

from . import token_usage as tokens
from . import token_proxy_audit


def _no_start(ctx):
    return None


def _no_finish(ctx, state):
    pass


def _no_observe(payload):
    pass


@dataclass(frozen=True)
class Collector:
    """Optional run window and per-operation evidence hooks. Never runs a model."""
    name: str
    start: Callable = _no_start
    finish: Callable = _no_finish
    observe: Callable = _no_observe


NATIVE_COLLECTORS: dict[str, Collector] = {}


def register_native(memory_backend: str, collector: Collector):
    """Register a backend-specific evidence reader without changing the runner."""
    if not memory_backend or memory_backend in NATIVE_COLLECTORS:
        raise ValueError("Collector backend must be nonempty and registered once")
    NATIVE_COLLECTORS[memory_backend] = collector


register_native("openviking", Collector("openviking_task", observe=tokens.collect_ov_task))
register_native("ogmemory", Collector("ogmemory_counter", start=tokens.start_counters,
                                     finish=tokens.finish_counters))
PROXY_COLLECTOR = Collector("model_proxy", start=token_proxy_audit.begin, finish=token_proxy_audit.finish)


def resolve(ctx):
    source = tokens.memory_source(ctx)
    if source == "model_proxy":
        return PROXY_COLLECTOR
    if source == "native":
        return NATIVE_COLLECTORS.get(ctx.get("memory_backend"))
    if source != "off":
        raise ValueError("Unknown memory token source")
    return None


@dataclass
class CollectionRun:
    context: dict
    collector: Collector
    state: Any


def start(ctx):
    if not tokens.enabled():
        return None
    ctx = {**ctx, "memory_token_source": tokens.memory_source(ctx)}
    with tokens.scope(ctx):
        def begin():
            collector = resolve(ctx)
            if collector is None:
                return None
            state = tokens.best_effort(collector.start, ctx)
            return CollectionRun(ctx, collector, state)
        return tokens.best_effort(begin)


def observe(payload):
    ctx = tokens.current_context()
    if not ctx:
        return
    def collect():
        collector = resolve(ctx)
        if collector:
            collector.observe(payload)
    tokens.best_effort(collect)


def finish(run):
    if run is None:
        return
    with tokens.scope(run.context):
        tokens.best_effort(run.collector.finish, run.context, run.state)
