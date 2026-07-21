# OpenViking Session API Ingest Design

## Goal

Replace the OpenViking memory runner's `ov add-memory` ingest path with the public session HTTP API so each ingest returns a concrete `task_id` that the native `poll` operator can track to a terminal state.

## Scope

This change is limited to the OpenViking memory runner and its tests.

- Change `memory.ingest` from the `ov add-memory` CLI to session HTTP APIs.
- Keep `memory.status` on `GET /api/v1/tasks/{task_id}`.
- Keep `memory.recall` on the existing `ov find` CLI path.
- Remove the ingest path's fallback to global `ov wait` by requiring a task ID from a successful commit.
- Do not change workflow scheduling, poll condition evaluation, benchmark case shape, or recall result filtering.

## API Flow

For each ingest request:

1. Resolve a session ID.
   - Use non-empty `inputs.session_id` when supplied.
   - Otherwise derive a stable ID from `request.idempotency_key`.
   - If the idempotency key is absent, derive it from the request task ID.
   - The generated ID uses a fixed `mbp-` prefix plus a SHA-256 digest fragment, so retries address the same session without exposing memory content.
2. Add the memory text as a user message:

   ```http
   POST /api/v1/sessions/{session_id}/messages
   Content-Type: application/json

   {"role":"user","content":"..."}
   ```

   The OpenViking server auto-creates a missing session.
3. Commit the session with no retained live messages:

   ```http
   POST /api/v1/sessions/{session_id}/commit
   Content-Type: application/json

   {"keep_recent_count":0}
   ```
4. Read `task_id`, `session_id`, and `archive_uri` from the commit result.
5. Return `state=accepted` and an operation containing:

   ```json
   {
     "task_id": "...",
     "resource_id": "<session_id>",
     "session_id": "<session_id>",
     "archive_uri": "...",
     "status_probe": "task"
   }
   ```

The existing poll memory probe then calls `GET /api/v1/tasks/{task_id}` until the task reaches `completed` or `failed`.

## Authentication And Identity

Both session requests reuse the existing OpenViking request headers:

- `X-API-Key`
- `X-OpenViking-Account`
- `X-OpenViking-User`
- `X-OpenViking-Agent`

The runner does not include secrets or raw content in error messages. Existing sanitization remains responsible for redacting the API key and ingest content.

## Response Handling

The HTTP helper accepts OpenViking's standard response envelope and returns its `result` value.

- Non-2xx responses fail the memory operation.
- Invalid JSON fails the memory operation.
- A successful commit without a non-empty `task_id` fails with `missing_task_id`.
- The runner does not fall back to `ov wait`, task listing, or global queue state.
- `resource_id` defaults to the resolved session ID when the response omits `session_id`.

This makes completion conservative: an ingest is never reported trackable unless it has an operation-specific task ID.

## Compatibility

The change targets the OpenViking `0.3.25.dev2` session API used on openGauss237. Its commit endpoint documents and returns a task ID for background Phase 2 extraction. The request and response handling remains envelope-based so later compatible versions can add fields without breaking the runner.

## Test Strategy

Use TDD in `tests/test_openviking_memory_runner.py`.

1. Replace the CLI-ingest expectation with a test that verifies both HTTP requests, identity headers, deterministic session ID, accepted state, and returned task operation.
2. Add a test proving an explicit `inputs.session_id` is used unchanged.
3. Add a regression test proving a commit response without `task_id` fails and never reports `accepted` or `completed`.
4. Keep the existing task-status API, fallback-status, recall normalization, and secret-redaction tests unless their ingest assumptions must be updated.
5. Run the targeted memory, poll, input-reference, workflow, and integration tests locally and in a Linux container on openGauss237.
6. Run the real native workflow against a clean OpenViking service and gate on:
   - ingest operation has a non-empty task ID;
   - poll uses `status_probe=task` and observes the same task ID;
   - backend task reaches `completed` before recall;
   - recall contains the newly written fact;
   - the builtin judge passes.

## Non-Goals

- Replacing `ov find` with the search HTTP API.
- Filtering generic recall overview nodes.
- Changing the benchmark's `source_kind` propagation.
- Making `openclaw-openviking:0704` a Python 3.11 benchmark runner image.
- Refactoring unrelated workflow or integration code.
