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
