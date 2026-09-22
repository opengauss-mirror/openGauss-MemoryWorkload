# Mem0 OSS HTTP backend

Implements `memory/1` for `backend_direct`: synchronous `ingest -> recall`.
The shared benchmark supplies session text, answer instructions and judge rules.
This adapter does not install Mem0. For OpenClaw agent_plugin, see
[openclaw-mem0](../../memory_plugins/openclaw-mem0/SKILL.md); it manages the unmodified official plugin, whose TypeScript SDK connects directly to storage.
The two paths do not share an HTTP adapter or SDK.

## Server contract

- `POST /memories`: extract from a complete historical session; return a synchronous
  results list. Empty extraction is allowed. Async/cloud event acknowledgements fail.
- `POST /search`: retrieve within the episode user ID. Return `results` containing
  `id` and `memory`. Empty recall is valid; malformed responses fail.
- No extra compact/flush step: the server's add operation performs extraction.
- `scope_id` derives the user ID. All sessions in an episode share that user;
  different run/sample scopes are isolated. Session IDs in outputs identify adapter
  source operations, not a server-side chat session or an idempotency guarantee.
- Historical dates are supplied in text and metadata. Do not use the cloud-only
  timestamp parameter. No backend extraction prompt is overridden.
- QA only searches; disable memory plugins on the answer agent.
- Use persistent vector/history storage. There are no automatic write retries or
  global resets. A timeout can leave a completed server write; start a fresh run
  when repeating an experiment. Cleanup must target the run's recorded user IDs.

## Configuration

| Environment variable | Meaning |
| --- | --- |
| MEM0_API_URL | OSS server URL; default http://127.0.0.1:8000 |
| MEM0_HTTP_PROFILE | `server` (default): filters.user_id + top_k; `benchmark`: user_id + limit |
| MEM0_API_KEY | Optional OSS X-API-Key; not the LLM credential |
| MEM0_RUNTIME_VERSION | Record deployed source, SDK version and image digest |
| MEM0_TOP_K | Default 20 when the task does not provide node_limit/top_k; native composer currently requests 10 |
| MEM0_HTTP_TIMEOUT_SECONDS | Optional override; default ingest 850s, recall 80s; keep below step deadlines |

The `server` profile follows mem0ai/mem0 server at a39a802. `benchmark` follows
mem0ai/memory-benchmarks docker/mem0 at 4b61c5d. Pin and verify your deployed
server schema: these profiles are not cloud V3. Configure extraction model,
embedding model and vector dimensions on the server, not in Workload.

From `memory_bench_platform/`, after configuring the answer agent and judge:

```bash
export MEM0_API_URL=http://127.0.0.1:8000
export MEM0_HTTP_PROFILE=server
export MEM0_RUNTIME_VERSION='<source commit / SDK version / image digest>'
python -m memory_bench_platform.memory_probe --memory-backend mem0 --output /tmp/mem0-probe-UNIQUE
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw \
  --memory-backend mem0 --memory-integration backend_direct \
  --data-path ../locomo_test/data/locomo_small.json --run-id mem0-locomo-UNIQUE
```

The probe writes a synthetic isolated episode. Answer configuration belongs to
OpenClaw; LoCoMo judge uses LOCOMO_API_KEY, LOCOMO_BASE_URL and LOCOMO_METRIC_MODEL.
Use the same dataset and answer/judge profiles when comparing memory backends.
