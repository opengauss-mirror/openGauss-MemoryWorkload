---
name: memory-system-auto-instrumentation
description: Use when an unfamiliar memory system needs add, ingest, search, recall, retrieval, latency, throughput, or stage-level performance instrumentation.
---

# Memory System Auto-Instrumentation

Trace the real public call paths before editing. Add request-correlated timing only at verified boundaries, preserving business behavior and keeping category shares additive.

## Discovery

1. Locate public add/search entry points: HTTP routes, SDK methods, CLI commands, queue consumers, and scheduled workers.
2. Trace each entry point through business services to model, embedding, storage, vector search, keyword search, fusion, rerank, and response formatting calls.
3. Identify async boundaries, queue hand-offs, thread/process offloads, retries, locks, connection pools, and background tasks.
4. Inspect the repository's logger, telemetry, feature configuration, and context propagation. Reuse them instead of introducing a parallel mechanism.
5. Record the verified call graph. Do not infer a stage from a function name alone.

Do not edit source until the call graph and Stage Map are complete.

## Stage Map

Publish a table before editing with: public operation, source location, concrete dependency call, canonical stage, `leaf` or `wrapper`, sync/async boundary, and `request_id` propagation path.

Map verified work to these canonical stages:

| Flow | Canonical stage |
|---|---|
| Write | `extract.preprocess` |
| Write | `extract.llm` |
| Write | `extract.vectorize` |
| Write | `extract.write` |
| Read | `retrieve.query_vectorize` |
| Read | `retrieve.vector_search` |
| Read | `retrieve.keyword_search` |
| Read | `retrieve.fusion` |
| Read | `retrieve.rerank` |
| Read | `retrieve.format` |

Category shares use non-overlapping leaf stages only. Parent orchestration, request totals, and queue totals are `wrapper` spans for diagnosis and never share the same denominator with their children.

If the system has no optional stage, such as keyword search or rerank, mark it `absent` in the Coverage Report. Do not manufacture a span or zero duration.

When async code offloads blocking work, time both roles when needed: an outer wrapper measures scheduling and wait time, while an inner `*.backend_call` leaf measures the actual dependency call. Never count both in category shares.

## Instrumentation Contract

Instrumentation is default off and becomes active only through the repository's explicit benchmark configuration. Emit one structured JSON object per line when enabled.

Every event contains:

```text
kind, ts, operation, stage, span_role, duration_ms, status, request_id
```

Use a monotonic clock for `duration_ms` and a timezone-aware wall clock for `ts`. Stage names are stable and contain no request-specific values. Add identifiers and non-sensitive dimensions only when already available, such as `trace_id`, hashed `task_id`, counts, provider, model, backend, and retry count.

Propagate one stable `request_id` from the public entry point across queues, tasks, thread offloads, dependency calls, success events, and error events. Missing correlation is a coverage failure, not a zero-duration stage.

Instrument the real dependency boundary. Emit `status=ok` on success. On failure, emit `status=error` with a sanitized exception type and message, then re-raise the original exception without changing its type, arguments, traceback, or return behavior.

## Safety

Do not log prompts, messages, queries, raw requests, memory content, retrieved content, credentials, authorization headers, API keys, tokens, private endpoints, or original user/tenant identities. Prefer counts, sizes, hashes, stable stage names, and allow-listed dimensions.

Keep edits surgical. Do not change response schemas, retry policy, timeout behavior, ordering, concurrency, exception behavior, or provider configuration. Avoid new instrumentation infrastructure when existing telemetry can express the contract.

## Verification

1. Add focused tests for enable/disable behavior, required fields, `request_id` propagation, success/error events, exception preservation, and redaction.
2. Run the target repository's relevant tests.
3. Run a short add probe and search probe with instrumentation enabled.
4. Parse every emitted line as JSON and verify required fields, stable stage names, and leaf/wrapper roles.
5. Scan trace output for request and memory sentinels, credentials, raw identities, and private endpoints.
6. Compare enabled and disabled business results and exceptions. They must be semantically identical.

If a probe cannot run because a dependency or credential is unavailable, mark the affected stage `unverified`; never report it as covered.

## Coverage Report

Return the verified call graph, Stage Map, modified files, test commands/results, and a coverage table with one row per canonical stage:

| Field | Meaning |
|---|---|
| `stage` | Canonical stage |
| `state` | Exactly one of `covered`, `absent`, `missing`, or `unverified` |
| `source_location` | Verified call boundary, if present |
| `span_role` | `leaf` or `wrapper`, if instrumented |
| `request_id` | Propagation source and carrier |
| `missing_request_id` | Whether events exist but cannot be joined |
| `verification` | Focused test or probe evidence |
| `notes` | Concise limitation or reason |

Report missing request correlation separately from missing timing. List every known limitation; do not convert missing or unverified evidence into performance conclusions.
