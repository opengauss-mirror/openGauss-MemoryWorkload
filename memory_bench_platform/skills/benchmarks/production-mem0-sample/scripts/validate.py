"""Reuse the JSONL shape validator for the Mem0 sample scenario."""
import runpy
import sys
from pathlib import Path


script = Path(__file__).resolve().parents[2] / "production-http-replay/scripts/validate.py"
sys.path.insert(0, str(script.parent))
runpy.run_path(str(script), run_name="__main__")
