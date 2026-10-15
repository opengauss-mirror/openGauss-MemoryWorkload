"""Run the production JSONL reader with the explicit Mem0 sample mapping."""
import os
import runpy
from pathlib import Path


os.environ["MEMORY_BENCH_REPLAY_ADAPTER_MODE"] = "mem0-sample"
runpy.run_path(str(Path(__file__).resolve().parents[2] /
                   "production-http-replay/scripts/run_replay.py"), run_name="__main__")
