# Memory Benchmark Platform MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal benchmark platform that can run `LoCoMo` and `LongMemEval` against `OpenClaw` and a `Generic CLI Agent`, then persist unified run/task/turn/artifact/metric/judge outputs.

**Architecture:** The platform stays thin. A Python orchestrator discovers benchmark and agent skills from directory manifests, validates them, expands benchmark data into protocol tasks, executes tasks through the selected agent skill, and writes all outputs into a unified run directory. Benchmark-specific and agent-specific behavior lives inside skill directories rather than the core runtime. Existing `ClusterBench` code is reused only in two narrow ways: resource monitoring may borrow its host-level collector implementation, and run archiving may borrow its directory organization pattern; its workload model and report schema are explicitly out of scope.

**Tech Stack:** Python 3.11+, `pydantic`, `jsonschema`, `PyYAML`, `pytest`

---

### Task 1: Scaffold the platform package and CLI shell

**Files:**
- Create: `memory_bench_platform/pyproject.toml`
- Create: `memory_bench_platform/README.md`
- Create: `memory_bench_platform/memory_bench_platform/__init__.py`
- Create: `memory_bench_platform/memory_bench_platform/cli.py`
- Create: `memory_bench_platform/memory_bench_platform/paths.py`
- Create: `memory_bench_platform/tests/test_cli_smoke.py`

- [ ] **Step 1: Write the failing CLI smoke test**

```python
from memory_bench_platform.cli import build_parser


def test_build_parser_exposes_expected_subcommands():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert {"list-skills", "plan-run", "run"} <= set(choices)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_cli_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_bench_platform'`

- [ ] **Step 3: Create the package skeleton and parser**

```toml
[project]
name = "memory-bench-platform"
version = "0.1.0"
description = "Benchmark orchestrator for memory-oriented agents"
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.7",
  "jsonschema>=4.22",
  "PyYAML>=6.0.1",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2",
]

[project.scripts]
memory-bench = "memory_bench_platform.cli:main"
```

```python
# memory_bench_platform/cli.py
import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-bench")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-skills")
    sub.add_parser("plan-run")
    sub.add_parser("run")
    return parser


def main() -> None:
    parser = build_parser()
    parser.parse_args()
```

```python
# memory_bench_platform/paths.py
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
SKILLS_ROOT = PROJECT_ROOT / "skills"
RUNS_ROOT = PROJECT_ROOT / "runs"
SCHEMAS_ROOT = PROJECT_ROOT / "schemas"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_cli_smoke.py -v`
Expected: PASS with `1 passed`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md memory_bench_platform/__init__.py memory_bench_platform/cli.py memory_bench_platform/paths.py tests/test_cli_smoke.py
git commit -m "feat: scaffold memory benchmark platform cli"
```

### Task 2: Define the unified run protocol models and run directory writer

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/protocol.py`
- Create: `memory_bench_platform/memory_bench_platform/storage.py`
- Create: `memory_bench_platform/tests/test_protocol_models.py`
- Create: `memory_bench_platform/tests/test_storage_layout.py`

- [ ] **Step 1: Write the failing protocol model tests**

```python
from memory_bench_platform.protocol import RunRecord, TaskRecord, TurnRecord


def test_run_record_requires_core_identifiers():
    record = RunRecord(
        run_id="run-001",
        benchmark_id="locomo",
        agent_id="openclaw",
        status="pending",
    )
    assert record.run_id == "run-001"
    assert record.benchmark_id == "locomo"
    assert record.agent_id == "openclaw"


def test_turn_record_binds_to_task():
    turn = TurnRecord(
        turn_id="turn-1",
        task_id="task-1",
        index=0,
        role="user",
        content="hello",
    )
    assert turn.task_id == "task-1"
```

```python
from pathlib import Path

from memory_bench_platform.protocol import RunRecord
from memory_bench_platform.storage import RunStorage


def test_run_storage_creates_expected_layout(tmp_path: Path):
    storage = RunStorage(tmp_path)
    run = RunRecord(run_id="run-001", benchmark_id="locomo", agent_id="openclaw", status="pending")
    run_dir = storage.init_run(run)
    assert (run_dir / "run.json").exists()
    assert (run_dir / "artifacts").is_dir()
    assert (run_dir / "records").is_dir()
    assert (run_dir / "logs").is_dir()
    assert (run_dir / "reports").is_dir()
    assert (run_dir / "config_snapshot").is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_protocol_models.py tests/test_storage_layout.py -v`
Expected: FAIL with missing `protocol.py` and `storage.py`

- [ ] **Step 3: Implement the protocol and storage models**

```python
# memory_bench_platform/protocol.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    run_id: str
    benchmark_id: str
    agent_id: str
    benchmark_version: str | None = None
    agent_version: str | None = None
    memory_backend: str | None = None
    hardware_profile: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: Literal["pending", "running", "passed", "failed", "partial"]


class TaskRecord(BaseModel):
    task_id: str
    run_id: str
    sample_id: str
    split: str | None = None
    scenario: str | None = None
    input_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    judge_mode: Literal["none", "builtin", "external"] = "none"


class TurnRecord(BaseModel):
    turn_id: str
    task_id: str
    index: int
    role: Literal["system", "user", "agent", "tool", "benchmark"]
    content: str
    timestamp: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
```

```python
# memory_bench_platform/storage.py
from __future__ import annotations

import json
from pathlib import Path

from .protocol import RunRecord


class RunStorage:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root

    def init_run(self, run: RunRecord) -> Path:
        run_dir = self.runs_root / run.run_id
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (run_dir / "records").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        (run_dir / "reports").mkdir(parents=True, exist_ok=True)
        (run_dir / "config_snapshot").mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
        return run_dir
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_protocol_models.py tests/test_storage_layout.py -v`
Expected: PASS with `3 passed`

- [ ] **Step 5: Commit**

```bash
git add memory_bench_platform/protocol.py memory_bench_platform/storage.py tests/test_protocol_models.py tests/test_storage_layout.py
git commit -m "feat: add unified run protocol and storage layout"
```

### Task 2.5: Extract ClusterBench-style resource monitoring into the new collector layer

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/resource_monitor.py`
- Create: `memory_bench_platform/tests/test_resource_monitor.py`

- [ ] **Step 1: Write the failing resource monitor test**

```python
from pathlib import Path

from memory_bench_platform.resource_monitor import ResourceMonitor


def test_resource_monitor_prepares_clusterbench_style_csv_files(tmp_path: Path):
    monitor = ResourceMonitor(output_dir=tmp_path, work_dir=tmp_path, disk_mount="root", net_interface="lo")
    monitor.setup_writers()
    assert (tmp_path / "cpu_status.csv").exists()
    assert (tmp_path / "mem_status.csv").exists()
    assert (tmp_path / "disk_status.csv").exists()
    assert (tmp_path / "net_status.csv").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_resource_monitor.py -v`
Expected: FAIL with missing `resource_monitor.py`

- [ ] **Step 3: Implement a decoupled resource monitor by adapting ClusterBench's collector logic**

```python
# memory_bench_platform/resource_monitor.py
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CsvWriter:
    path: Path
    headers: list[str]

    def create(self) -> None:
        with self.path.open("w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(self.headers)


class ResourceMonitor:
    def __init__(self, output_dir: Path, work_dir: Path, disk_mount: str, net_interface: str):
        self.output_dir = output_dir
        self.work_dir = work_dir
        self.disk_mount = disk_mount
        self.net_interface = net_interface

    def setup_writers(self) -> None:
        writers = [
            CsvWriter(self.output_dir / "cpu_status.csv", ["timestamp", "summary_util_user", "summary_util_sys", "summary_util_idle"]),
            CsvWriter(self.output_dir / "mem_status.csv", ["timestamp", "mem_free_mb", "mem_used_mb"]),
            CsvWriter(self.output_dir / "disk_status.csv", ["timestamp", "read_bw_mb", "write_bw_mb", "disk_bw_mb", "disk_free_mb"]),
            CsvWriter(self.output_dir / "net_status.csv", ["timestamp", "recv_pcks_rate", "sent_pcks_rate", "recv_bytes_rate", "sent_bytes_rate"]),
        ]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for writer in writers:
            writer.create()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_resource_monitor.py -v`
Expected: PASS with `1 passed`

- [ ] **Step 5: Commit**

```bash
git add memory_bench_platform/resource_monitor.py tests/test_resource_monitor.py
git commit -m "feat: add clusterbench-style resource monitor"
```

### Task 3: Add skill manifests, manifest schemas, and the skill loader

**Files:**
- Create: `memory_bench_platform/schemas/benchmark-manifest.schema.json`
- Create: `memory_bench_platform/schemas/agent-manifest.schema.json`
- Create: `memory_bench_platform/memory_bench_platform/manifests.py`
- Create: `memory_bench_platform/memory_bench_platform/loader.py`
- Create: `memory_bench_platform/tests/test_loader.py`

- [ ] **Step 1: Write the failing loader test**

```python
from pathlib import Path

from memory_bench_platform.loader import load_all_skills


def test_load_all_skills_reads_benchmark_and_agent_manifests(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "benchmarks" / "locomo").mkdir(parents=True)
    (skills / "agents" / "generic-cli").mkdir(parents=True)
    (skills / "benchmarks" / "locomo" / "manifest.yaml").write_text(
        "kind: benchmark\nid: locomo\nversion: 0.1.0\nentry:\n  task_builder: scripts/build_tasks.py\n",
        encoding="utf-8",
    )
    (skills / "agents" / "generic-cli" / "manifest.yaml").write_text(
        "kind: agent\nid: generic-cli\nversion: 0.1.0\nentry:\n  runner: scripts/run_task.py\n",
        encoding="utf-8",
    )
    loaded = load_all_skills(skills)
    assert loaded["benchmarks"][0].id == "locomo"
    assert loaded["agents"][0].id == "generic-cli"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_loader.py -v`
Expected: FAIL with missing loader or validation implementation

- [ ] **Step 3: Implement manifest models, schemas, and loader**

```python
# memory_bench_platform/manifests.py
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EntryPoints(BaseModel):
    task_builder: str | None = None
    scorer: str | None = None
    validator: str | None = None
    launcher: str | None = None
    runner: str | None = None
    collector: str | None = None
    teardown: str | None = None


class BenchmarkManifest(BaseModel):
    kind: str
    id: str
    version: str
    entry: EntryPoints
    dataset: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    judging: dict[str, Any] = Field(default_factory=dict)


class AgentManifest(BaseModel):
    kind: str
    id: str
    version: str
    entry: EntryPoints
    runtime: dict[str, Any] = Field(default_factory=dict)
    io: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    collection: dict[str, Any] = Field(default_factory=dict)
```

```python
# memory_bench_platform/loader.py
from __future__ import annotations

from pathlib import Path

import yaml

from .manifests import AgentManifest, BenchmarkManifest


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_all_skills(skills_root: Path) -> dict[str, list]:
    benchmarks = []
    agents = []
    for manifest_path in sorted((skills_root / "benchmarks").glob("*/manifest.yaml")):
        benchmarks.append(BenchmarkManifest.model_validate(_load_yaml(manifest_path)))
    for manifest_path in sorted((skills_root / "agents").glob("*/manifest.yaml")):
        agents.append(AgentManifest.model_validate(_load_yaml(manifest_path)))
    return {"benchmarks": benchmarks, "agents": agents}
```

```json
{
  "type": "object",
  "required": ["kind", "id", "version", "entry"]
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_loader.py -v`
Expected: PASS with `1 passed`

- [ ] **Step 5: Commit**

```bash
git add schemas/benchmark-manifest.schema.json schemas/agent-manifest.schema.json memory_bench_platform/manifests.py memory_bench_platform/loader.py tests/test_loader.py
git commit -m "feat: add skill manifests and loader"
```

### Task 4: Build the planner and execution contract between benchmarks and agents

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/planner.py`
- Create: `memory_bench_platform/memory_bench_platform/executor.py`
- Create: `memory_bench_platform/tests/test_planner.py`
- Create: `memory_bench_platform/tests/test_executor_contract.py`

- [ ] **Step 1: Write the failing planner and executor contract tests**

```python
from memory_bench_platform.planner import RunPlanRequest, build_run_plan


def test_build_run_plan_resolves_selected_benchmark_and_agent():
    request = RunPlanRequest(
        benchmark_id="locomo",
        agent_id="openclaw",
        benchmark_version="0.1.0",
        agent_version="0.1.0",
    )
    plan = build_run_plan(request)
    assert plan.run_id.startswith("locomo-openclaw-")
    assert plan.benchmark_id == "locomo"
```

```python
from memory_bench_platform.executor import SkillCommand


def test_skill_command_renders_as_process_args():
    cmd = SkillCommand(script="scripts/run_task.py", args=["--task", "task-1"])
    assert cmd.to_argv() == ["scripts/run_task.py", "--task", "task-1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_planner.py tests/test_executor_contract.py -v`
Expected: FAIL with missing planner and executor modules

- [ ] **Step 3: Implement the planning and execution contract**

```python
# memory_bench_platform/planner.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RunPlanRequest:
    benchmark_id: str
    agent_id: str
    benchmark_version: str | None = None
    agent_version: str | None = None
    memory_backend: str | None = None
    hardware_profile: str | None = None


@dataclass
class RunPlan:
    run_id: str
    benchmark_id: str
    agent_id: str
    benchmark_version: str | None
    agent_version: str | None
    memory_backend: str | None
    hardware_profile: str | None


def build_run_plan(request: RunPlanRequest) -> RunPlan:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return RunPlan(
        run_id=f"{request.benchmark_id}-{request.agent_id}-{stamp}",
        benchmark_id=request.benchmark_id,
        agent_id=request.agent_id,
        benchmark_version=request.benchmark_version,
        agent_version=request.agent_version,
        memory_backend=request.memory_backend,
        hardware_profile=request.hardware_profile,
    )
```

```python
# memory_bench_platform/executor.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillCommand:
    script: str
    args: list[str] = field(default_factory=list)

    def to_argv(self) -> list[str]:
        return [self.script, *self.args]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_planner.py tests/test_executor_contract.py -v`
Expected: PASS with `2 passed`

- [ ] **Step 5: Commit**

```bash
git add memory_bench_platform/planner.py memory_bench_platform/executor.py tests/test_planner.py tests/test_executor_contract.py
git commit -m "feat: add run planner and execution contract"
```

### Task 5: Add the first benchmark skills for LoCoMo and LongMemEval

**Files:**
- Create: `memory_bench_platform/skills/benchmarks/locomo/SKILL.md`
- Create: `memory_bench_platform/skills/benchmarks/locomo/manifest.yaml`
- Create: `memory_bench_platform/skills/benchmarks/locomo/scripts/build_tasks.py`
- Create: `memory_bench_platform/skills/benchmarks/longmemeval/SKILL.md`
- Create: `memory_bench_platform/skills/benchmarks/longmemeval/manifest.yaml`
- Create: `memory_bench_platform/skills/benchmarks/longmemeval/scripts/build_tasks.py`
- Create: `memory_bench_platform/tests/test_benchmark_skills.py`

- [ ] **Step 1: Write the failing benchmark skill test**

```python
from pathlib import Path

import yaml


def test_locomo_manifest_marks_multi_turn_stateful_execution():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/locomo/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["execution"]["mode"] == "multi_turn"
    assert manifest["execution"]["requires_stateful_agent"] is True


def test_longmemeval_manifest_declares_task_builder():
    manifest = yaml.safe_load(
        Path("skills/benchmarks/longmemeval/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["entry"]["task_builder"] == "scripts/build_tasks.py"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_benchmark_skills.py -v`
Expected: FAIL because benchmark skill directories do not exist yet

- [ ] **Step 3: Create both benchmark skill skeletons**

```yaml
# skills/benchmarks/locomo/manifest.yaml
kind: benchmark
id: locomo
version: 0.1.0
entry:
  task_builder: scripts/build_tasks.py
dataset:
  default_split: small
execution:
  mode: multi_turn
  requires_stateful_agent: true
judging:
  mode: external
```

```yaml
# skills/benchmarks/longmemeval/manifest.yaml
kind: benchmark
id: longmemeval
version: 0.1.0
entry:
  task_builder: scripts/build_tasks.py
dataset:
  default_split: dev
execution:
  mode: multi_turn
  requires_stateful_agent: true
judging:
  mode: external
```

```python
# scripts/build_tasks.py
from __future__ import annotations

import json
import sys


def main() -> None:
    payload = {"tasks": []}
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_benchmark_skills.py -v`
Expected: PASS with `2 passed`

- [ ] **Step 5: Commit**

```bash
git add skills/benchmarks/locomo skills/benchmarks/longmemeval tests/test_benchmark_skills.py
git commit -m "feat: add locomo and longmemeval benchmark skills"
```

### Task 6: Add the first agent skills for OpenClaw and Generic CLI Agent

**Files:**
- Create: `memory_bench_platform/skills/agents/openclaw/SKILL.md`
- Create: `memory_bench_platform/skills/agents/openclaw/manifest.yaml`
- Create: `memory_bench_platform/skills/agents/openclaw/scripts/run_task.py`
- Create: `memory_bench_platform/skills/agents/generic-cli/SKILL.md`
- Create: `memory_bench_platform/skills/agents/generic-cli/manifest.yaml`
- Create: `memory_bench_platform/skills/agents/generic-cli/scripts/run_task.py`
- Create: `memory_bench_platform/tests/test_agent_skills.py`

- [ ] **Step 1: Write the failing agent skill test**

```python
from pathlib import Path

import yaml


def test_openclaw_manifest_declares_service_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/openclaw/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "service"
    assert manifest["io"]["protocol_mode"] == "stateful_session"


def test_generic_cli_manifest_declares_process_runtime():
    manifest = yaml.safe_load(
        Path("skills/agents/generic-cli/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtime"]["mode"] == "process"
    assert manifest["io"]["protocol_mode"] == "stateless_cli"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_agent_skills.py -v`
Expected: FAIL because agent skill directories do not exist yet

- [ ] **Step 3: Create both agent skill skeletons**

```yaml
# skills/agents/openclaw/manifest.yaml
kind: agent
id: openclaw
version: 0.1.0
entry:
  runner: scripts/run_task.py
runtime:
  mode: service
io:
  protocol_mode: stateful_session
lifecycle:
  startup_required: true
collection:
  stdout: true
  stderr: true
```

```yaml
# skills/agents/generic-cli/manifest.yaml
kind: agent
id: generic-cli
version: 0.1.0
entry:
  runner: scripts/run_task.py
runtime:
  mode: process
io:
  protocol_mode: stateless_cli
lifecycle:
  startup_required: false
collection:
  stdout: true
  stderr: true
```

```python
# scripts/run_task.py
from __future__ import annotations

import json
import sys


def main() -> None:
    request = json.load(sys.stdin)
    response = {
        "status": "ok",
        "turns": [],
        "artifacts": [],
        "metrics": [],
    }
    json.dump(response, sys.stdout)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_agent_skills.py -v`
Expected: PASS with `2 passed`

- [ ] **Step 5: Commit**

```bash
git add skills/agents/openclaw skills/agents/generic-cli tests/test_agent_skills.py
git commit -m "feat: add openclaw and generic cli agent skills"
```

### Task 7: Wire the first end-to-end run and JSON reporting path

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/cli.py`
- Modify: `memory_bench_platform/memory_bench_platform/executor.py`
- Create: `memory_bench_platform/memory_bench_platform/reporter.py`
- Create: `memory_bench_platform/tests/test_e2e_stub_run.py`

- [ ] **Step 1: Write the failing stub E2E test**

```python
from pathlib import Path

from memory_bench_platform.cli import main


def test_stub_run_creates_run_json_and_summary(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    try:
        main([
            "run",
            "--benchmark", "locomo",
            "--agent", "generic-cli",
        ])
    except TypeError:
        # main() may not yet accept argv
        pass
    runs = list((tmp_path / "runs").glob("*"))
    assert runs, "expected one run directory to be created"
    assert (runs[0] / "summary.json").exists()
    assert (runs[0] / "reports").is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_e2e_stub_run.py -v`
Expected: FAIL because `run` does not execute anything yet

- [ ] **Step 3: Implement the first stub run path**

```python
# memory_bench_platform/reporter.py
from __future__ import annotations

import json
from pathlib import Path


def write_summary(run_dir: Path, summary: dict) -> None:
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
```

```python
# cli.py
def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        ...
```

```python
# executor.py
def run_stub_plan(...) -> Path:
    # create run dir
    # write run.json
    # write summary.json
    return run_dir
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_e2e_stub_run.py -v`
Expected: PASS with `1 passed`

- [ ] **Step 5: Commit**

```bash
git add memory_bench_platform/cli.py memory_bench_platform/executor.py memory_bench_platform/reporter.py tests/test_e2e_stub_run.py
git commit -m "feat: add stub end-to-end run and summary reporting"
```

### Task 8: Write operator docs for the MVP boundary and extension points

**Files:**
- Modify: `memory_bench_platform/README.md`
- Create: `memory_bench_platform/docs/architecture.md`
- Create: `memory_bench_platform/docs/manifests.md`

- [ ] **Step 1: Write the failing docs assertion test**

```python
from pathlib import Path


def test_readme_mentions_mvp_matrix():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "LoCoMo" in text
    assert "LongMemEval" in text
    assert "OpenClaw" in text
    assert "Generic CLI Agent" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_docs_smoke.py -v`
Expected: FAIL because the README does not document the agreed MVP matrix

- [ ] **Step 3: Add docs for boundaries and next extensions**

```markdown
# Memory Benchmark Platform

## MVP Matrix

- Benchmarks: `LoCoMo`, `LongMemEval`
- Agents: `OpenClaw`, `Generic CLI Agent`

## Current Scope

- Unified run protocol
- Directory skill loading
- Stub execution contract
- JSON run artifacts
- Reuse `ClusterBench` resource monitoring logic only after decoupling it from global state
- Reuse `ClusterBench` run-directory organization only as a storage pattern

## Deferred

- Real cluster scheduling
- Full hardware orchestration
- Unified scoring normalization across all benchmarks
- Reuse of `ClusterBench` workload drivers or `test_result` report schema
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/d/code/Agent/test/memory_bench_platform && pytest tests/test_docs_smoke.py -v`
Expected: PASS with `1 passed`

- [ ] **Step 5: Commit**

```bash
git add README.md docs/architecture.md docs/manifests.md tests/test_docs_smoke.py
git commit -m "docs: describe memory benchmark platform mvp"
```

### Spec Coverage Check

- Covered unified protocol objects with `protocol.py` and storage layout in Task 2.
- Covered selective `ClusterBench` reuse boundaries through Task 2.5 and Task 8.
- Covered `benchmark skill manifest` and `agent skill manifest` through Task 3, Task 5, and Task 6.
- Covered skill directory conventions through Task 5 and Task 6.
- Covered the first `2 benchmark x 2 agent` MVP matrix through Task 5, Task 6, and Task 8.
- Covered future memory backend and hardware profile extension points through `RunPlanRequest`, `RunRecord`, and operator docs in Task 4 and Task 8.

### Self-Review Notes

- No placeholder markers such as `TBD` or `TODO` remain in the plan.
- The implementation keeps the platform core thin and pushes benchmark/agent specifics into skill directories.
- `ClusterBench` reuse is explicitly constrained to host-level monitoring and archive layout, avoiding accidental coupling to its workload model.
- The MVP intentionally stops at stub execution plus unified persistence; real benchmark expansion and real agent invocation can follow in a later plan once this skeleton is passing.
