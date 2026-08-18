# 测试入口统一打通与稳定复用实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让仓库内与 LoCoMo / OpenClaw / OpenViking 相关的全部测试入口在当前远端环境中都能独立跑通、结果可信、无并发串口径，并沉淀为可重复执行的稳定入口。

**Architecture:** 以“统一测试入口目录 + 独占运行守护 + 共享环境探测 + 统一结果归档”作为主线，不再让 `locomo_test`、`memory_bench_platform`、`OpenViking/benchmark/locomo/openclaw` 三套逻辑各自维护一套不兼容的运行假设。先修兼容层和运行隔离，再补统一 orchestrator 与结果汇总，最后做端到端稳定性验证。

**Tech Stack:** Python 3.11、pytest、OpenClaw gateway、OpenViking server、远端 Docker 容器 `jcp-dev`、bash orchestration、CSV/JSON artifact 汇总。

---

## 文件结构与职责

- `locomo_test/locomo_test/eval.py`
  - 当前 `locomo_test` 入口的 ingest / QA / judge 主逻辑。
  - 需要去掉对旧 compact/task 统计接口的错误假设，统一接入共享运行助手。

- `locomo_test/locomo_test/config.py`
  - `locomo_test` 的环境与测试配置模型。
  - 需要补共享运行模式所需的远端锁、结果目录、服务健康策略字段。

- `locomo_test/locomo_test/pipeline.py`
  - `locomo_test` pipeline 编排器。
  - 需要接入独占运行检查、统一 run meta、统一失败恢复。

- `memory_bench_platform/memory_bench_platform/cli.py`
  - 平台主 CLI 入口。
  - 需要补充对 OpenViking 真实运行模式的稳定入口、统一 remote profile、统一结果桥接。

- `memory_bench_platform/memory_bench_platform/integration.py`
  - benchmark / agent skill 加载与接入层。
  - 需要接入“官方 benchmark 脚本入口”这一类外部 runner。

- `memory_bench_platform/memory_bench_platform/workflow.py`
  - workflow/case 执行核心。
  - 需要补独占运行、远端运行 step、结果镜像采集。

- `memory_bench_platform/memory_bench_platform/reporter.py`
  - 平台结果汇总。
  - 需要支持导入 `locomo_test` / `phase_a_off.py` 的产物并归一化展示。

- `memory_bench_platform/memory_bench_platform/storage.py`
  - run 落盘布局。
  - 需要补“外部入口镜像归档”目录约定。

- `memory_bench_platform/memory_bench_platform/resource_monitor.py`
  - 资源监控与 CPU/mem 统计。
  - 需要支持外部脚本模式下的 monitor 包装与汇总。

- `memory_bench_platform/memory_bench_platform/protocol.py`
  - 平台对象模型。
  - 需要补 `ExternalRunRecord` / `EntryPointRecord` / `EnvironmentSnapshot` 等对象。

- `memory_bench_platform/tests/*`
  - 需要新增 shared runner / external entrypoint / report import / lock recovery 的单测。

- `locomo_test/tests/`
  - 当前不存在。
  - 需要新增最小单测覆盖 compact fallback、task polling、exclusive run guard、result integrity。

- `tools/test_entrypoints/`
  - 新建统一测试入口辅助目录。
  - 存放共享远端运行脚本、容器内环境探测、锁文件管理、结果采集脚本。

- `docs/memory-benchmark-platform-wsl-validation.md`
  - 当前验证记录。
  - 需要更新为“入口可用性矩阵 + 当前稳定推荐入口 + 已知限制”。

- `docs/test-entrypoints-matrix.md`
  - 新建文档。
  - 统一列出三个入口的用途、依赖、当前状态、推荐场景、已知风险。

### 入口边界收敛

- `locomo_test`
  - 定位为“当前权威 small / locomo10 跑数入口”。
  - 输出准确率、CSV、judge 结果。

- `memory_bench_platform`
  - 定位为“统一测试平台与归档入口”。
  - 能直接跑 skill/native case，也能封装外部 benchmark runner。

- `OpenViking benchmark/locomo/openclaw`
  - 定位为“官方回归入口与底层基线脚本”。
  - 需要被平台与共享运行脚本稳定调用，而不是直接裸跑。

---

### Task 1: 建立统一入口可用性矩阵与独占运行约束

**Files:**
- Create: `docs/test-entrypoints-matrix.md`
- Create: `tools/test_entrypoints/README.md`
- Create: `tools/test_entrypoints/remote_run_lock.sh`
- Modify: `docs/memory-benchmark-platform-wsl-validation.md`

- [ ] **Step 1: 写入口矩阵文档初稿**

```md
# 测试入口可用性矩阵

| 入口 | 当前用途 | 依赖 | 当前状态 | 推荐级别 | 已知问题 |
| --- | --- | --- | --- | --- | --- |
| `locomo_test` | LoCoMo small / locomo10 主跑数入口 | gateway + openviking + judge | 可跑 | 高 | 历史兼容逻辑需清理 |
| `memory_bench_platform` | 统一平台 / case workflow / skill 验证 | benchmark skill + agent skill | 部分可跑 | 中 | external runner 未统一 |
| `benchmark/locomo/openclaw/phase_a_off.py` | 官方基线 / direct-ov 验证 | OpenViking benchmark env | 可跑但易串口径 | 中 | 并发 run 会污染插件配置 |
```

- [ ] **Step 2: 新增远端独占锁脚本**

```bash
#!/usr/bin/env bash
set -euo pipefail

LOCK_DIR="${1:?lock dir required}"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/locomo_eval.lock"

if [ -f "$LOCK_FILE" ]; then
  echo "LOCKED:$LOCK_FILE"
  exit 2
fi

trap 'rm -f "$LOCK_FILE"' EXIT
echo "$$" > "$LOCK_FILE"
shift
"$@"
```

- [ ] **Step 3: 落盘工具目录说明**

```md
# tools/test_entrypoints

- `remote_run_lock.sh`: 远端独占运行包装器
- `probe_remote_env.py`: 探测 gateway / openviking / plugin / auth profile
- `collect_run_artifacts.py`: 收集 CSV/JSON/log 到统一目录
```

- [ ] **Step 4: 更新验证文档中的入口推荐**

Run: `rg -n "当前判断|已验证通过|已验证失败" docs/memory-benchmark-platform-wsl-validation.md`
Expected: 能定位到需要改写的推荐段落

- [ ] **Step 5: 提交**

```bash
git add docs/test-entrypoints-matrix.md tools/test_entrypoints/README.md tools/test_entrypoints/remote_run_lock.sh docs/memory-benchmark-platform-wsl-validation.md
git commit -m "docs: define test entrypoint matrix and run isolation"
```

### Task 2: 提炼共享远端环境探测与恢复助手

**Files:**
- Create: `tools/test_entrypoints/probe_remote_env.py`
- Create: `tools/test_entrypoints/reset_remote_locomo_env.py`
- Create: `tools/test_entrypoints/collect_run_artifacts.py`
- Test: `memory_bench_platform/tests/test_remote_helpers.py`

- [ ] **Step 1: 为远端环境探测写失败测试**

```python
from tools.test_entrypoints.probe_remote_env import parse_openclaw_config


def test_parse_openclaw_config_reads_gateway_token_and_state_dir():
    data = {
        "gateway": {"port": 18789, "auth": {"token": "abc"}},
        "stateDir": "/root/.openclaw",
    }
    result = parse_openclaw_config(data)
    assert result["gateway_port"] == 18789
    assert result["gateway_token"] == "abc"
    assert result["state_dir"] == "/root/.openclaw"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest memory_bench_platform/tests/test_remote_helpers.py::test_parse_openclaw_config_reads_gateway_token_and_state_dir -v`
Expected: FAIL with `ModuleNotFoundError` or missing function

- [ ] **Step 3: 编写最小环境探测实现**

```python
def parse_openclaw_config(data: dict) -> dict:
    gateway = data.get("gateway", {})
    auth = gateway.get("auth", {})
    return {
        "gateway_port": gateway.get("port"),
        "gateway_token": auth.get("token", ""),
        "state_dir": data.get("stateDir") or "/root/.openclaw",
    }
```

- [ ] **Step 4: 补充 OpenViking 配置探测与 artifact 收集接口**

```python
def parse_openviking_config(data: dict) -> dict:
    server = data.get("server", {})
    vlm = data.get("vlm", {})
    return {
        "port": server.get("port"),
        "root_api_key": server.get("root_api_key", ""),
        "judge_base_url": vlm.get("api_base", ""),
        "judge_model": vlm.get("model", ""),
    }
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest memory_bench_platform/tests/test_remote_helpers.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add tools/test_entrypoints/probe_remote_env.py tools/test_entrypoints/reset_remote_locomo_env.py tools/test_entrypoints/collect_run_artifacts.py memory_bench_platform/tests/test_remote_helpers.py
git commit -m "feat: add shared remote test environment helpers"
```

### Task 3: 修复 `locomo_test` 的 OpenViking 兼容层并补本地单测

**Files:**
- Modify: `locomo_test/locomo_test/config.py`
- Modify: `locomo_test/locomo_test/eval.py`
- Modify: `locomo_test/locomo_test/pipeline.py`
- Create: `locomo_test/tests/test_eval_openviking.py`
- Create: `locomo_test/tests/test_pipeline_locking.py`

Review note:
- 已执行验证表明，当前 `OpenClaw + OpenViking` 真实稳定链路优先依赖官方 `direct-ov` ingest，而不是旧 `sessions.compact` + `/api/v1/tasks` 轮询。
- 因此这里的目标应调整为：
  1. 让 `locomo_test` 优先走官方 `direct-ov` 稳定路径；
  2. 保留旧 compact/task 链路仅作为 fallback 或兼容旧环境；
  3. 不再把旧 `/api/v1/tasks` 统计接口作为主统计真值来源。

- [ ] **Step 1: 为 compact fallback 与 task 轮询写失败测试**

```python
from locomo_test.eval import normalize_ov_task_query_mode


def test_normalize_ov_task_query_mode_prefers_direct_ov_commit_polling():
    assert normalize_ov_task_query_mode("openviking") == "direct_commit_poll"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest locomo_test/tests/test_eval_openviking.py::test_normalize_ov_task_query_mode_prefers_direct_ov_commit_polling -v`
Expected: FAIL because helper does not exist

- [ ] **Step 3: 把旧 `sessions.compact` 从强依赖改成兼容分支**

```python
def normalize_ov_task_query_mode(memory_mode: str) -> str:
    if memory_mode == "openviking":
        return "direct_commit_poll"
    return "legacy"


def should_attempt_gateway_compact(memory_mode: str) -> bool:
    return memory_mode != "openviking"
```

- [ ] **Step 4: 为 pipeline 接入独占锁**

```python
def ensure_run_lock(lock_file: Path) -> None:
    if lock_file.exists():
        raise RuntimeError(f"existing run lock: {lock_file}")
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
```

- [ ] **Step 5: 为远端 judge/stats 完整落盘补回归测试**

```python
def test_pipeline_meta_written_after_judge(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text('{"overall_accuracy": 0.85}', encoding="utf-8")
    assert '"overall_accuracy": 0.85' in meta.read_text(encoding="utf-8")
```

- [ ] **Step 6: 运行本地测试**

Run: `pytest locomo_test/tests -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add locomo_test/locomo_test/config.py locomo_test/locomo_test/eval.py locomo_test/locomo_test/pipeline.py locomo_test/tests
git commit -m "fix: stabilize locomo test openviking runner"
```

### Task 4: 让 `memory_bench_platform` 支持外部 benchmark runner

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/protocol.py`
- Modify: `memory_bench_platform/memory_bench_platform/integration.py`
- Modify: `memory_bench_platform/memory_bench_platform/cli.py`
- Modify: `memory_bench_platform/memory_bench_platform/workflow.py`
- Modify: `memory_bench_platform/memory_bench_platform/storage.py`
- Create: `memory_bench_platform/tests/test_external_entrypoints.py`

- [ ] **Step 1: 先写失败测试，要求平台识别 external runner**

```python
from memory_bench_platform.integration import classify_entrypoint


def test_classify_entrypoint_marks_official_locomo_script_as_external():
    entry = {"external_runner": "benchmark/locomo/openclaw/run_clean_small_in_container.sh"}
    assert classify_entrypoint(entry) == "external_runner"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd memory_bench_platform && pytest tests/test_external_entrypoints.py::test_classify_entrypoint_marks_official_locomo_script_as_external -v`
Expected: FAIL

- [ ] **Step 3: 在 protocol 中定义 external run record**

```python
@dataclass
class EntryPointRecord:
    entrypoint_id: str
    entrypoint_kind: str
    command: list[str]
    output_dir: str
```

- [ ] **Step 4: 在 integration 中识别 external runner**

```python
def classify_entrypoint(entry: dict) -> str:
    if entry.get("external_runner"):
        return "external_runner"
    if entry.get("case_builder"):
        return "case_builder"
    return "unknown"
```

- [ ] **Step 5: 在 CLI/workflow 中执行外部 runner 并镜像结果**

```python
subprocess.run(command, check=True, cwd=runner_cwd, env=runner_env)
mirror_tree(source_output_dir, run_root / "external_artifacts")
```

- [ ] **Step 6: 运行相关测试**

Run: `cd memory_bench_platform && pytest tests/test_external_entrypoints.py tests/test_workflow_executor.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add memory_bench_platform/memory_bench_platform/protocol.py memory_bench_platform/memory_bench_platform/integration.py memory_bench_platform/memory_bench_platform/cli.py memory_bench_platform/memory_bench_platform/workflow.py memory_bench_platform/memory_bench_platform/storage.py memory_bench_platform/tests/test_external_entrypoints.py
git commit -m "feat: support external benchmark entrypoints"
```

### Task 5: 为官方 `phase_a_off.py` / `run_clean_small_in_container.sh` 增加平台级稳定封装

**Files:**
- Create: `tools/test_entrypoints/run_official_locomo_small.sh`
- Create: `tools/test_entrypoints/run_official_locomo_sample.sh`
- Modify: `memory_bench_platform/README.md`
- Modify: `docs/test-entrypoints-matrix.md`

- [ ] **Step 1: 写可复用 wrapper 脚本**

```bash
#!/usr/bin/env bash
set -euo pipefail

MODE="${MODE:-on}"
SAMPLE="${SAMPLE:-0}"
SESSIONS="${SESSIONS:-1-4}"
RUN_ID="${RUN_ID:-official_${MODE}_sample${SAMPLE}_$(date +%Y%m%d_%H%M%S)}"

bash /home/jcp/agent/code/OpenViking/benchmark/locomo/openclaw/run_clean_small_in_container.sh
```

- [ ] **Step 2: 为 sample 级别跑法补 wrapper**

```bash
export SAMPLE=6
export SESSIONS=1-19
bash tools/test_entrypoints/run_official_locomo_small.sh
```

- [ ] **Step 3: 在 README 中声明推荐调用方式**

```md
## Official LoCoMo Entry

Use `tools/test_entrypoints/run_official_locomo_small.sh` instead of invoking
`benchmark/locomo/openclaw/run_clean_small_in_container.sh` directly.
```

- [ ] **Step 4: 手工验证 wrapper**

Run: `bash tools/test_entrypoints/run_official_locomo_small.sh`
Expected: 产生 `/tmp/official_*` 输出目录，且 `master.log` 包含 `phaseA` 行

- [ ] **Step 5: 提交**

```bash
git add tools/test_entrypoints/run_official_locomo_small.sh tools/test_entrypoints/run_official_locomo_sample.sh memory_bench_platform/README.md docs/test-entrypoints-matrix.md
git commit -m "feat: wrap official locomo benchmark entrypoints"
```

### Task 6: 统一结果归档与跨入口汇总

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/reporter.py`
- Create: `memory_bench_platform/memory_bench_platform/external_report_import.py`
- Create: `memory_bench_platform/tests/test_external_report_import.py`
- Modify: `docs/memory-benchmark-platform-wsl-validation.md`

- [ ] **Step 1: 为外部 CSV/meta 导入写失败测试**

```python
from memory_bench_platform.external_report_import import import_locomo_test_result


def test_import_locomo_test_result_reads_accuracy_and_counts(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text('{"overall_accuracy": 0.8571, "total_correct": 30, "total_graded": 35}', encoding="utf-8")
    result = import_locomo_test_result(tmp_path)
    assert result["overall_accuracy"] == 0.8571
    assert result["total_correct"] == 30
    assert result["total_graded"] == 35
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd memory_bench_platform && pytest tests/test_external_report_import.py::test_import_locomo_test_result_reads_accuracy_and_counts -v`
Expected: FAIL

- [ ] **Step 3: 实现导入器**

```python
def import_locomo_test_result(run_dir: Path) -> dict:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    return {
        "overall_accuracy": meta["overall_accuracy"],
        "total_correct": meta["total_correct"],
        "total_graded": meta["total_graded"],
        "source": "locomo_test",
    }
```

- [ ] **Step 4: 在 reporter 中输出统一 summary**

```python
summary["entrypoint_kind"] = imported["source"]
summary["overall_accuracy"] = imported["overall_accuracy"]
```

- [ ] **Step 5: 运行测试**

Run: `cd memory_bench_platform && pytest tests/test_external_report_import.py tests/test_storage_layout.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add memory_bench_platform/memory_bench_platform/reporter.py memory_bench_platform/memory_bench_platform/external_report_import.py memory_bench_platform/tests/test_external_report_import.py docs/memory-benchmark-platform-wsl-validation.md
git commit -m "feat: unify external benchmark result import"
```

### Task 7: 做三条入口的独占端到端验证并固化推荐口径

**Files:**
- Modify: `docs/memory-benchmark-platform-wsl-validation.md`
- Modify: `docs/test-entrypoints-matrix.md`
- Create: `outputs/test-entrypoint-validation-20260614.md`

- [ ] **Step 1: 独占验证 `locomo_test`**

Run:

```bash
ssh -p 10008 jcp@123.60.114.206 \
  'docker exec jcp-dev bash -lc "pkill -f phase_a_off.py || true; pkill -f openclaw-gateway || true"'
```

Expected: 无并发 LoCoMo run 残留

- [ ] **Step 2: 跑 `locomo_test` small 并记录结果**

Run: `python3 -m locomo_test.cli run configs/<stable-openviking-small>.toml`
Expected: 产出 `meta.json`、`qa_results.csv`、`pipeline.log`

- [ ] **Step 3: 跑 `memory_bench_platform` external runner 模式**

Run:

```bash
cd memory_bench_platform
python3 -m memory_bench_platform.cli run --benchmark locomo --agent openclaw --entrypoint official-small
```

Expected: 产出 `external_artifacts/` 与统一 `reports/summary.json`

- [ ] **Step 4: 跑官方 wrapper**

Run: `bash tools/test_entrypoints/run_official_locomo_small.sh`
Expected: `/tmp/official_*` 下出现 `master.log`、CSV 和最终统计

- [ ] **Step 5: 写验证报告**

```md
# 测试入口验证结果

- `locomo_test`: PASS
- `memory_bench_platform external runner`: PASS
- `official wrapper`: PASS
- 推荐主入口：`locomo_test`
- 推荐平台入口：`memory_bench_platform`
```

- [ ] **Step 6: 提交**

```bash
git add docs/memory-benchmark-platform-wsl-validation.md docs/test-entrypoints-matrix.md outputs/test-entrypoint-validation-20260614.md
git commit -m "docs: record stable test entrypoint validation"
```

---

## 自检结论

- 覆盖项：
  - 三条测试入口的职责边界
  - 远端独占运行与防串口径
  - `locomo_test` 兼容层修复
  - `memory_bench_platform` 外部 runner 接入
  - 官方 benchmark wrapper
  - 统一结果归档与最终验证

- 仍需严格执行的约束：
  - 所有远端跑数前必须先做独占清场，禁止并发 `phase_a_off.py`
  - `locomo_test` 的 OpenViking 旧 compact/task 路径必须彻底替换，不再作为统计真值来源
  - 最终“稳定可复用”必须以三条入口都通过独占验证为准，不能只看单次局部成功

Plan complete and saved to `docs/superpowers/plans/2026-06-14-test-entrypoints-unification-and-stabilization.md`. Two execution options:

1. Subagent-Driven (recommended) - 我按任务分段推进，每段完成后 review，再进入下一段
2. Inline Execution - 我在当前会话里连续执行这份计划，按阶段给你 review checkpoint

Which approach?
