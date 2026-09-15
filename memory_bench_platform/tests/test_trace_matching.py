from concurrent.futures import ThreadPoolExecutor

from memory_bench_platform.trace_runtime.matching import ReplayMatcher
from memory_bench_platform.trace_runtime.protocol import (
    ProviderRequest,
    ProviderResponse,
    TraceRecord,
    TraceScope,
    TraceTiming,
)


def _embedding_record(index: int) -> TraceRecord:
    return TraceRecord(
        trace_id=f"trace-{index}",
        ordinal=index,
        channel="embedding",
        protocol="openai-embeddings",
        scope=TraceScope(request_id=f"request-{index}"),
        request=ProviderRequest(
            method="POST",
            path="/v1/embeddings",
            headers={"content-type": "application/json"},
            body={"model": "embed", "input": [f"text-{index}"]},
        ),
        response=ProviderResponse(
            status=200,
            headers={"content-type": "application/json"},
            body={"data": [{"embedding": [float(index)]}]},
        ),
        timing=TraceTiming(
            started_at="2026-09-14T00:00:00+00:00",
            upstream_duration_ms=1,
        ),
    )


def test_compat_match_uses_bucket_and_consumes_copies_thread_safely():
    matcher = ReplayMatcher(
        records=[_embedding_record(1)],
        match_mode="compat",
        copies=8,
    )
    request = ProviderRequest(
        method="POST",
        path="/v1/embeddings",
        headers={"content-type": "application/json"},
        body={"model": "embed", "input": ["different text"]},
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: matcher.match(request, TraceScope()), range(8)))

    assert len(results) == 8
    assert matcher.snapshot().model_dump() == {
        "loaded": 8,
        "matched": 8,
        "mismatched": 0,
        "remaining": 0,
        "errors": 0,
        "active": 0,
        "queued": 0,
        "peak_active": 1,
    }


def test_strict_match_rejects_different_request_body():
    matcher = ReplayMatcher(
        records=[_embedding_record(1)],
        match_mode="strict",
        copies=1,
    )
    request = ProviderRequest(
        method="POST",
        path="/v1/embeddings",
        headers={"content-type": "application/json"},
        body={"model": "embed", "input": ["different text"]},
    )

    assert matcher.match(request, TraceScope()) is None
    assert matcher.snapshot().mismatched == 1


def test_held_mismatch_stays_active_until_request_finishes():
    matcher = ReplayMatcher(
        records=[_embedding_record(1)],
        match_mode="strict",
        copies=1,
    )
    request = ProviderRequest(
        method="POST",
        path="/v1/embeddings",
        body={"model": "embed", "input": ["different text"]},
    )

    assert matcher.match(request, TraceScope(), hold_active=True) is None
    assert matcher.snapshot().active == 1
    matcher.finish_request()
    assert matcher.snapshot().active == 0


def test_ordered_match_keeps_session_queues_isolated():
    first = _embedding_record(1).model_copy(
        update={"scope": TraceScope(request_id="request-1", session_id="session-1")}
    )
    second = _embedding_record(2).model_copy(
        update={"scope": TraceScope(request_id="request-2", session_id="session-2")}
    )
    matcher = ReplayMatcher(
        records=[first, second],
        match_mode="ordered",
        order_scope="session",
        copies=1,
    )

    matched_second = matcher.match(second.request, TraceScope(session_id="session-2"))
    matched_first = matcher.match(first.request, TraceScope(session_id="session-1"))

    assert matched_second is not None and matched_second.trace_id == "trace-2"
    assert matched_first is not None and matched_first.trace_id == "trace-1"


def test_ordered_session_match_rejects_missing_scope():
    record = _embedding_record(1).model_copy(
        update={"scope": TraceScope(request_id="request-1")}
    )
    matcher = ReplayMatcher(
        records=[record],
        match_mode="ordered",
        order_scope="session",
        copies=1,
    )

    assert matcher.match(record.request, TraceScope()) is None
    snapshot = matcher.snapshot()
    assert snapshot.mismatched == 1
    assert snapshot.remaining == 1
    assert snapshot.active == 0
