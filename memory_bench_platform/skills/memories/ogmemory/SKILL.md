# oGMemory memory backend

Native `backend_direct` adapter: `ingest -> flush -> status -> recall`.

Configure the environment variables documented below, then use the platform's
`python -m memory_bench_platform.memory_probe --memory-backend ogmemory --output <new-directory>`
to verify the backend without an answer agent. This writes a synthetic episode.
The adapter does not install oGMemory or apply patches. Use upstream dev commit
`33c27f4740c79d898d35797c063c68d2eb315213` or a descendant containing the required-null
fix (MR !210). At verification on 2026-09-20, upstream master did not contain it.

## Lifecycle

- `ingest`: POST a complete benchmark session to `/api/v1/sessions/<id>/messages`. Preserve speaker labels, dialogue IDs and occurrence date in the content; set `created_at` using the historical date (naive timestamps are interpreted as UTC).
- `flush`: POST `/api/v1/compact`. This synchronously extracts facts and archives the session. **Do not substitute Session Commit**: in the supported dev implementation it archives without extracting facts.
The flush `operation.task_id` identifies the synchronous adapter operation, not a native async job; readiness uses the scoped session.

- `status`: call `wait_until_idle` to drain the index outbox, then check unfinished work. An absent session, unavailable status or failed index write is a failure, not success.
- `recall`: POST `/api/v1/compose` with a separate QA session and the question as `prompt`. Join only `messages` marked `_ogmem=true` into `evidence_text`; retain the full response for diagnosis. Do not use the normally empty legacy `systemPromptAddition` field. No QA messages are ingested and no `after_turn` is called; compose may maintain internal retrieval/session state. Empty evidence is a valid measured outcome.

`scope_id` is mandatory. Account, user and agent IDs are derived from the episode scope. Account isolation is required because some dev configurations search all owner spaces within an account. All history sessions in one sample share this identity; different runs/samples do not. Use an isolated deployment with agent sharing disabled. Authenticated deployments must supply credentials authorized for these scoped accounts and identities; a fixed ordinary-user key is insufficient.

## Environment

| Variable | Default / meaning |
| --- | --- |
| `OGMEM_API_URL` | `http://127.0.0.1:8090` |
| `OGMEM_ACCOUNT_ID` | `memory-bench`; prefix for per-episode accounts |
| `OGMEM_API_KEY` | Optional authorized API key |
| `OGMEM_RUNTIME_VERSION` | Actual source commit and image digest; set for measured runs |
| `OGMEM_FLUSH_TIMEOUT_SECONDS` | `900`; extraction HTTP timeout |
| `OGMEM_COMPOSE_TOKEN_BUDGET` | `128000`; context budget passed to compose |
| `OGMEM_COMPOSE_TIMEOUT_SECONDS` | `300`; keep below the platform recall timeout of 330 seconds |

No write retries are performed: a transport timeout can leave the server working. Inspect the session before retrying; use a fresh run ID for a new experiment. This adapter does not claim durable exactly-once replay or provision users automatically.

## Run

From `memory_bench_platform/`, with a configured answer agent and judge:

```bash
export OGMEM_API_URL=http://127.0.0.1:8090
export OGMEM_RUNTIME_VERSION='<deployed commit and image digest>'
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw \
  --memory-backend ogmemory --memory-integration backend_direct \
  --data-path ../locomo_test/data/locomo_small.json \
  --run-id locomo-small-ogmemory-UNIQUE
```

Disable memory plugins/tools on the answering agent so it uses only the supplied evidence. The judge uses `LOCOMO_API_KEY`, `LOCOMO_BASE_URL`, and `LOCOMO_METRIC_MODEL`. Probe extraction, embedding, answer and judge endpoints before an expensive run.

This is the platform's session-based native protocol, not a reproduction of the OpenClaw `after_turn` protocol in locomo-test. Pin the deployed source and record the data hash, model settings, compose token budget and result coverage when comparing scores.

Backend-direct QA uses the shared platform prompt: answer from recalled evidence, allow reasonable supported inferences, and abstain only when no reasonable answer is supported. Compose controls retrieval internally; generic `node_limit` / `top_k` inputs are not applied.
