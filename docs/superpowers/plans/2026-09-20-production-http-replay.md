# Production HTTP Replay Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a benchmark entrypoint that streams OpenMem-style add/search JSONL, sends complete requests through a compatible Memory Skill, drains asynchronous writes, and archives request-correlated performance evidence without archiving content.

**Architecture:** A `production-http-replay` external Benchmark Skill owns dataset parsing and replay. Its Python runner invokes the selected Memory Skill through `run_memory_task`, writes a content-free workload result contract, and relies on the platform external-result importer for the final run summary. Platform changes stay limited to capability validation, runner environment injection, and generic import of the new result contract.

**Tech Stack:** Python 3.11, Pydantic v2, concurrent.futures, JSONL, pytest, YAML

**Spec:** `docs/superpowers/specs/2026-09-20-production-replay-instrumentation-design.md`

## Global Constraints

- `--data-path` points to a directory containing one add JSONL and one search JSONL.
- Each row contains an object-valued `request`; `response` is optional and never sent to the target.
- The runner reads files line by line and never persists `raw_request`.
- Add requests complete or reach terminal failure before the first search request starts.
- The runner uses `ingest`, `status`, and `recall` through a Memory Skill that declares `openmem-v1`.
- Empty `user_id` values become `replay-<run_id>`; non-empty values stay unchanged.
- A stable request ID uses operation, six-digit line number, and a canonical request SHA-256 prefix.
- Single-request failures remain in the workload and contribute to failure-rate metrics.
- Missing request correlation marks attribution `exploratory`.
- No artifact may contain messages, queries, memory content, credentials, private endpoints, or original identities.
- The macOS baseline is `memory_bench_platform: 320 passed, 9 /proc failures` and `locomo_test: 84 passed`.

## Review Focus

- Filenames that contain both `add` and `search`, uppercase suffixes, symlinks, or nested files must not cause ambiguous discovery; Task 1 tests exact regular-file matching.
- A blank line or JSON scalar must fail with file and line metadata without echoing content; Task 1 tests both cases.
- An add response that reports `ok/completed` needs no status poll, while `accepted/running` must poll to a terminal state; Task 2 tests both paths.
- Concurrent add completion order must not alter the barrier or request IDs; Task 3 checks that search starts after every add future and poll finish.
- A malformed external summary or missing request-event file must fail import instead of producing a zero-success report; Task 5 tests both failures.

---

### Task 1: Stream and validate production replay datasets

**Files:**
- Create: `memory_bench_platform/skills/benchmarks/production-http-replay/scripts/run_replay.py`
- Create: `memory_bench_platform/skills/benchmarks/production-http-replay/scripts/validate.py`
- Create: `memory_bench_platform/tests/test_production_replay.py`

**Interfaces:**
- Consumes: a directory path containing add/search JSONL files.
- Produces: `ReplayDataset`, `ReplayRecord`, `discover_dataset(path)`, `iter_replay_records(path, operation, run_id)`, and `validate_dataset(path)`.

- [ ] **Step 1: Write failing parser and discovery tests**

Load the hyphenated Skill script with `importlib.util`:

```python
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/benchmarks/production-http-replay/scripts/run_replay.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("production_replay_runner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_row(path: Path, request: dict, response: dict | None = None):
    path.write_text(
        json.dumps({"request": request, "response": response or {}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_discover_dataset_requires_one_regular_file_per_operation(tmp_path: Path):
    add = tmp_path / "openmem_add_sample.jsonl"
    search = tmp_path / "openmem_search_sample.jsonl"
    _write_row(add, {"user_id": "", "messages": []})
    _write_row(search, {"user_id": "", "query": "needle"})
    dataset = _module().discover_dataset(tmp_path)
    assert dataset.add_path == add
    assert dataset.search_path == search


def test_iter_records_preserves_payload_and_replaces_only_empty_user(tmp_path: Path):
    path = tmp_path / "openmem_add_sample.jsonl"
    _write_row(path, {"user_id": "", "messages": [{"role": "user", "content": "secret"}]})
    record = next(_module().iter_replay_records(path, "add", "run-7"))
    assert record.request["user_id"] == "replay-run-7"
    assert record.request["messages"][0]["content"] == "secret"
    assert record.request_id.startswith("add-000001-")
    assert record.payload_size_bytes > 0


def test_iter_records_reports_line_without_echoing_bad_content(tmp_path: Path):
    path = tmp_path / "openmem_search_sample.jsonl"
    path.write_text("\nnot-secret-valid-json\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        list(_module().iter_replay_records(path, "search", "run-7"))
    assert path.name in str(exc.value)
    assert "line 1" in str(exc.value)
    assert "not-secret-valid-json" not in str(exc.value)
```

Add tests for two add files, missing search, a JSON scalar row, nested files, symlinks, uppercase `.JSONL`, and a filename containing both operation names. Require a clear ambiguity error for every non-unique match.

- [ ] **Step 2: Run tests and confirm the module is missing**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_production_replay.py -v`

Expected: FAIL with `FileNotFoundError` for `run_replay.py`.

- [ ] **Step 3: Implement immutable data models and dataset discovery**

```python
@dataclass(frozen=True)
class ReplayDataset:
    add_path: Path
    search_path: Path


@dataclass(frozen=True)
class ReplayRecord:
    operation: Literal["add", "search"]
    line_number: int
    request_id: str
    request: dict[str, Any]
    payload_sha256: str
    payload_size_bytes: int
    top_level_fields: tuple[str, ...]
    baseline: dict[str, Any]
```

`discover_dataset()` must inspect direct child regular files only. Match case-insensitive `.jsonl` names that contain exactly one operation token delimited by `_`, `-`, or string boundaries. Reject symlinks and ambiguous candidates.

- [ ] **Step 4: Implement canonical hashing, identity replacement, and streaming errors**

```python
def _canonical_request_bytes(request: dict[str, Any]) -> bytes:
    return json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_id(operation: str, line_number: int, digest: str) -> str:
    return f"{operation}-{line_number:06d}-{digest[:12]}"
```

Compute the hash before replacing an empty `user_id`. Copy the request object before mutation. Extract only content-free baseline values such as response code, success, status, and result-list counts.

- [ ] **Step 5: Implement `validate.py` as a thin JSON CLI**

`validate_dataset()` must return:

```json
{
  "status": "ok",
  "source_path": "/path/to/directory",
  "add_file": "openmem_add_sample.jsonl",
  "search_file": "openmem_search_sample.jsonl",
  "add_count": 1,
  "search_count": 1,
  "request_shapes": {"add": [], "search": []}
}
```

The script accepts one directory argument, prints one JSON object to stdout, and sends validation failures to stderr with a nonzero exit status.

- [ ] **Step 6: Run focused tests**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_production_replay.py -v`

Expected: PASS.

- [ ] **Step 7: Commit the dataset layer**

```bash
git add memory_bench_platform/skills/benchmarks/production-http-replay/scripts memory_bench_platform/tests/test_production_replay.py
git commit -m "feat: parse production replay datasets"
```

### Task 2: Execute add, async drain, and search through Memory Skill

**Files:**
- Modify: `memory_bench_platform/skills/benchmarks/production-http-replay/scripts/run_replay.py`
- Modify: `memory_bench_platform/tests/test_production_replay.py`

**Interfaces:**
- Consumes: `ReplayRecord` from Task 1 and `run_memory_task(memory_id, MemoryTaskInput) -> MemoryTaskOutput`.
- Produces: `ReplayConfig`, `RequestEvent`, `ReplaySummary`, and `execute_replay(config, invoke_memory)`.

- [ ] **Step 1: Write failing lifecycle tests with a recording fake**

```python
@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    data = tmp_path / "dataset"
    data.mkdir()
    _write_row(
        data / "openmem_add_sample.jsonl",
        {"user_id": "", "conversation_id": "conv-1", "messages": []},
    )
    _write_row(
        data / "openmem_search_sample.jsonl",
        {"user_id": "", "query": "needle", "memory_limit_number": 5},
    )
    return data


def test_execute_replay_drains_add_before_search(dataset_dir: Path, tmp_path: Path):
    module = _module()
    calls: list[tuple[str, str]] = []
    polls: dict[str, int] = {}

    def invoke(memory_id, request):
        calls.append((request.action, request.task_id))
        if request.action == "ingest":
            return module.MemoryTaskOutput(
                status="ok", state="accepted",
                operation={"session_id": "session-1", "task_id": request.task_id},
                output={}, metrics=[], artifacts=[], error={},
            )
        if request.action == "status":
            polls[request.task_id] = polls.get(request.task_id, 0) + 1
            return module.MemoryTaskOutput(
                status="ok", state="completed",
                operation={"task_id": request.task_id}, output={}, metrics=[], artifacts=[], error={},
            )
        return module.MemoryTaskOutput(
            status="ok", state="completed", operation={},
            output={"count": 1, "memories": [], "evidence_text": ""},
            metrics=[], artifacts=[], error={},
        )

    summary = module.execute_replay(
        module.ReplayConfig(
            data_path=dataset_dir, output_dir=tmp_path / "out", run_id="run-1",
            memory_id="fake-memory", agent_id="generic-cli", poll_interval_seconds=0,
        ),
        invoke,
    )
    first_recall = next(index for index, item in enumerate(calls) if item[0] == "recall")
    assert all(action in {"ingest", "status"} for action, _ in calls[:first_recall])
    assert summary.dataset_state == "complete"
```

Add tests for completed ingest without polling, accepted ingest that reaches failed, status timeout, recall failure, request exception, exact `raw_request` equality, `source_protocol=openmem-v1`, stable `idempotency_key`, and `partially_written` after any terminal add failure.

- [ ] **Step 2: Run the lifecycle tests and confirm missing interfaces**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_production_replay.py -v`

Expected: FAIL because `ReplayConfig` and `execute_replay` do not exist.

- [ ] **Step 3: Implement replay configuration and content-free events**

```python
@dataclass(frozen=True)
class ReplayConfig:
    data_path: Path
    output_dir: Path
    run_id: str
    memory_id: str
    agent_id: str
    add_concurrency: int = 1
    search_concurrency: int = 1
    add_rate_per_second: float = 0.0
    search_rate_per_second: float = 0.0
    request_timeout_seconds: float = 120.0
    poll_interval_seconds: float = 1.0
    drain_timeout_seconds: float = 600.0


@dataclass(frozen=True)
class RequestEvent:
    request_id: str
    operation: str
    line_number: int
    ts: str
    payload_sha256: str
    payload_size_bytes: int
    top_level_fields: tuple[str, ...]
    duration_ms: float
    status: str
    state: str
    result_count: int | None
    response_size_bytes: int | None
    error_type: str | None
    error_message: str | None
```

Build `WorkflowRuntimeContext` from config and pass the complete copied request under `inputs.raw_request`. Use request ID as task ID and as part of the idempotency key.

The raw-protocol Memory Skill still follows `memory/1`: a successful ingest returns `operation.session_id`. An asynchronous add also returns `operation.task_id`, and later status calls receive that complete operation object.

- [ ] **Step 4: Implement terminal-state polling and the phase barrier**

Use `time.perf_counter()` for durations, `time.monotonic()` for deadlines, and `datetime.now(timezone.utc).isoformat()` for event timestamps. Poll only `accepted` or `running` add results. The status request inputs contain the prior operation, request ID, source protocol, and source operation. Stop on `completed`, `failed`, or drain deadline.

Execute every add record and finish all polling before calling `iter_replay_records()` for search. Convert exceptions to failed request events after sanitizing error type and message; do not include exception arguments that contain payload text.

- [ ] **Step 5: Run lifecycle tests**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_production_replay.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the replay lifecycle**

```bash
git add memory_bench_platform/skills/benchmarks/production-http-replay/scripts/run_replay.py memory_bench_platform/tests/test_production_replay.py
git commit -m "feat: replay add and search through memory skills"
```

### Task 3: Add bounded concurrency, pacing, summaries, and privacy checks

**Files:**
- Modify: `memory_bench_platform/skills/benchmarks/production-http-replay/scripts/run_replay.py`
- Modify: `memory_bench_platform/tests/test_production_replay.py`

**Interfaces:**
- Consumes: `execute_replay()` and `RequestEvent` from Task 2.
- Produces: `request_events.jsonl` and `production_replay_summary.json` under `ReplayConfig.output_dir`.

- [ ] **Step 1: Write failing concurrency and artifact tests**

Add tests that use threading events to hold two add calls open, prove configured concurrency never exceeds two, and assert the first recall starts after both add calls and polls finish.

Add a privacy test with sentinel strings in add content, search query, response memory, fake API key, and raised exception:

```python
def test_replay_artifacts_exclude_payload_and_secret_sentinels(
    dataset_dir: Path, tmp_path: Path
):
    module = _module()
    add_path = dataset_dir / "openmem_add_sample.jsonl"
    search_path = dataset_dir / "openmem_search_sample.jsonl"
    _write_row(
        add_path,
        {"user_id": "", "messages": [{"role": "user", "content": "MESSAGE_SENTINEL"}]},
        {"api_key": "API_KEY_SENTINEL"},
    )
    _write_row(
        search_path,
        {"user_id": "", "query": "QUERY_SENTINEL"},
        {"data": {"memory_detail_list": [{"content": "MEMORY_SENTINEL"}]}},
    )
    config = module.ReplayConfig(
        data_path=dataset_dir,
        output_dir=tmp_path / "out",
        run_id="run-private",
        memory_id="fake-memory",
        agent_id="generic-cli",
        poll_interval_seconds=0,
    )

    def invoke(memory_id, request):
        if request.action == "ingest":
            return module.MemoryTaskOutput(
                status="ok", state="completed", operation={"session_id": "session-1"},
                output={}, metrics=[], artifacts=[], error={},
            )
        return module.MemoryTaskOutput(
            status="ok", state="completed", operation={},
            output={"count": 1, "memories": [{"content": "MEMORY_SENTINEL"}], "evidence_text": ""},
            metrics=[], artifacts=[], error={},
        )

    summary = module.execute_replay(config, invoke)
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.output_dir.rglob("*")
        if path.is_file()
    )
    for sentinel in ("MESSAGE_SENTINEL", "QUERY_SENTINEL", "MEMORY_SENTINEL", "API_KEY_SENTINEL"):
        assert sentinel not in artifact_text
    assert summary.join_coverage["client_requests"] == 2
```

Add percentile tests with fixed durations, duplicate request-ID detection, zero/missing internal span coverage, and invalid concurrency/rate/timeout values.

- [ ] **Step 2: Run tests and confirm sequential or missing artifact failures**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_production_replay.py -v`

Expected: FAIL on concurrency and missing output artifacts.

- [ ] **Step 3: Implement bounded phase execution**

Use one `ThreadPoolExecutor(max_workers=phase_concurrency)` per phase. Submit records lazily with at most `phase_concurrency` outstanding futures so the runner does not materialize the dataset. A locked pacer reserves request start slots when the configured rate is greater than zero.

Collect events by input line number before writing output so artifact order remains deterministic despite completion order.

- [ ] **Step 4: Implement summary and artifact writers**

Write each `RequestEvent` with `json.dumps(asdict(event), sort_keys=True)` to `request_events.jsonl`. Write `production_replay_summary.json` with:

```json
{
  "schema": "production-replay-summary/1",
  "run_id": "run-1",
  "dataset_state": "complete",
  "model_mode": "real-model",
  "operations": {
    "add": {"count": 1, "success": 1, "success_rate": 1.0, "qps": 1.0, "p50_ms": 1.0, "p95_ms": 1.0, "p99_ms": 1.0, "max_ms": 1.0},
    "search": {"count": 1, "success": 1, "success_rate": 1.0, "qps": 1.0, "non_empty_rate": 1.0, "result_count_distribution": {"1": 1}}
  },
  "join_coverage": {"client_requests": 2, "matched_internal_traces": 0, "missing_internal_traces": 2, "duplicate_request_ids": 0, "unmatched_internal_traces": 0},
  "attribution_status": "exploratory"
}
```

The runner may import internal span JSONL from `MEMORY_BENCH_PERF_TRACE_PATH`; it must parse content-free correlation fields only. Set `attribution_status=validated` only when every unique client request matches an internal trace and there are no duplicates.

- [ ] **Step 5: Add the executable `main()`**

Read configuration from `DATA_PATH`, `OUTPUT_DIR`, `RUN_ID`, `AGENT_ID`, `MEMORY_BACKEND`, and these optional variables:

```text
MEMORY_BENCH_REPLAY_ADD_CONCURRENCY=1
MEMORY_BENCH_REPLAY_SEARCH_CONCURRENCY=1
MEMORY_BENCH_REPLAY_ADD_RATE=0
MEMORY_BENCH_REPLAY_SEARCH_RATE=0
MEMORY_BENCH_REPLAY_REQUEST_TIMEOUT_SECONDS=120
MEMORY_BENCH_REPLAY_POLL_INTERVAL_SECONDS=1
MEMORY_BENCH_REPLAY_DRAIN_TIMEOUT_SECONDS=600
MEMORY_BENCH_MODEL_MODE=real-model
```

Accept the four model modes defined by the workload-testing contract: `real-model`, `replay-zero-delay`, `replay-with-delay`, and `mock-fixed`. Write the selected mode and a content-free configuration snapshot to `run_config.json` before replay starts. Exit nonzero for configuration or dataset errors. Keep per-request target failures in the summary and exit zero after artifacts are written.

- [ ] **Step 6: Run focused tests and commit**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_production_replay.py -v`

Expected: PASS.

```bash
git add memory_bench_platform/skills/benchmarks/production-http-replay/scripts/run_replay.py memory_bench_platform/tests/test_production_replay.py
git commit -m "feat: archive production replay metrics"
```

### Task 4: Register the benchmark and enforce raw-protocol compatibility

**Files:**
- Create: `memory_bench_platform/skills/benchmarks/production-http-replay/manifest.yaml`
- Create: `memory_bench_platform/skills/benchmarks/production-http-replay/SKILL.md`
- Modify: `memory_bench_platform/memory_bench_platform/integration.py`
- Modify: `memory_bench_platform/memory_bench_platform/cli.py`
- Modify: `memory_bench_platform/tests/test_benchmark_skills.py`
- Modify: `memory_bench_platform/tests/test_integration_contract.py`
- Modify: `memory_bench_platform/tests/test_external_entrypoints.py`

**Interfaces:**
- Consumes: benchmark and memory manifests through `resolve_run_skill_bundle()`.
- Produces: external entrypoint `production-http-replay:replay`, compatibility field `capabilities.raw_request_protocols`, and runner env keys `MEMORY_BACKEND`, `MEMORY_INTEGRATION`, `RUN_DIR`.

- [ ] **Step 1: Write failing manifest, capability, and environment tests**

Manifest assertions:

```python
def test_production_http_replay_manifest_declares_raw_protocol_runner():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/production-http-replay/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["dataset"]["requires_data_path"] is True
    assert manifest["execution"]["entrypoints"]["replay"]["external_runner"] == "scripts/run_replay.py"
    assert manifest["requirements"]["memory"]["actions"] == ["ingest", "status", "recall"]
    assert manifest["requirements"]["memory"]["raw_request_protocols"] == ["openmem-v1"]
```

Create temporary manifests in `test_integration_contract.py` and assert `resolve_run_skill_bundle()` rejects a memory capability without `openmem-v1`, then accepts `capabilities.raw_request_protocols: [openmem-v1]`.

In the CLI external runner test, replace `execute_external_runner` with a capturing fake, replace `ResourceMonitor` with the existing test stub, and assert:

```python
assert env["MEMORY_BACKEND"] == "openmem-memory"
assert env["MEMORY_INTEGRATION"] == "backend_direct"
assert env["RUN_DIR"].endswith("/runs/run-production-replay")
```

- [ ] **Step 2: Run tests and confirm missing manifest/capability/env failures**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_benchmark_skills.py tests/test_integration_contract.py tests/test_external_entrypoints.py -v`

Expected: FAIL on the new benchmark and environment assertions.

- [ ] **Step 3: Add manifest and human-readable Skill**

Use `version_source: runtime_observed_only` for the benchmark tooling target. Set `entry.validator: scripts/validate.py`, `execution.mode: production_replay`, and the `replay` external entrypoint. Document this invocation:

```bash
memory-bench validate \
  --benchmark production-http-replay \
  --data-path /path/to/add-and-search-directory

memory-bench run \
  --benchmark production-http-replay \
  --entrypoint replay \
  --agent generic-cli \
  --memory-backend <openmem-v1-compatible-memory-skill> \
  --data-path /path/to/add-and-search-directory
```

- [ ] **Step 4: Enforce raw request protocol compatibility**

In `_validate_run_skill_bundle()`, compare:

```python
required_protocols = {
    str(item)
    for item in bundle.benchmark.requirements.get("memory", {}).get("raw_request_protocols", [])
}
available_protocols = {
    str(item)
    for item in bundle.memory.capabilities.get("raw_request_protocols", [])
}
missing_protocols = sorted(required_protocols - available_protocols)
if missing_protocols:
    raise ValueError(
        f"memory {bundle.memory.id} does not support raw request protocols: "
        + ", ".join(missing_protocols)
    )
```

Also verify every action in `requirements.memory.actions` exists in memory capabilities so the external path receives the same early checks as native scenarios.

- [ ] **Step 5: Inject selected runtime information into external runners**

Extend the external runner environment in `cli.py`:

```python
"MEMORY_BACKEND": str(run_contract["selection"].get("memory_id") or ""),
"MEMORY_INTEGRATION": args.memory_integration,
"RUN_DIR": str(run_dir),
```

Do not inject secrets or serialize the full run contract into an environment variable.

- [ ] **Step 6: Run focused tests and commit**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_benchmark_skills.py tests/test_integration_contract.py tests/test_external_entrypoints.py -v`

Expected: PASS. The resource-monitor stub keeps this focused test independent of Linux `/proc`.

```bash
git add memory_bench_platform/skills/benchmarks/production-http-replay memory_bench_platform/memory_bench_platform/integration.py memory_bench_platform/memory_bench_platform/cli.py memory_bench_platform/tests/test_benchmark_skills.py memory_bench_platform/tests/test_integration_contract.py memory_bench_platform/tests/test_external_entrypoints.py
git commit -m "feat: register production replay benchmark"
```

### Task 5: Import production replay results into platform reports

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/external_report_import.py`
- Modify: `memory_bench_platform/tests/test_external_report_import.py`

**Interfaces:**
- Consumes: `production_replay_summary.json` and `request_events.jsonl` from Task 3.
- Produces: the existing external import envelope with `summary`, `case_results`, and `benchmark_diagnostics`.

- [ ] **Step 1: Write failing importer tests**

```python
def test_import_external_result_reads_production_replay_contract(tmp_path: Path):
    (tmp_path / "production_replay_summary.json").write_text(
        json.dumps({
            "schema": "production-replay-summary/1",
            "run_id": "run-1",
            "dataset_state": "partially_written",
            "operations": {
                "add": {"count": 2, "success": 1, "success_rate": 0.5},
                "search": {"count": 1, "success": 1, "success_rate": 1.0, "non_empty_rate": 1.0},
            },
            "join_coverage": {"client_requests": 3, "missing_internal_traces": 1},
            "attribution_status": "exploratory",
        }),
        encoding="utf-8",
    )
    (tmp_path / "request_events.jsonl").write_text(
        "\n".join([
            json.dumps({"request_id": "add-1", "operation": "add", "status": "ok", "state": "completed"}),
            json.dumps({"request_id": "add-2", "operation": "add", "status": "failed", "state": "failed", "error_type": "TimeoutError"}),
            json.dumps({"request_id": "search-1", "operation": "search", "status": "ok", "state": "completed", "result_count": 2}),
        ]) + "\n",
        encoding="utf-8",
    )
    imported = import_external_result(tmp_path)
    assert imported["source"] == "production_http_replay"
    assert imported["summary"]["total_questions"] == 3
    assert imported["summary"]["total_correct"] == 2
    assert imported["summary"]["run_validity"]["valid"] is False
    assert imported["benchmark_diagnostics"]["dataset_state"] == "partially_written"
```

Add tests for missing events, malformed JSONL, duplicate request IDs, unknown summary schema, and event rows containing forbidden `raw_request`, `messages`, `query`, or `memories` fields.

- [ ] **Step 2: Run importer tests and confirm the current LoCoMo-only failure**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_external_report_import.py -v`

Expected: FAIL because the importer searches for LoCoMo CSV output.

- [ ] **Step 3: Add contract dispatch and strict event loading**

At the start of `import_external_result()`:

```python
production_summary = run_dir / "production_replay_summary.json"
if production_summary.is_file():
    return _import_production_replay(run_dir, production_summary)
```

Require `schema == "production-replay-summary/1"`, one event per request ID, allowed content-free keys only, and counts that match summary operation totals.

Map each event to a case result:

```python
{
    "case_id": event["request_id"],
    "passed": event["status"] == "ok" and event["state"] == "completed",
    "label": "passed" if passed else "failed",
    "question": event["operation"],
    "expected": "successful request",
    "response": event["state"],
    "category": event["operation"],
    "reasoning": event.get("error_type", ""),
}
```

Set run validity false when the dataset is partially written, attribution is exploratory, event counts mismatch, or duplicates exist. Preserve workload latency and correctness metrics under `benchmark_diagnostics`.

- [ ] **Step 4: Run existing and new importer tests**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_external_report_import.py tests/test_external_entrypoints.py -v`

Expected: new importer tests pass; existing external entrypoint behavior remains unchanged apart from known macOS `/proc` failures.

- [ ] **Step 5: Commit the importer**

```bash
git add memory_bench_platform/memory_bench_platform/external_report_import.py memory_bench_platform/tests/test_external_report_import.py
git commit -m "feat: import production replay results"
```

### Task 6: Validate the sample shape, CLI flow, and documentation

**Files:**
- Modify: `memory_bench_platform/tests/test_validate_cli.py`
- Modify: `memory_bench_platform/tests/test_external_entrypoints.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: the registered Benchmark Skill, runner outputs, and generic importer.
- Produces: documented validate/run commands and an end-to-end platform archive test.

- [ ] **Step 1: Add failing CLI validation and archive tests**

Create tiny add/search JSONL files in `tmp_path`. Assert `memory-bench validate` reports both counts. For the archive test, monkeypatch the resource monitor and `execute_external_runner`; have the fake runner write a valid summary and two request events into `OUTPUT_DIR`. Assert the final run contains:

```text
records/external_entrypoint.json
reports/summary.json
reports/case_results.json
reports/analysis.json
reports/run_report.html
```

Assert summary status is `partial` when attribution is exploratory and `passed` when all request IDs match internal spans.

- [ ] **Step 2: Run tests and confirm missing documentation/archive behavior**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_validate_cli.py tests/test_external_entrypoints.py -v`

Expected: FAIL on the new production replay assertions.

- [ ] **Step 3: Complete CLI archive wiring without benchmark-ID branches**

Use the importer source and `run_validity` returned in Task 5. Keep `cli.py` generic: do not add `if benchmark_id == "production-http-replay"`. Put workload-specific metrics in `benchmark_diagnostics`, then allow `analyze_run()` and the HTML reporter to render the existing diagnostic section.

- [ ] **Step 4: Document validation, execution, outputs, and limitations**

Add a README section containing the commands from Task 4, environment settings from Task 3, required Memory Skill capability, output files, privacy behavior, and the add-drain-search ordering limitation. State that separate input files cannot reconstruct production read/write interleaving.

- [ ] **Step 5: Run against the provided sanitized sample directory in validation mode**

Run:

```bash
cd memory_bench_platform
/opt/homebrew/bin/python3.11 -m memory_bench_platform.cli validate \
  --benchmark production-http-replay \
  --data-path '/Users/fang/Downloads/add和search案例'
```

Expected: status `ok`, add count `1000`, search count `1000`; output contains filenames and shape statistics but no message or query text.

- [ ] **Step 6: Run focused and regression verification**

Run:

```bash
cd memory_bench_platform
/opt/homebrew/bin/python3.11 -m pytest \
  tests/test_production_replay.py \
  tests/test_benchmark_skills.py \
  tests/test_integration_contract.py \
  tests/test_external_report_import.py \
  tests/test_external_entrypoints.py \
  tests/test_validate_cli.py \
  tests/test_docs_smoke.py -v
```

Expected: all new tests pass; macOS may retain only existing `/proc` failures in CLI tests.

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest -q`

Expected: no failures beyond the 9 existing macOS `/proc` failures.

Run: `cd locomo_test && /opt/homebrew/bin/python3.11 -m pytest -q`

Expected: `84 passed`.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 7: Commit documentation and end-to-end coverage**

```bash
git add README.md memory_bench_platform/tests/test_validate_cli.py memory_bench_platform/tests/test_external_entrypoints.py
git commit -m "docs: add production replay workflow"
```
