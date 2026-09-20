---
name: production-http-replay
description: Use when replaying OpenMem add and search JSONL traffic through a compatible Memory Skill for production-shaped memory-system benchmarks.
---

# Production HTTP Replay

Streams one add JSONL and one search JSONL from a directory. It sends complete requests through a Memory Skill using `ingest`, drains accepted writes with `status`, then starts `recall` traffic. The selected Memory Skill must declare `openmem-v1` in `capabilities.raw_request_protocols`.

## Validate

```bash
memory-bench validate \
  --benchmark production-http-replay \
  --data-path /path/to/add-and-search-directory
```

The directory must contain exactly one direct, regular add JSONL and one search JSONL. Every row contains an object-valued `request`; `response` is optional and is never sent.

## Run

```bash
memory-bench run \
  --benchmark production-http-replay \
  --entrypoint replay \
  --agent generic-cli \
  --memory-backend <openmem-v1-compatible-memory-skill> \
  --data-path /path/to/add-and-search-directory
```

The run archive contains request hashes, sizes, field names, timing, status, counts, and correlation coverage. It does not contain raw requests, messages, queries, returned memories, credentials, private endpoints, or original identities.

## Execution Contract

- All add calls and async status polling finish before search begins.
- Empty `user_id` becomes the deterministic `replay-<run_id>` identity; non-empty values remain unchanged in the live request.
- Per-request failures are recorded and do not stop the remaining workload.
- Any terminal add failure or drain timeout marks the dataset `partially_written`.
- Attribution is `validated` only when every unique client request joins to an internal trace without duplicate request IDs; otherwise it is `exploratory`.
