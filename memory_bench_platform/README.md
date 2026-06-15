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

Recommended priority:

1. User-specified official version
2. Latest upstream official release tag
3. Verified fallback release tag
