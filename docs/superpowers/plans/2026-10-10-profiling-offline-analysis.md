# Profiling Offline Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild stage, internal-span, and hardware analysis from archived evidence through both existing `analyze-run` paths.

**Architecture:** Keep ordinary and multi-user report validation separate. A shared offline module reads versioned profiling artifacts, normalizes client events through two adapters, computes attribution and collector validity, then writes standalone reports. Existing reports link to its output without changing old run semantics.

**Tech Stack:** Python 3.11+, Pydantic 2, pytest, standard-library CSV/JSON/HTML.

**Spec:** `docs/superpowers/specs/2026-10-10-end-to-end-profiling-analysis-design.md` §§3, 6–9.

## Global Constraints

- Do not call a live memory service from `analyze-run`.
- Preserve existing ordinary-run and `multi-user-run/1` inputs and outputs when profiling artifacts are absent.
- Join on run, physical instance, user/tenant where present, request ID, and operation; never invent missing identity.
- Hardware attribution stays at physical target × phase, not request or user.
- Missing, skipped, and invalid measurements are distinct from zero; never add wrapper and leaf durations.
- Raw prompts, responses, credentials, and private endpoints must not enter public reports.

## Review Focus

- A multi-user archive has no `run.json`: analysis still uses its validated events (Task 1).
- Two users share a request ID: the trace joins only the matching scoped request (Task 2).
- A leaf sits under a wrapper: only the leaf contributes to stage share (Task 2).
- A counter has low running fraction or a target restarted: report invalid, not a numeric bottleneck (Task 3).
- A profiling marker exists but a required artifact is missing: fail explicitly; an old archive with no marker retains old behavior (Task 4).

---

### Task 1: Archive contract and client-event adapters

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_archive.py`; create `memory_bench_platform/tests/test_profiling_archive.py`.

**Interfaces:** Produce `load_profile_archive(run_dir: Path) -> ProfileArchive | None`, `load_client_events(run_dir: Path) -> list[ClientEvent]`, and typed `PhaseEvent`, `ClientEvent`, `CollectorStatus`, `ProfileArchive`. `None` means no profiling marker; marked-but-incomplete archives raise `ValueError`. Ordinary adapter reads existing run artifacts; multi-user adapter reads `run_config.json` and `request_events.jsonl` after existing report validation.

- [ ] **Step 1: Write failing tests.** `test_old_archives_return_none`; `test_multi_user_without_run_json_loads_scoped_events`; `test_marked_archive_missing_phase_file_fails`; `test_phase_half_open_windows_and_duplicate_target_phase_fail`. Assert ordinary and multi-user fixtures normalize operation, run/instance/user IDs, request ID, start/end, status; absent IDs remain absent.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_archive.py -q`; expect the new tests to fail.
- [ ] **Step 3: Implement the interfaces.** Use strict schema/version checks and read only archived files. Reject missing references, negative or overlapping same-target phase windows, duplicate event IDs, and unknown schema versions; allow explicit collector `skipped` without raw counters.
- [ ] **Step 4: Verify green.** Run the focused test module; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit archive models, adapters, and tests.

### Task 2: Trace join and non-overlapping stage attribution

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_trace_analysis.py`; create `memory_bench_platform/tests/test_profiling_trace_analysis.py`.

**Interfaces:** Consume `ProfileArchive` and `ClientEvent` from Task 1. Produce `analyze_trace(archive: ProfileArchive, clients: list[ClientEvent]) -> TraceAnalysis`; `TraceAnalysis` contains join counts/rate, per-operation stage distributions, residuals, and validity reason.

- [ ] **Step 1: Write failing tests.** `test_join_requires_scope_and_operation`; `test_wrapper_leaf_not_double_counted`; `test_async_task_span_maps_to_drain`; `test_missing_clock_anchor_prevents_perf_bucket_join`; `test_low_join_rate_marks_attribution_invalid`. Include duplicate span IDs, negative durations, unknown stage, extra trace rows, and unmatched client requests; assert each receives a count/reason.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_trace_analysis.py -q`; expect failures.
- [ ] **Step 3: Implement `analyze_trace`.** Read only sanitized `profiling/perf_trace_events.jsonl` and a versioned stage-map manifest. Sum non-overlapping leaf spans per request, retain wrappers for gaps, compute `unattributed_ms` and `client_service_gap_ms` separately, and gate attribution by `min_request_join_rate` (default `0.95`). Do not correlate cross-machine monotonic clocks.
- [ ] **Step 4: Verify green.** Run the focused test module; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit trace analysis and tests.

### Task 3: Hardware validity and standalone reports

**Files:** Create `memory_bench_platform/memory_bench_platform/profiling_analysis.py`; create `memory_bench_platform/memory_bench_platform/profiling_report.py`; create `memory_bench_platform/tests/test_profiling_analysis.py`.

**Interfaces:** Consume Tasks 1–2. Produce `analyze_profiling_archive(run_dir: Path) -> ProfilingAnalysis | None`; it writes `reports/join_coverage.json`, `stage_summary.csv`, `attribution_residuals.csv`, and `profiling_analysis.json/md/html`. `ProfilingAnalysis` exposes a content-free summary and report paths to callers.

- [ ] **Step 1: Write failing tests.** `test_reanalysis_rebuilds_same_report`; `test_counter_running_below_090_invalid`; `test_topdown_sum_outside_95_to_105_invalid`; `test_restart_invalidates_target_phase`; `test_skipped_hardware_is_unavailable_not_zero`; `test_report_excludes_trace_payload_and_private_endpoint`. Include a fixture where high LLM wall wait does not become a CPU bottleneck claim.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_profiling_analysis.py -q`; expect failures.
- [ ] **Step 3: Implement analysis and rendering.** Parse normalized counter rows, check same-target/phase/CPU/event comparability, preserve running fraction and collector status, and compute phase/user/target metrics using actual windows. Present Topdown and wall-time stage shares side by side, never add them. Write deterministic UTF-8 reports from sanitized data; compare any existing profiling summary to recomputed values before replacement.
- [ ] **Step 4: Verify green.** Run the focused test module; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit analysis, rendering, and tests.

### Task 4: Reuse both `analyze-run` paths

**Files:** Modify `memory_bench_platform/memory_bench_platform/result_analysis.py`; modify `memory_bench_platform/memory_bench_platform/multi_user_report.py`; modify `memory_bench_platform/tests/test_result_analysis.py`; modify `memory_bench_platform/tests/test_multi_user_replay.py`.

**Interfaces:** Consume `analyze_profiling_archive(run_dir)` from Task 3. Keep CLI schema dispatch in `cli.py` unchanged. Ordinary run adds a profiling summary/link before writing existing reports; multi-user run validates/recomputes its existing report first, then adds a link in HTML without extending `multi-user-report/1` summary fields.

- [ ] **Step 1: Write failing integration tests.** `test_ordinary_analyze_embeds_profile_link`; `test_multi_user_analyze_profiles_without_run_json`; `test_legacy_analyze_outputs_unchanged`; `test_invalid_multi_user_binding_blocks_profile_report`; `test_marked_incomplete_profile_is_not_silently_ignored`.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_result_analysis.py memory_bench_platform/tests/test_multi_user_replay.py -q`; expect new assertions to fail.
- [ ] **Step 3: Add the two calls and report links.** Keep format-specific LoCoMo/official_small timing independent; do not pass fabricated ordinary-run files into multi-user analysis.
- [ ] **Step 4: Verify green and regressions.** Run the two focused modules and `python -m pytest memory_bench_platform/tests -q`; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit only integration and test changes.
