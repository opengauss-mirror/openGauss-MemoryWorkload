# Memory Benchmark Platform

Minimal benchmark orchestrator for evaluating memory-oriented agents against
benchmark skills and agent skills with a unified run protocol.

## MVP Matrix

- Benchmarks: `LoCoMo`, `LongMemEval`
- Agents: `OpenClaw`, `Generic CLI Agent`

## Current Scope

- Unified run protocol
- Directory skill loading
- Stub execution contract
- JSON run archive
- ClusterBench-style resource monitor extraction

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
