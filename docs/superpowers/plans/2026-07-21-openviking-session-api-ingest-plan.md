# OpenViking Session API Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make native `memory.ingest` return an operation-specific OpenViking `task_id` by using the session message and commit HTTP APIs.

**Architecture:** The OpenViking memory runner will derive or accept a stable session ID, POST the memory text to that session, POST a commit, and return the commit task as the workflow operation. Existing task polling and CLI-based recall remain unchanged.

**Tech Stack:** Python 3.11+, `urllib.request`, pytest, OpenViking HTTP API `0.3.25.dev2`, Docker on openGauss237.

## Global Constraints

- Modify only the existing OpenViking runner and its focused tests.
- Preserve existing OpenViking account, user, agent, and API-key headers.
- Do not log or return API keys or ingest content.
- Keep recall on `ov find`.
- Do not use global `ov wait` as a successful ingest completion contract.
- Do not commit implementation files because both target files pre-exist as untracked user work in this dirty worktree.
- Preserve all unrelated worktree changes.

---

### Task 1: Drive ingest through the session HTTP API

**Files:**
- Modify: `memory_bench_platform/tests/test_openviking_memory_runner.py`
- Modify: `memory_bench_platform/skills/memories/openviking/scripts/run_operation.py`

**Interfaces:**
- Consumes: `run_operation(request, environ=..., urlopen=...)` and the existing `_request_headers(environment)` helper.
- Produces: `_run_ingest(request, inputs, environment, urlopen, secrets)` returning a `MemoryTaskOutput`-compatible dictionary whose operation has `task_id`, `resource_id`, `session_id`, `archive_uri`, and `status_probe="task"`.

- [ ] **Step 1: Replace the CLI-ingest test with a failing session-API test**

  Build two fake HTTP responses, capture both requests, and assert:

  ```python
  request = _request("ingest", {"content": content})
  expected_session_id = "mbp-" + hashlib.sha256(
      request["idempotency_key"].encode("utf-8")
  ).hexdigest()[:24]

  result = runner.run_operation(
      request,
      environ=_environment(),
      command_runner=lambda *args, **kwargs: (_ for _ in ()).throw(
          AssertionError("ingest must not invoke ov")
      ),
      urlopen=fake_urlopen,
  )

  assert calls[0]["url"].endswith(
      f"/api/v1/sessions/{expected_session_id}/messages"
  )
  assert calls[0]["body"] == {"role": "user", "content": content}
  assert calls[1]["url"].endswith(
      f"/api/v1/sessions/{expected_session_id}/commit"
  )
  assert calls[1]["body"] == {"keep_recent_count": 0}
  assert result["operation"] == {
      "task_id": "task-1",
      "resource_id": expected_session_id,
      "session_id": expected_session_id,
      "archive_uri": f"viking://session/{expected_session_id}/history/archive_001",
      "status_probe": "task",
  }
  ```

- [ ] **Step 2: Run the focused test and verify RED**

  Run:

  ```bash
  cd memory_bench_platform
  PYTHONPATH=. python3 -m pytest -q \
    tests/test_openviking_memory_runner.py::test_ingest_uses_session_api_and_returns_task_operation
  ```

  Expected: FAIL because the current runner invokes `ov add-memory` and never calls `fake_urlopen`.

- [ ] **Step 3: Implement the minimal session API ingest path**

  In `run_operation.py`:

  ```python
  import hashlib

  def _session_id(request: dict[str, Any], inputs: dict[str, Any]) -> str:
      seed = str(request.get("idempotency_key") or request.get("task_id") or "").strip()
      if not seed:
          raise ValueError("memory.ingest requires idempotency_key or task_id")
      digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
      return f"mbp-{digest}"

  def _post_json(
      url: str,
      body: dict[str, Any],
      environment: dict[str, str],
      urlopen: Callable[..., Any],
      timeout: float,
  ) -> dict[str, Any]:
      headers = _request_headers(environment)
      headers["Content-Type"] = "application/json"
      request = urllib.request.Request(
          url,
          data=json.dumps(body).encode("utf-8"),
          headers=headers,
          method="POST",
      )
      with urlopen(request, timeout=timeout) as response:
          payload = json.loads(response.read().decode("utf-8") or "{}")
      result = payload.get("result", payload) if isinstance(payload, dict) else {}
      if not isinstance(result, dict):
          raise ValueError("OpenViking API returned an invalid result")
      return result
  ```

  Change the ingest dispatch and implementation to POST the message and commit, then return the commit task operation. Do not change recall or task-status behavior.

- [ ] **Step 4: Run the focused test and verify GREEN**

  Run the Step 2 command again.

  Expected: `1 passed`.

---

### Task 2: Make session choice and missing-task behavior explicit

**Files:**
- Modify: `memory_bench_platform/tests/test_openviking_memory_runner.py`
- Modify: `memory_bench_platform/skills/memories/openviking/scripts/run_operation.py`

**Interfaces:**
- Consumes: `_session_id(...)` and `_run_ingest(...)` from Task 1.
- Produces: explicit session override support and a `missing_task_id` failure result.

- [ ] **Step 1: Add a failing explicit-session test**

  ```python
  result = runner.run_operation(
      _request("ingest", {"content": "remember", "session_id": "session-explicit"}),
      environ=_environment(),
      urlopen=fake_urlopen,
  )

  assert calls[0]["url"].endswith("/api/v1/sessions/session-explicit/messages")
  assert calls[1]["url"].endswith("/api/v1/sessions/session-explicit/commit")
  assert result["operation"]["session_id"] == "session-explicit"
  ```

- [ ] **Step 2: Run the explicit-session test and verify RED**

  Run:

  ```bash
  cd memory_bench_platform
  PYTHONPATH=. python3 -m pytest -q \
    tests/test_openviking_memory_runner.py::test_ingest_uses_explicit_session_id
  ```

  Expected: FAIL until `_session_id` honors `inputs.session_id`.

- [ ] **Step 3: Implement explicit-session support and verify GREEN**

  Add this branch before deriving the digest:

  ```python
  explicit = str(inputs.get("session_id") or "").strip()
  if explicit:
      return explicit
  ```

  Then run the Step 2 command again.

  Expected: `1 passed`.

- [ ] **Step 4: Add a failing missing-task regression test**

  Make the commit response contain `status="accepted"` and `archived=true` but omit `task_id`, then assert:

  ```python
  assert result["status"] == "failed"
  assert result["state"] == "failed"
  assert result["operation"] == {}
  assert result["error"]["type"] == "missing_task_id"
  ```

- [ ] **Step 5: Run the missing-task test and verify RED**

  Run:

  ```bash
  cd memory_bench_platform
  PYTHONPATH=. python3 -m pytest -q \
    tests/test_openviking_memory_runner.py::test_ingest_fails_when_commit_omits_task_id
  ```

  Expected: FAIL because the Task 1 implementation still constructs an accepted operation with an empty task ID.

- [ ] **Step 6: Reject a missing task ID and verify GREEN**

  Add:

  ```python
  task_id = _find_first_string(commit_result, {"task_id", "taskId"})
  if not task_id:
      return _failed_result(
          "missing_task_id",
          "OpenViking session commit did not return a task_id",
          secrets,
      )
  ```

  Run the Step 5 command again.

  Expected: `1 passed`.

- [ ] **Step 7: Migrate the existing ingest-error redaction test to HTTP**

  Replace its CLI failure with a `urlopen` callable that raises an exception containing the ingest content and API key. Keep the existing assertions that both values are absent from serialized output and `[REDACTED]` is present.

- [ ] **Step 8: Run the complete runner test file**

  ```bash
  cd memory_bench_platform
  PYTHONPATH=. python3 -m pytest -q tests/test_openviking_memory_runner.py
  ```

  Expected: all tests pass.

---

### Task 3: Verify workflow regressions locally

**Files:**
- Verify only; no additional source files.

**Interfaces:**
- Consumes: the updated OpenViking runner.
- Produces: evidence that memory, poll, references, and workflow execution still pass.

- [ ] **Step 1: Compile the changed runner**

  ```bash
  python3 -m py_compile \
    memory_bench_platform/skills/memories/openviking/scripts/run_operation.py
  ```

  Expected: exit 0.

- [ ] **Step 2: Run the targeted regression set**

  ```bash
  cd memory_bench_platform
  PYTHONPATH=. python3 -m pytest -q \
    tests/test_workflow_inputs.py \
    tests/test_workflow_poll.py \
    tests/test_memory_runner.py \
    tests/test_openviking_memory_runner.py \
    tests/test_ovtest_memory_case_source.py \
    tests/test_workflow_executor.py \
    tests/test_run_with_builder.py \
    tests/test_integration_contract.py
  ```

  Expected: all selected tests pass with no warnings or errors attributable to this change.

- [ ] **Step 3: Inspect the surgical diff**

  ```bash
  git diff --check
  git diff -- \
    memory_bench_platform/skills/memories/openviking/scripts/run_operation.py \
    memory_bench_platform/tests/test_openviking_memory_runner.py
  ```

  Expected: only the session-API ingest implementation and focused tests changed.

---

### Task 4: Verify the real native workflow on openGauss237

**Files:**
- Create remotely under `/home/fangt`: a timestamped result directory containing source snapshot, logs, commands, evidence, and report.
- Do not modify additional repository files.

**Interfaces:**
- Consumes: the updated dirty-worktree snapshot, OpenViking `0.3.25.dev2`, configured real LLM and embedding services.
- Produces: a real ingest operation with task ID and an evidence-backed pass/fail report.

- [ ] **Step 1: Copy the worktree snapshot to a new result directory**

  ```bash
  stamp=$(date +%Y%m%d-%H%M%S)
  result=/home/fangt/native-memory-session-api-$stamp
  ssh openGauss237 "mkdir -p '$result/src' '$result/logs'"
  rsync -a --delete --exclude .git \
    /Users/fang/Documents/MemoryWorkloadTest/.worktrees/native-memory-workflow/ \
    openGauss237:"$result/src/"
  ```

- [ ] **Step 2: Run the Linux targeted regression in a compatible runner container**

  Start `openviking:0617`, mount `$result/src`, install pytest, and execute the exact Task 3 regression command. Save stdout and stderr under `$result/logs`.

- [ ] **Step 3: Start a clean OpenViking server and run the native workflow**

  Use the same real-model configuration as the prior validated run, with a new account/user/agent identity and clean OpenViking data directory. Run:

  ```bash
  cd /workspace/memory_bench_platform
  PYTHONPATH=. python3 -m memory_bench_platform.cli run \
    --benchmark ovtest-memory \
    --agent generic-cli \
    --memory-backend openviking \
    --run-id "ovtest-memory-session-api-$stamp"
  ```

  Save platform stdout/stderr, OpenViking logs, and the complete run directory.

- [ ] **Step 4: Gate on operation-specific completion**

  Parse step artifacts and assert:

  ```python
  assert ingest_operation["task_id"]
  assert ingest_operation["status_probe"] == "task"
  assert poll_probe_operation["task_id"] == ingest_operation["task_id"]
  assert poll_last_probe["state"] == "completed"
  assert poll_count >= 1
  ```

- [ ] **Step 5: Gate on read-after-write quality**

  Assert:

  ```python
  assert "For systems programming I prefer Go over Python." in recall_evidence
  assert "For systems programming I prefer Go over Python." in agent_answer
  assert judge_result["passed"] is True
  assert case_result["passed"] is True
  ```

  If operation-specific completion passes but recall still misses, report it separately as a recall/indexing issue rather than a poll issue.

- [ ] **Step 6: Write the remote report and clean up**

  Store `TEST_REPORT.md`, `COMMANDS.md`, `verification.json`, security scan results, and all raw logs beneath `$result`. Remove temporary containers and secret-bearing environment files, then verify no API key pattern is present in the result directory.

---

### Task 5: Final verification and handoff

**Files:**
- Verify: the two changed implementation/test files and the remote result directory.

**Interfaces:**
- Consumes: local and remote verification evidence.
- Produces: final status without committing the user's pre-existing untracked implementation files.

- [ ] **Step 1: Re-run the complete local targeted test command from Task 3**

  Expected: exit 0 and zero failures.

- [ ] **Step 2: Re-read the remote verification JSON and report**

  Confirm every claimed pass/fail has a corresponding artifact path and command log.

- [ ] **Step 3: Confirm worktree scope**

  ```bash
  git status --short --branch
  git diff --check
  ```

  Expected: no unrelated file changed by this implementation; pre-existing dirty files remain preserved.
