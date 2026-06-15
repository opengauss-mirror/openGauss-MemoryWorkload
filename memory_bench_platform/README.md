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
python3 -m memory_bench_platform.cli run --benchmark locomo --agent generic-cli
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

Expected run artifacts:

- `run.json` with `source_kind = "external_benchmark_runner"`
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
      upstream: https://github.com/coding-guy/openclaw
    - name: openviking
      scope: memory_backend
      upstream: https://github.com/xforce-io/openviking
  record_runtime_version: true
```
