# Profiling Run Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect deployment and multi-user workloads to phase collectors, offline analysis, and an optional owned-deployment `experiment` command.

**Architecture:** A small phase sink writes true target windows and brackets each measured operation with one collector session. Existing runners emit phase/request events; they do not parse perf. `experiment` coordinates existing deploy/run/analyze calls and cleans up only a deployment it created when explicitly requested.

**Tech Stack:** Python 3.11+, existing CLI/workflow/deployment modules, pytest; Linux perf smoke only where supported.

**Spec:** `docs/superpowers/specs/2026-10-10-end-to-end-profiling-analysis-design.md` §§1, 3–4, 7–9. Depends on both preceding profiling plans. Deployment pressure-test integration additionally depends on `2026-10-09-multi-agent-pressure-plan.md`; keep its current rejection until implemented.

## Global Constraints

- No `--profiling-config` means current run behavior and output stay unchanged.
- `ingest`, `drain`, and `recall` windows use actual monotonic start/end, not accumulated step durations.
- One physical target gets one collector per phase even if many users or Agents share it.
- Collector must be ready before formal requests; close the phase window before waiting for collector shutdown.
- Interrupted/failed runs retain events, collector state, and partial reports; external deployments and source logs remain untouched.
- Multi-Agent `--pressure-test` remains rejected until its prerequisite implementation exists.

## Review Focus

- An empty phase: record `not_applicable` without starting perf or inventing QPS (Task 1).
- A slow user and a fast user share a physical memory instance: exactly one target/phase capture covers both (Task 2).
- `required` fails after deploy seed but before the first formal add: no measured request is sent (Task 3).
- Ctrl-C during a phase: events and collector status remain available for offline analysis (Tasks 1–3).
- `experiment --cleanup-owned-deployment` sees a pre-existing deployment: it never undeploys that resource (Task 4).

---

### Task 1: Phase sink and failure-safe run archive

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_phase.py`; create `memory_bench_platform/tests/test_profiling_phase.py`.

**Interfaces:** Produce `PhaseSink(run_dir: Path, run_id: str, session: ProfilerSession)` with `run_phase(phase_id: str, target_ids: list[str], operation: Callable[[], T], *, warmup: bool = False) -> T`. Write `phase_events.jsonl` and `target_registry.json`; preserve partial events and collector status if `operation` raises. Per-target windows are half-open; global windows are their union.

- [ ] **Step 1: Write failing tests.** `test_collector_ready_before_operation`; `test_window_closes_before_collector_stop`; `test_empty_phase_not_applicable`; `test_shared_target_started_once`; `test_exception_persists_partial_phase`; `test_overlapping_parent_child_not_summed`.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_phase.py -q`; expect failures.
- [ ] **Step 3: Implement `PhaseSink`.** Use UTC timestamps for placement and monotonic nanoseconds for duration. Write/flush phase records before returning or propagating an exception. Record collector startup separately from the measured window.
- [ ] **Step 4: Verify green.** Run the focused module; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit phase sink and tests.

### Task 2: Multi-user add/drain/search connection

**Files:** Modify `memory_bench_platform/memory_bench_platform/multi_user_cli.py`; modify `memory_bench_platform/memory_bench_platform/multi_user_replay.py`; modify `memory_bench_platform/memory_bench_platform/cli.py`; modify `memory_bench_platform/tests/test_multi_user_replay.py`.

**Interfaces:** Add optional `profiling_config: ProfilingConfig | None = None` to `run_multi_user(config, output_dir, run_id, *, profiling_config=None)` and `--profiling-config FILE` to `run`. Route `add`→`ingest`, `drain`→`drain`, `search`→`recall`. Reuse `PhaseSink`; keep per-user metrics in existing report.

- [ ] **Step 1: Write failing tests.** `test_backend_direct_phases_ingest_and_recall`; `test_openclaw_add_drain_search_phases`; `test_two_users_one_instance_one_collector`; `test_missing_target_identity_is_skipped`; `test_required_profile_blocks_requests`; `test_setup_interrupt_keeps_empty_event_log_and_partial_profile`.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_multi_user_replay.py -q`; expect new assertions to fail.
- [ ] **Step 3: Add CLI parsing and phase wrapping.** Preflight config and targets, create `request_events.jsonl` before setup, snapshot config/target identities, wrap existing `Replay.phase` calls, and import selected trace source after cleanup. Do not replace the existing scheduler or add a collector per user.
- [ ] **Step 4: Verify green.** Run the focused module and `python -m pytest memory_bench_platform/tests/test_cli_smoke.py -q`; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit multi-user connection and tests.

### Task 3: Ordinary/deployed run phase connection

**Files:** Modify `memory_bench_platform/memory_bench_platform/cli.py`; modify `memory_bench_platform/memory_bench_platform/workflow.py`; modify `memory_bench_platform/memory_bench_platform/deployment_run.py`; modify `memory_bench_platform/skills/benchmarks/production-http-replay/scripts/run_replay.py`; modify `memory_bench_platform/tests/test_workflow_executor.py`; modify `memory_bench_platform/tests/test_deployment_run.py`.

**Interfaces:** Pass `--profiling-config FILE` through `run --deployment` to each native child and through ordinary `run` into the workflow/production-replay path. A runner without a trustworthy stage hook records `skipped: phase_boundary_unavailable`; it must not fabricate a phase from total runtime.

- [ ] **Step 1: Write failing tests.** `test_deployment_run_forwards_profile_per_combination`; `test_workflow_phase_events_bracket_ingest_recall`; `test_external_replay_add_drain_search_boundaries`; `test_required_probe_failure_precedes_measured_write`; `test_unknown_runner_has_explicit_unavailable_phase`; `test_deployment_drift_blocks_collector`.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_workflow_executor.py memory_bench_platform/tests/test_deployment_run.py memory_bench_platform/tests/test_generic_cli_runner.py -q`; expect new assertions to fail.
- [ ] **Step 3: Connect existing hooks to `PhaseSink`.** Resolve physical instances from checked deployment state, record actual stage transitions, and pass only sanitized profiling settings to children. Keep current workflow/entrypoint behavior when profiling is absent. Do not silently accept a runner whose stage boundary cannot be observed.
- [ ] **Step 4: Verify green.** Run focused modules and `python -m pytest memory_bench_platform/tests -q`; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit ordinary/deployed integration and tests.

### Task 4: Owned experiment orchestration and end-to-end gate

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_experiment.py`; modify `memory_bench_platform/memory_bench_platform/cli.py`; create `memory_bench_platform/tests/test_profiling_experiment.py`; modify `README.md`.

**Interfaces:** Produce `run_experiment(config_path: Path, profiling_path: Path, cwd: Path, docker: DockerClient, *, pressure_test: str | None = None, cleanup_owned_deployment: bool = False) -> ExperimentResult`. Add `experiment --config DEPLOYMENT.yaml --profiling-config FILE [--pressure-test ID] [--cleanup-owned-deployment]`.

- [ ] **Step 1: Write failing tests.** `test_deploy_verify_run_analyze_order`; `test_existing_deployment_never_cleaned`; `test_owned_cleanup_only_when_requested`; `test_run_failure_keeps_partial_archive`; `test_pressure_test_still_rejected_without_prerequisite`; `test_unsupported_platform_best_effort_completes_with_skipped_hardware`.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_experiment.py -q`; expect failures.
- [ ] **Step 3: Implement orchestration and documentation.** Use existing `deploy`, `status`, `run_deployment`, `analyze-run`, and `undeploy` paths; persist state after each step. Clean only an owned deployment in `finally` when requested, retaining the run directory. Document Linux permission limits, output paths, and unavailable semantics.
- [ ] **Step 4: Verify green and end-to-end.** Run the focused module and full test suite. On a controlled Linux host, run one add→drain→search profile and re-run `analyze-run`; verify report equality, raw counter links, join coverage, and no leaked request content. On macOS, verify explicit unsupported-platform status instead of perf output.
- [ ] **Step 5: Commit.** Commit experiment command, tests, and documentation.
