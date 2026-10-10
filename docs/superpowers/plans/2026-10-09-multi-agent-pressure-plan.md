# Multi-Agent Memory Pressure Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run one deployment-bound multi-user pressure test across multiple Agent–memory combinations, with optional shared memory, scoped preparation, route/isolation proof, sustained search load, and trustworthy per-target reports.

**Architecture:** Extend the existing multi-user replay scheduler behind a new deployment pressure route. Agent Skills implement one `openmem-agent/1` replay interface; the coordinator owns user/target routing, lifecycle, immutable input snapshots, scheduling, and measurement. Keep the existing `--workload-config` route and `multi-user-report/1` format intact; pressure runs emit their own report schema.

**Tech Stack:** Python >=3.11, Pydantic >=2.7, PyYAML >=6.0.1, Docker Compose v2 CLI, pytest >=8.2.

**Spec:** `docs/superpowers/specs/2026-10-09-compose-deployment-design.md`

**Prerequisite:** Complete `docs/superpowers/plans/2026-10-09-compose-deploy-and-run-plan.md` through its completion gate.

## Global Constraints

- Preserve the existing manual `--workload-config` route and its report schema.
- A pressure target is one `agent_plugin` combination; a target routes through its Agent to the configured physical memory instance.
- Require `AgentManifest.capabilities.multi_user_replay.supported: true`, `entry.multi_user_runner`, and plugin `flush`/`wait_settle` with exact evidence; require `MemoryPluginManifest.capabilities.shared_instance_scoped: true` for shared physical memory. Do not infer these from an image.
- One user has at most one request in flight; `max_workers` and each target's `max_inflight` both apply.
- Deploy seed covers only user-independent baseline; `user_seed.enabled` defaults false and run-time user seed is outside measured traffic.
- `finite_replay` is the default; `steady_search` repeats search only after finite add and exact drain, with ramp and steady windows.
- No raw prompts, responses, secrets, or private endpoints appear in durable reports.
- No product code is to be written before this plan is reviewed and an execution method is chosen.

## Review Focus

- Two users on one target use the same `agent_id`: reject during preflight, before any Docker operation (Task 1).
- A generic Agent Skill declares replay support but lacks a runnable entrypoint: reject rather than falling back to OpenClaw (Task 2).
- The configured Agent endpoint is healthy but its plugin actually writes to another memory container: route probe fails before load (Task 3).
- The source JSONL changes after preflight: every user reads the same immutable snapshot and the report fingerprint matches it (Task 3).
- A steady-search target reaches its in-flight cap: report missed planned requests and queue delay; do not grow an unbounded backlog (Task 4).

---

### Task 1: Pressure schema, references, and capability preflight

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/pressure_config.py`
- Create: `memory_bench_platform/tests/test_pressure_config.py`
- Modify: `memory_bench_platform/memory_bench_platform/deployment_config.py`
- Modify: `memory_bench_platform/memory_bench_platform/deployment_lifecycle.py`

**Interfaces:**
- Consumes: `DeploymentConfig`, `DeploymentState`, and `load_deployment` from the prerequisite plan.
- Produces: `resolve_pressure_test(config: DeploymentConfig, state: DeploymentState, test_id: str) -> PressureConfig` and `validate_pressure_capabilities(config: DeploymentConfig) -> None`.

- [ ] **Step 1: Write failing schema tests.** Assert targets resolve combination→Agent Skill/Memory Plugin Skill/physical memory instance; reject duplicate users, unknown workload/target, duplicate `agent_id` within one target, `backend_direct` target, missing `seed_file` when enabled, invalid rates/windows/thresholds, unsupported Skill actions, and shared plugin lifecycle without scoped capability. Assert all failures occur before the recording Docker fake sees a command.

  ```python
  with pytest.raises(ValueError, match="agent_id"):
      validate_pressure_capabilities(config_with_duplicate_agent_ids)
  assert docker.calls == []
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_pressure_config.py -q`; expect failures.
- [ ] **Step 3: Implement the pressure models and preflight.** Resolve paths relative to deployment YAML. Validate the complete optional `pressure_tests` section at deploy time, including the exact Agent/plugin capability keys in Global Constraints, replacing the prerequisite plan's temporary rejection. Preserve `--workload-config`'s existing schema and parser.
- [ ] **Step 4: Verify green.** Run the focused test module and deployment config tests; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit pressure parsing and preflight.

### Task 2: Agent replay interface and scoped plugin lifecycle

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/agent_replay.py`
- Create: `memory_bench_platform/skills/agents/openclaw/scripts/run_multi_user.py`
- Create: `memory_bench_platform/tests/test_agent_replay.py`
- Modify: `memory_bench_platform/memory_bench_platform/manifests.py`
- Modify: `memory_bench_platform/memory_bench_platform/integration.py`
- Modify: `memory_bench_platform/skills/agents/openclaw/manifest.yaml`

**Interfaces:**
- Produces: `AgentReplayInput` (`operation: Literal["add", "search"]`, `request: dict[str, object]`, `request_id: str`, `agent_id: str`, `user_id: str`, `session_id: str`, `target_id: str`) and `AgentReplayOutput` (`status: Literal["ok", "failed"]`, `request_id: str`, `answer_or_results: str | list[dict] | None`, `session_handle: dict[str, object] | None`).
- Produces: `run_agent_replay(skill_id: str, request: AgentReplayInput, environment: dict[str, str], timeout_seconds: float) -> AgentReplayOutput`; Skill manifest field `entry.multi_user_runner` is an argv-safe local script path.
- Consumes: existing `integration.run_agent_task`, `integration.run_memory_plugin_task`, and `WorkflowRuntimeContext`.

- [ ] **Step 1: Write failing interface tests.** A fake second Agent Skill runner receives the same structured input and returns a validated output; OpenClaw maps add history/search query using its current semantics; unknown response fields and mismatched `request_id` fail. Plugin task context uses the actual Agent Skill ID, unique target namespace, and target-specific environment; missing exact flush/wait evidence fails.

  ```python
  result = run_agent_replay("fake-agent", request, env, 30)
  assert result.request_id == request.request_id
  assert result.session_handle["session_id"] == "session-a"
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_agent_replay.py -q`; expect failures.
- [ ] **Step 3: Implement interface, OpenClaw adapter, and lifecycle routing.** Implement the OpenClaw conversion in the new Skill entrypoint for deployment pressure calls and declare its Agent replay capability; leave legacy `multi_user_replay.py` behavior unchanged. Validate manifest capability plus runnable entrypoint before calls; keep per-target idempotency keys and plugin snapshot directories separate. Do not mark an existing Memory Plugin as shared-instance scoped unless its lifecycle behavior is verified.
- [ ] **Step 4: Verify green and regressions.** Run `test_agent_replay.py`, `test_agent_skills.py`, and `test_multi_user_replay.py`; expect all to pass.
- [ ] **Step 5: Commit.** Commit Agent adapter and lifecycle changes.

### Task 3: Bound target routing, isolation, user seed, and frozen input

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/pressure_runtime.py`
- Create: `memory_bench_platform/tests/test_pressure_runtime.py`

**Interfaces:**
- Consumes: `PressureConfig` from Task 1 and `run_agent_replay` from Task 2.
- Produces: `prepare_targets(config: PressureConfig, run_id: str, deployment: DeploymentState, docker: DockerClient) -> PreparedPressureRun` and `snapshot_pressure_inputs(config: PressureConfig, directory: Path) -> PressureConfig`.

- [ ] **Step 1: Write failing preparation tests.** Verify each Agent gets only its own environment and recorded endpoint even when parent process variables conflict. Probe stdin/output uses `multi-user-isolation-probe/2` and checks container ID, request IDs, scoped write/read and cross-target negatives; wrong-memory success JSON is rejected. With `user_seed.enabled`, write each workload's `seed_file` through that user's Agent identity, wait for exact plugin readiness, and exclude those events from measured counters; one user seed failure excludes that user. Mutate source JSONL after snapshot and assert all users still see original content/fingerprint.

  ```python
  prepared = prepare_targets(config, "run-a", deployment, docker)
  assert prepared.targets["claw-mem0"].memory_instance_id == "memory-a"
  assert prepared.seed_events[0]["measured"] is False
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_pressure_runtime.py -q`; expect failures.
- [ ] **Step 3: Implement preparation and immutable snapshot.** Reuse existing `identity(run_id, user.id)` and snapshot privacy patterns; include seed files in the snapshot and resolve expected results before target writes. Run probe and optional warmup before the shared measurement start; preserve partial reports and finalize every prepared plugin after failure/interruption.
- [ ] **Step 4: Verify green.** Run focused runtime tests and `test_multi_user_replay.py`; expect all to pass.
- [ ] **Step 5: Commit.** Commit preparation and snapshot code.

### Task 4: Multi-target finite replay and bounded steady search

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/pressure_scheduler.py`
- Create: `memory_bench_platform/tests/test_pressure_scheduler.py`

**Interfaces:**
- Consumes: `PreparedPressureRun` from Task 3 and the Agent replay interface from Task 2.
- Produces: `run_pressure_phases(prepared: PreparedPressureRun, event_sink: Callable[[dict], None], probe: Callable[[str], dict], clock: Clock) -> PressurePhasesResult`; `Clock` supplies `monotonic()` and `sleep(seconds)` for deterministic tests.

- [ ] **Step 1: Write failing scheduler tests using a fake monotonic clock.** Assert finite add→exact drain→search barrier across all ready targets; one in-flight request per user, global/target limits, target fairness, partial failure continuation, interrupt status 130, and no extra post-final-request sleep. For `steady_search`, assert ramp events are separate, steady query records cycle with new request IDs, target RPS is planned per wall-clock window, in-flight saturation increments `not_issued_count` without growing a queue, and actual RPS/queue time reflect completed events. Call the isolation probe once at the steady midpoint and once after steady traffic; tag its events outside business QPS/latency.

  ```python
  result = run_pressure_phases(prepared, events.append, record_probe, fake_clock)
  assert result.steady["claw-mem0"].not_issued_count == 3
  assert max_concurrent_by_user["user-a"] == 1
  assert len(result.probe_events) == 2
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_pressure_scheduler.py -q`; expect failures.
- [ ] **Step 3: Implement the scheduler.** Follow the existing multi-user scheduler's fairness and file-descriptor rules without changing the manual route. Close JSONL descriptors between reads. Use a bounded pending plan and record planned, issued, completed, timed-out, and missed counts per target/window.
- [ ] **Step 4: Verify green and regressions.** Run scheduler and existing multi-user tests; expect all to pass.
- [ ] **Step 5: Commit.** Commit scheduler and tests.

### Task 5: Correctness gate and pressure report

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/pressure_report.py`
- Create: `memory_bench_platform/tests/test_pressure_report.py`

**Interfaces:**
- Consumes: `PressurePhasesResult` from Task 4, `PreparedPressureRun` from Task 3.
- Produces: `score_sample(checker_command: list[str], sample: PressureSample, cwd: Path) -> ScoreResult` and `write_pressure_report(run_dir: Path, prepared: PreparedPressureRun, result: PressurePhasesResult) -> dict` with schema `pressure-report/1`.

- [ ] **Step 1: Write failing report tests.** Assert checker receives request/answer/expected only through protected temporary input, raw content and credentials never enter durable artifacts, sample correctness/nonempty/isolation thresholds fail the suite, absent expectation produces `unavailable`, configured `min_correct_rate` without a score fails preflight, and percentiles are recomputed from event rows. Assert reports distinguish physical memory instance from target, mock dependency mode from Agent model mode, ramp from steady, requested from achieved RPS, and unavailable trace/hardware data from measured values.

  ```python
  report = write_pressure_report(run_dir, prepared, result)
  assert report["targets"]["claw-mem0"]["memory_instance_id"] == "memory-a"
  assert "PRIVATE_BODY" not in (run_dir / "summary.json").read_text()
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_pressure_report.py -q`; expect failures.
- [ ] **Step 3: Implement scoring and reports.** Extend replay-record handling for pressure runs so the existing optional JSONL `response` reaches the checker; select samples deterministically from a hash of `request_id` and the configured `sample_rate`, and keep `multi-user-report/1` unchanged. Emit per-user, per-target, per-physical-memory and overall summaries plus request events and input fingerprints. Include comparison claims only when workload, user count, rate, model mode, and initial state match.
- [ ] **Step 4: Verify green.** Run focused report tests and `test_multi_user_replay.py`; expect all to pass.
- [ ] **Step 5: Commit.** Commit scorer/report code and tests.

### Task 6: CLI integration, documentation, and end-to-end fake-Docker gate

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/pressure_run.py`
- Create: `memory_bench_platform/tests/test_pressure_cli_integration.py`
- Modify: `memory_bench_platform/memory_bench_platform/cli.py`
- Modify: `README.md`
- Modify: `docs/deployment-yaml.md`

**Interfaces:**
- Consumes: Tasks 1–5 and the prerequisite deployment plan.
- Produces: `run_pressure(name: str, pressure_test_id: str, cwd: Path, docker: DockerClient) -> int`; CLI `memory-bench run --deployment <name> --pressure-test <id>`.

- [ ] **Step 1: Write a failing CLI integration test.** With fake Docker, two Agent Skill runners, two memory backends, and three users, execute `deploy → run --pressure-test → status → undeploy`; assert route/identity isolation, optional shared instance ownership, exact seed/warmup counts, phase ordering, report links, and nonzero return on one target failure. Assert `--pressure-test` conflicts with `--combination` and manual selection flags; manual `--workload-config` still runs as before.

  ```python
  assert cli("run", "--deployment", "suite", "--pressure-test", "agent-memory-load") == 0
  assert report["targets"]["claw-mem0"]["agent_skill"] == "openclaw"
  assert report["targets"]["worker-graphmem"]["memory_skill"] == "graphmem"
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_pressure_cli_integration.py -q`; expect failures.
- [ ] **Step 3: Implement dispatch and docs.** Acquire deployment resource locks, recheck live status, prepare targets, run phases, always finalize, write partial/final reports, and return 130 for interruption. Document YAML mapping, shared vs isolated memory, model/Mock declarations, finite vs steady load, user seed, correctness limits, and real-image smoke requirements.
- [ ] **Step 4: Verify green and full relevant regression.** Run all pressure/deployment modules, `test_multi_user_replay.py`, `test_production_replay.py`, CLI tests, and `git diff --check`; expect all to pass. Run real Docker end-to-end only with suitable images/data and record skips explicitly.
- [ ] **Step 5: Commit.** Commit integration and docs.

## Completion Gate

The full spec is implemented only when both plans' tests pass and the pressure report can trace each request to its target and physical memory instance. A fake-Docker integration proves orchestration only; real end-to-end status requires real images, datasets, and dependencies.
