# Result Analysis Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `memory_bench_platform` 增加统一结果分析模块，既能对既有 run 做离线分析，也能在平台 `run` 结束后自动产出分析结果，并保存 analysis skill 说明。

**Architecture:** 在平台核心新增 `result_analysis.py`，用一套纯函数读取 `run_dir` 中的 `summary/case_results/external_result_summary/cpu_status.csv` 并产出 `analysis.json` 与 `analysis.md`。CLI 增加 `analyze-run` 离线入口，`run` 主流程在报告落盘后自动调用同一分析函数；analysis skill 只作为说明和扩展注册层，不进入 benchmark/agent 执行协议。

**Tech Stack:** Python 3, pytest, pathlib, csv, json, markdown text rendering

---

### Task 1: 为结果分析模块写失败测试与最小夹具

**Files:**
- Create: `memory_bench_platform/tests/test_result_analysis.py`
- Modify: `memory_bench_platform/tests/test_cli_smoke.py`

- [ ] **Step 1: 写结果分析最小失败测试**

```python
import json
from pathlib import Path

from memory_bench_platform.result_analysis import analyze_run


def test_analyze_run_writes_analysis_json_and_md(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    (run_dir / "reports").mkdir(parents=True)
    (run_dir / "artifacts" / "monitor").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "source_id": "locomo:official_small",
                "source_kind": "external_benchmark_runner",
                "agent_id": "openclaw",
                "status": "failed",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "reports" / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "failed",
                "case_total": 2,
                "case_passed": 1,
                "case_failed": 1,
                "category_summary": {"1": {"correct": 0, "total": 1, "accuracy": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "reports" / "case_results.json").write_text(
        json.dumps(
            [
                {
                    "case_id": "conv-1-q1",
                    "question": "When did Alice join the support group?",
                    "expected_answer": "March 12",
                    "response": "There is no mention of that in the memory.",
                    "category": "1",
                    "passed": False,
                },
                {
                    "case_id": "conv-1-q2",
                    "question": "Where did Alice go?",
                    "expected_answer": "Paris",
                    "response": "Paris",
                    "category": "1",
                    "passed": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "artifacts" / "monitor" / "cpu_status.csv").write_text(
        "timestamp,summary_util_user,summary_util_sys,summary_util_idle\n"
        "1,10.0,5.0,85.0\n"
        "2,20.0,7.0,73.0\n",
        encoding="utf-8",
    )

    analysis = analyze_run(run_dir)

    assert analysis["overall_accuracy"] == 0.5
    assert analysis["failure_summary"]["retrieval_miss_count"] == 1
    assert analysis["resource_summary"]["cpu_user_peak"] == 20.0
    assert (run_dir / "reports" / "analysis.json").is_file()
    assert (run_dir / "reports" / "analysis.md").is_file()
```

- [ ] **Step 2: 再写一个 CPU 缺失降级测试**

```python
def test_analyze_run_tolerates_missing_cpu_monitor_file(tmp_path: Path):
    run_dir = tmp_path / "run-2"
    (run_dir / "reports").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "run-2", "source_id": "locomo", "source_kind": "benchmark_case_source", "status": "partial"}),
        encoding="utf-8",
    )
    (run_dir / "reports" / "summary.json").write_text(
        json.dumps({"run_id": "run-2", "status": "partial", "case_total": 1, "case_passed": 0, "case_failed": 1, "category_summary": {}}),
        encoding="utf-8",
    )
    (run_dir / "reports" / "case_results.json").write_text(
        json.dumps([{"case_id": "c1", "question": "Q", "expected_answer": "A", "response": "", "passed": False}]),
        encoding="utf-8",
    )

    analysis = analyze_run(run_dir)

    assert analysis["failure_summary"]["format_or_empty_count"] == 1
    assert analysis["resource_summary"]["cpu_sample_count"] == 0
```

- [ ] **Step 3: 给 CLI smoke test 加 `analyze-run` 可见性断言**

```python
from memory_bench_platform.cli import build_parser


def test_build_parser_exposes_expected_subcommands():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert {"list-skills", "plan-run", "run", "validate", "analyze-run"} <= set(choices)
```

- [ ] **Step 4: 运行测试，确认先失败**

Run: `cd /mnt/d/code/agent/test/memory_bench_platform && pytest tests/test_result_analysis.py tests/test_cli_smoke.py -v`

Expected: FAIL，提示 `memory_bench_platform.result_analysis` 不存在，且 CLI 未暴露 `analyze-run`

### Task 2: 实现核心分析模块与报告写出

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/result_analysis.py`
- Modify: `memory_bench_platform/memory_bench_platform/reporter.py`
- Test: `memory_bench_platform/tests/test_result_analysis.py`

- [ ] **Step 1: 写最小实现骨架，提供统一入口**

```python
from __future__ import annotations

from pathlib import Path


def analyze_run(run_dir: Path) -> dict:
    summary = _load_json(run_dir / "reports" / "summary.json")
    case_results = _load_json(run_dir / "reports" / "case_results.json")
    analysis = {
        "run_id": summary["run_id"],
        "status": summary["status"],
        "overall_accuracy": _compute_accuracy(summary),
        "case_total": summary["case_total"],
        "case_passed": summary["case_passed"],
        "case_failed": summary["case_failed"],
        "category_summary": summary.get("category_summary", {}),
        "failure_summary": _summarize_failures(case_results),
        "failure_buckets": _bucket_failures(case_results),
        "resource_summary": _read_cpu_summary(run_dir / "artifacts" / "monitor" / "cpu_status.csv"),
        "source_artifacts": _source_artifacts(run_dir),
        "analysis_notes": _build_notes(summary, case_results, run_dir),
    }
    write_analysis_files(run_dir, analysis)
    return analysis
```

- [ ] **Step 2: 实现失败归因规则**

```python
RETRIEVAL_MISS_PATTERNS = (
    "no information",
    "no mention",
    "don't have memory",
    "not specified",
    "没有相关信息",
    "没有提到",
)


def classify_failure(case_result: dict) -> tuple[str, str]:
    response = str(case_result.get("response", "") or "").strip()
    if not response:
        return "format_or_empty", "empty response"
    lowered = response.lower()
    if any(pattern in lowered for pattern in RETRIEVAL_MISS_PATTERNS):
        return "retrieval_miss", "memory refusal pattern detected"
    if response == str(case_result.get("question", "")).strip():
        return "unsupported_no_info", "response repeats question"
    if not case_result.get("passed") and len(response) > 0:
        return "judge_mismatch_candidate", "non-empty factual response but judged wrong"
    return "other", "unclassified"
```

- [ ] **Step 3: 实现 CPU 摘要读取**

```python
def _read_cpu_summary(csv_path: Path) -> dict:
    if not csv_path.exists():
        return {"cpu_sample_count": 0, "missing": "artifacts/monitor/cpu_status.csv"}
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    users = [float(row["summary_util_user"]) for row in rows]
    systems = [float(row["summary_util_sys"]) for row in rows]
    idles = [float(row["summary_util_idle"]) for row in rows]
    return {
        "cpu_sample_count": len(rows),
        "cpu_user_avg": round(sum(users) / len(users), 4),
        "cpu_sys_avg": round(sum(systems) / len(systems), 4),
        "cpu_idle_avg": round(sum(idles) / len(idles), 4),
        "cpu_user_peak": max(users),
        "cpu_sys_peak": max(systems),
        "cpu_idle_min": min(idles),
    }
```

- [ ] **Step 4: 在 reporter 中补分析报告写出函数**

```python
def write_analysis_json(run_dir: Path, analysis: dict) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_analysis_markdown(run_dir: Path, markdown_text: str) -> None:
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "analysis.md").write_text(markdown_text, encoding="utf-8")
```

- [ ] **Step 5: 运行测试，确认转绿**

Run: `cd /mnt/d/code/agent/test/memory_bench_platform && pytest tests/test_result_analysis.py tests/test_cli_smoke.py -v`

Expected: PASS

### Task 3: 接入 CLI 离线入口与 run 自动挂载

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/cli.py`
- Test: `memory_bench_platform/tests/test_result_analysis.py`

- [ ] **Step 1: 先补 CLI 离线分析路径的失败测试**

```python
from memory_bench_platform.cli import main


def test_cli_analyze_run_generates_analysis_files(tmp_path: Path, monkeypatch):
    run_dir = tmp_path / "run-3"
    _build_minimal_run(run_dir)
    monkeypatch.chdir(tmp_path)

    main(["analyze-run", "--run-dir", str(run_dir)])

    assert (run_dir / "reports" / "analysis.json").is_file()
    assert (run_dir / "reports" / "analysis.md").is_file()
```

- [ ] **Step 2: 在 `cli.py` 中增加 `analyze-run` 子命令**

```python
p_analyze = sub.add_parser("analyze-run")
p_analyze.add_argument("--run-dir", required=True)
```

- [ ] **Step 3: 在 `main()` 中实现离线分析分支**

```python
if args.command == "analyze-run":
    run_dir = Path(args.run_dir)
    analyze_run(run_dir)
    print(str(run_dir))
    return
```

- [ ] **Step 4: 在 `run` 主流程两条路径末尾自动挂载分析**

```python
write_summary(run_dir, summary_record.model_dump(mode="json"))
write_case_results(run_dir, case_results)
analyze_run(run_dir)
print(str(run_dir))
return
```

- [ ] **Step 5: 跑结果分析与 CLI 相关测试**

Run: `cd /mnt/d/code/agent/test/memory_bench_platform && pytest tests/test_result_analysis.py tests/test_cli_smoke.py tests/test_external_report_import.py -v`

Expected: PASS

### Task 4: 增加 analysis skill 与文档说明

**Files:**
- Create: `memory_bench_platform/skills/analysis/result-analyzer/SKILL.md`
- Create: `memory_bench_platform/skills/analysis/result-analyzer/manifest.yaml`
- Modify: `memory_bench_platform/README.md`

- [ ] **Step 1: 写 analysis skill 说明**

```md
# Result Analyzer Skill

负责对平台 run 目录做离线或自动结果分析，输出 `reports/analysis.json` 与 `reports/analysis.md`。

输入：
- `run.json`
- `reports/summary.json`
- `reports/case_results.json`
- `reports/external_result_summary.json`
- `artifacts/monitor/cpu_status.csv`

输出：
- `reports/analysis.json`
- `reports/analysis.md`
```

- [ ] **Step 2: 写 analysis skill manifest**

```yaml
kind: analysis
id: result-analyzer
version: 0.1.0
entry:
  module: memory_bench_platform.result_analysis
supports:
  benchmarks: [locomo, longmemeval, ovtest-memory, ovtest-health, ovtest-admin-memory]
  sources: [benchmark_case_source, external_benchmark_runner, native_workflow]
outputs:
  - reports/analysis.json
  - reports/analysis.md
```

- [ ] **Step 3: 在 README 增加用法**

```md
## Result Analysis

```bash
python3 -m memory_bench_platform.cli analyze-run --run-dir /path/to/run
```

Every successful `run` also writes:

- `reports/analysis.json`
- `reports/analysis.md`
```

- [ ] **Step 4: 明确 analysis skill 不进入现有 benchmark/agent loader**

```md
说明：
- `skills/analysis/` 仅作为说明层与扩展注册点。
- 当前 `load_all_skills()` 仍只加载 `benchmarks/` 与 `agents/`。
- 本次实现不要修改现有 benchmark/agent skill 加载协议。
```

- [ ] **Step 5: 跑文档与 CLI smoke 测试**

Run: `cd /mnt/d/code/agent/test/memory_bench_platform && pytest tests/test_cli_smoke.py tests/test_docs_smoke.py -v`

Expected: PASS

### Task 5: 用真实 LoCoMo run 验证离线分析与自动挂载

**Files:**
- Modify: `docs/memory-benchmark-platform-wsl-validation.md`
- Output: `memory_bench_platform/runs/locomo-openclaw-fromscratch-full/reports/analysis.json`
- Output: `memory_bench_platform/runs/locomo-openclaw-fromscratch-full/reports/analysis.md`

- [ ] **Step 1: 对真实 run 跑离线分析**

Run: `cd /mnt/d/code/agent/test/memory_bench_platform && python3 -m memory_bench_platform.cli analyze-run --run-dir runs/locomo-openclaw-fromscratch-full`

Expected: 生成 `runs/locomo-openclaw-fromscratch-full/reports/analysis.json` 与 `analysis.md`

- [ ] **Step 2: 检查分析结果关键字段**

Run: `cd /mnt/d/code/agent/test/memory_bench_platform && python3 - <<'PY'
import json
from pathlib import Path
path = Path("runs/locomo-openclaw-fromscratch-full/reports/analysis.json")
data = json.loads(path.read_text(encoding="utf-8"))
print(data["overall_accuracy"])
print(data["failure_summary"])
print(data["resource_summary"])
PY`

Expected: 输出 accuracy、失败归因统计、CPU 摘要

- [ ] **Step 3: 全量回归关键测试**

Run: `cd /mnt/d/code/agent/test/memory_bench_platform && pytest tests/test_result_analysis.py tests/test_cli_smoke.py tests/test_external_report_import.py tests/test_storage_layout.py tests/test_docs_smoke.py -v`

Expected: PASS

- [ ] **Step 4: 提交验证结论到 WSL 验证文档**

```md
- 已新增统一结果分析模块，可对既有 run 做离线分析。
- `run` 主流程结束后会自动生成 `reports/analysis.json` 与 `reports/analysis.md`。
- LoCoMo 实测结果可自动给出 accuracy、失败归因和 CPU 摘要。
```
