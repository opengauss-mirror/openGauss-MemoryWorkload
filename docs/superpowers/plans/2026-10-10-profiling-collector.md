# Profiling Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture per-target, per-phase Linux perf evidence and sanitized memory spans without modifying the measured service.

**Architecture:** Strict profiling config and target registry feed one `ProfilerSession`. A Linux cgroup adapter owns only its spawned process groups; an unavailable adapter records explicit skipped status. A trace importer copies only validated, content-free span fields into the archive. Runner integration is in the follow-on plan.

**Tech Stack:** Python 3.11+, Pydantic 2, PyYAML, Linux perf CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-10-10-end-to-end-profiling-analysis-design.md` §§3–6, 8–9. Depends on archive models in `2026-10-10-profiling-offline-analysis.md` Task 1.

## Global Constraints

- Linux cgroup v2 is the default hardware scope; no silent single-PID fallback.
- No `sudo`, sysctl changes, privileged-container changes, or deletion of source logs.
- `required` must fail before formal measured requests if selected collection cannot start; `best_effort` records skipped/degraded evidence.
- Starting and stopping a collector never replays a target write request.
- The platform stops only its own collector process groups, including descendants, on timeout and interruption.
- Raw `perf.data` and source trace logs remain private; public reports contain only sanitized fields.

## Review Focus

- A container ID or start time changes mid-phase: invalidate its counters (Task 1).
- A parent perf process times out with a child still alive: stop the whole group (Task 2).
- Topdown is unsupported but basic counters work: `best_effort` labels downgrade; `required` blocks measured traffic (Task 2).
- Raw perf output exceeds the configured 1073741824-byte cap: stop capture and retain an invalid status (Task 2).
- A trace file rotates or contains prompt text: import only this run's selected range and never copy payload text (Task 3).

---

### Task 1: Config, target registry, and capability probe

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_config.py`; create `memory_bench_platform/memory_bench_platform/profiling_targets.py`; create `memory_bench_platform/tests/test_profiling_config.py`; create `memory_bench_platform/tests/test_profiling_targets.py`.

**Interfaces:** Produce `load_profiling_config(path: Path) -> ProfilingConfig`, `resolve_targets(config: ProfilingConfig, deployment_state: DeploymentState | None, explicit_targets: list[TargetIdentity]) -> list[TargetIdentity]`, and `probe_target(target: TargetIdentity, config: ProfilingConfig, docker: DockerClient | None) -> CapabilityResult`. `TargetIdentity` records the fields in spec §4; `CapabilityResult` carries supported event group, perf version, CPU scope, and skip/failure reason.

- [ ] **Step 1: Write failing tests.** `test_profiling_1_strict_schema_and_defaults`; `test_shared_memory_resolves_once`; `test_external_target_without_verified_pid_or_cgroup_skips`; `test_container_restart_drift_is_detected`; `test_non_linux_probe_is_unsupported_platform`. Reject unknown YAML fields, invalid quality-gate range, selected phases without targets, ambiguous role mapping, and secrets/commands in config.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_config.py memory_bench_platform/tests/test_profiling_targets.py -q`; expect failures.
- [ ] **Step 3: Implement the interfaces.** Reuse deployment state and live `DockerClient.inspect`; record container ID, image digest, start time, restart count, cgroup path, and CPU set. Probe Linux/perf/cgroup/permissions/events using injectable command execution; do not infer that an installed perf binary means counters are usable.
- [ ] **Step 4: Verify green.** Run the focused modules; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit config, registry, probe, and tests.

### Task 2: Owned perf process lifecycle and normalization

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_perf.py`; create `memory_bench_platform/tests/test_profiling_perf.py`.

**Interfaces:** Produce `ProfilerSession(config: ProfilingConfig, targets: list[TargetIdentity], output_dir: Path, command_factory: Callable)` with `start_phase(phase_id: str, target_ids: list[str]) -> list[CollectorHandle]` and `stop_phase(handles: list[CollectorHandle]) -> list[CollectorStatus]`. Write `profiling/collector_status.json`, per-target/phase raw files, `perf_counters.csv`, and `topdown_summary.csv` using Task 1 archive schema.

- [ ] **Step 1: Write failing tests.** `test_ready_precedes_phase_window`; `test_required_probe_failure_blocks_start`; `test_best_effort_topdown_downgrades_by_name`; `test_timeout_kills_process_group`; `test_counter_running_fraction_below_090_invalid`; `test_raw_size_cap_invalidates_capture`; `test_stop_is_idempotent_after_interrupt`. Test `perf stat` not-supported/not-counted values and Topdown 95%–105% sum checks with a recording command fake.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_perf.py -q`; expect failures.
- [ ] **Step 3: Implement `ProfilerSession`.** Use argv, no shell, Linux `perf stat` cgroup scope, and separate process groups. Probe event groups before start, persist every state transition, cap raw output, normalize perf machine output, and retain running fraction. Gate `perf record` to selected phases and the same size/lifecycle rules. Re-inspect target identity at stop; invalidate changed targets.
- [ ] **Step 4: Verify green.** Run the focused module; on Linux with permissions, run a separate minimal real-perf smoke; otherwise assert explicit skipped status.
- [ ] **Step 5: Commit.** Commit collector and tests.

### Task 3: Trace source capture and sanitized import

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_trace_import.py`; create `memory_bench_platform/tests/test_profiling_trace_import.py`.

**Interfaces:** Produce `capture_trace_cursor(source: Path) -> TraceCursor` and `import_trace(source: Path, cursor: TraceCursor, run: TraceRunScope, stage_map: Path, output_dir: Path) -> TraceImportStatus`. Write only `profiling/perf_trace_events.jsonl` plus counts/reasons; do not copy the source file. `TraceRunScope` identifies run, allowed instances/users, time range, and request/task IDs.

- [ ] **Step 1: Write failing tests.** `test_mem_perf_prefix_and_plain_jsonl`; `test_rotation_uses_saved_identity_and_offset`; `test_out_of_run_and_missing_ids_counted`; `test_duplicate_span_and_negative_duration_counted`; `test_prompt_and_response_never_copied`; `test_async_task_id_preserved`; `test_incompatible_stage_map_fails`.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_trace_import.py -q`; expect failures.
- [ ] **Step 3: Implement cursor and importer.** Require timezone-aware `ts`, operation, stage, role, duration, status, request ID, and `kind=memory_perf`; limit source bytes per config, validate a versioned stage map, and emit only numeric fields, scope IDs, stage, status, and trusted timing anchors. Record malformed/oversized/unknown rows without leaking their bodies.
- [ ] **Step 4: Verify green.** Run the focused module and `python -m pytest memory_bench_platform/tests/test_profiling_archive.py -q`; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit importer and tests.
