from __future__ import annotations

import json
import sys

from memory_bench_platform.benchmark_scenario import BenchmarkScenario


if __name__ == "__main__":
    BenchmarkScenario.model_validate(json.load(sys.stdin))
    print(json.dumps({"status": "ok"}))
