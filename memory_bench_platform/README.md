# Memory Benchmark Platform

Workflow/case oriented benchmark and test platform for memory-oriented systems.

## MVP Matrix

- Benchmarks: `LoCoMo`, `LongMemEval`
- Agents: `OpenClaw`, `Generic CLI Agent`

## Current Scope

- CaseSource driven execution
- Workflow/case core
- Builtin judge
- JSON archive with `cases/steps/traces/judge_results/metrics`
- ClusterBench-style resource monitor extraction

## Core Path

```text
CaseSource -> Case/Step DAG -> Operator Execution -> Gate/Retry -> Trace/Evidence -> Judge -> Report/Archive
```

## Quick Checks

```bash
python3 -m memory_bench_platform.cli list-skills
python3 -m memory_bench_platform.cli validate --benchmark locomo
python3 -m memory_bench_platform.cli validate --agent openclaw
python3 -m memory_bench_platform.cli validate --smoke locomo-openclaw-openviking-minimal
python3 -m memory_bench_platform.cli run --benchmark locomo --agent generic-cli
python3 -m memory_bench_platform.cli run-smoke --smoke locomo-openclaw-openviking-minimal
python3 -m memory_bench_platform.cli run --benchmark locomo --agent openclaw --entrypoint locomo_test_remote --smoke-gate locomo-openclaw-openviking-minimal
```

## External Runner

The platform can dispatch an external benchmark runner declared by a benchmark
manifest and then import its result files back into the platform run archive.

Example:

```bash
python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent openclaw \
  --entrypoint official_small
```

Derived LoCoMo execution path via `locomo_test`:

```bash
python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent openclaw \
  --entrypoint locomo_test_remote
```

This is the preferred architecture direction when LoCoMo needs the richer
OpenClaw/OpenViking bootstrap and diagnostics that already exist in
`locomo_test`:

- `memory_bench_platform` remains the main run entry, archive, monitor, and report framework.
- `locomo_test_remote` acts as a LoCoMo-specific external runner derived from that framework.
- `locomo_test` is therefore treated as a benchmark-specialized execution layer, not a parallel benchmark platform.

Expected run artifacts:

- `run.json` with `source_kind = "external_benchmark_runner"`
- `run.json.benchmark_version_policy` / `run.json.agent_version_policy`
- `records/version_selection.json`
- `records/external_entrypoint.json`
- `logs/external_runner.stdout.log`
- `logs/external_runner.stderr.log`
- `reports/summary.json`
- `reports/case_results.json`
- `reports/external_result_summary.json`
- `reports/analysis.json`
- `reports/analysis.md`

## Result Analysis

Analyze an existing run directory:

```bash
python3 -m memory_bench_platform.cli analyze-run --run-dir /path/to/run
```

Every successful `run` command also writes:

- `reports/analysis.json`
- `reports/analysis.md`
- `reports/run_report.html`

## Smoke Skills

The platform now discovers smoke skills under `skills/smoke/` and supports:

```bash
python3 -m memory_bench_platform.cli validate --smoke locomo-openclaw-openviking-minimal
python3 -m memory_bench_platform.cli run-smoke --smoke locomo-openclaw-openviking-minimal
python3 -m memory_bench_platform.cli run --benchmark locomo --agent openclaw --entrypoint locomo_test_remote --smoke-gate locomo-openclaw-openviking-minimal
```

Current behavior:

- `validate --smoke` performs static prerequisite validation.
- `run-smoke` executes the smoke skill and writes:
  - `reports/smoke_trace.json`
  - `reports/smoke_summary.json`
  - `reports/smoke_report.html`
- `run --smoke-gate <smoke-id>` runs the smoke first and blocks the benchmark
  if the smoke fails. The blocked run writes `records/smoke_gate.json` and a
  failed `reports/summary.json` with `case_total = 0`.

The bundled `locomo-openclaw-openviking-minimal` smoke skill uses
`locomo_test/configs/mini-test.toml` as the minimum runnable chain. Its runtime
config fills an isolated OpenClaw `state_dir` under the smoke run directory when
the local `env.toml` leaves `gateway.state_dir` empty, and pins `data_file` to
the repository LoCoMo small dataset path.

## External Integration Examples

```bash
python3 -m memory_bench_platform.cli validate \
  --benchmark locomo \
  --data-path /path/to/locomo/data/locomo10.json

python3 -m memory_bench_platform.cli validate \
  --memory-backend openviking \
  --source-path /path/to/OpenViking \
  --api-base https://ark.cn-beijing.volces.com/api/coding/v3 \
  --api-key "$ARK_API_KEY" \
  --vlm-model doubao-seed-2.0-pro \
  --embedding-model doubao-embedding-vision
```

## Version Policy

- Platform integration skills must treat `latest_official_release_tag` as the default software selection policy.
- A benchmark/agent run may deviate from the latest official tag only when the run config or operator input explicitly requests an allowed override.
- Every real run should archive both:
  - the version policy declared by the selected skill
  - the default-or-override selection outcome derived from that skill
  - the concrete runtime version that was actually observed/resolved
- `records/version_selection.json` should include per-target default resolution results, for example the latest upstream release tag resolved from `targets[].upstream`.
- For real benchmark integrations, default to the latest official release tag of the tested software.
- Do not default to:
  - dirty worktrees
  - dev builds
  - unpublished local commits
- If a run uses a non-release build, record that explicitly in the run conclusion or analysis report.
- Every benchmark skill and agent skill manifest should declare a `version_policy` block so the default is machine-readable, not only written in `SKILL.md`.
- `version_policy` should not only say "use latest tag"; it should also declare:
  - `resolution_order`: the concrete fallback order
  - `targets`: which software components are governed by this policy
  - `targets[].version_source`: whether the version is resolved from an upstream release/tag source or can only be recorded from runtime observation
  - `targets[].upstream`: where the platform should resolve the latest official tag for each governed component
  - `record_runtime_version`: whether the resolved runtime version must be archived

Recommended priority:

1. User-specified official version
2. Latest upstream official release tag
3. Verified fallback release tag

Minimal manifest shape:

```yaml
version_policy:
  default_selection: latest_official_release_tag
  resolution_order:
    - user_specified_official_version
    - latest_official_release_tag
    - verified_fallback_release_tag
    - historical_repro_release_tag
  allowed_overrides:
    - user_specified_official_version
    - verified_fallback_release_tag
    - historical_repro_release_tag
  disallowed_defaults:
    - dirty_worktree
    - dev_build
    - non_tag_commit
  targets:
    - name: openclaw
      scope: system_under_test
      version_source: upstream_release_tag
      upstream: https://github.com/openclaw/openclaw
    - name: openviking
      scope: memory_backend
      version_source: upstream_release_tag
      upstream: https://github.com/volcengine/OpenViking
  record_runtime_version: true
```

Recommended target-level policy:

- Real benchmark / agent integration skills should use `version_source: upstream_release_tag`.
- Generic wrapper skills that do not own a concrete upstream mapping may use `version_source: runtime_observed_only`, but they should not be treated as authoritative official benchmark baselines.
