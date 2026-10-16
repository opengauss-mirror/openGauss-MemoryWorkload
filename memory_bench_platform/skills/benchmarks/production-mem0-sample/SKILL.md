---
name: production-mem0-sample
description: Replay a small sanitized OpenMem add/search sample against the Mem0 OSS HTTP Skill with an explicit lossy mapping.
---

# Mem0 production-shaped sample

This scenario uses the production JSONL reader and timing/report pipeline, but maps requests to the existing Mem0 `ingest` and `recall` actions. It is **not** OpenMem API equivalence.

- Add: concatenate text `messages` as `role: content` lines, then call synchronous Mem0 ingest.
- Search: pass `query` and `memory_limit_number` as Mem0 `top_k`.
- Empty source `user_id` becomes a run-specific identity. All rows with that identity share one Mem0 scope within the run.
- The mapping does not implement OpenMem conversation filtering, tags, source, async mode, preferences, tool memories, relativity, visibility flags, or response formatting. Search quality must not be compared to the OpenMem baseline.

Provide a private directory containing one add JSONL and one search JSONL. Use a bounded sample, for example 20 rows from each file; never commit the request bodies. Run with `--benchmark production-mem0-sample --entrypoint replay --agent generic-cli --memory-backend mem0 --data-path DIRECTORY`. For capture and replay, set the same unique `MEMORY_BENCH_REPLAY_IDENTITY_NAMESPACE` in both runs, add `--trace-mode capture` or `--trace-mode replay-zero-delay --trace-bundle BUNDLE`, and `--trace-channel mem0_http=memory-http:ordered:global`. Capture talks to real Mem0; replay serves recorded Mem0 HTTP responses and does not measure Mem0, LLM, embedding, or Milvus latency. Run the same rows sequentially for ordered replay.
