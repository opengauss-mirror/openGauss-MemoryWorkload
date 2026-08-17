# Memory Benchmark Platform

`memory_bench_platform` 是一个面向记忆系统和 Agent 工作流的 benchmark 测试底座。它的核心目标不是重新实现每个 benchmark 或 Agent，而是提供统一的编排、Skill 装载、执行归档、资源采样、结果分析和 HTML 报告能力，让 LoCoMo、LongMemEval、OpenClaw、OpenViking 等不同对象可以用同一套测试入口接入和复用。

仓库的官方安装入口在根目录，一次安装平台和 LoCoMo 执行层：

```bash
python3 -m pip install -e .
memory-bench --help
locomo-test --help
```

## 系统定位

平台定位为“中心编排器 + 双侧 Skill 插件 + 统一结果闭环”。

- 对 benchmark：统一样本发现、任务展开、执行入口、评分入口和结果字段映射。
- 对 Agent：统一健康检查、执行命令、输入输出协议、日志和 artifact 采集。
- 对 memory backend：统一版本记录、运行参数、写入/检索诊断、资源和耗时统计。
- 对使用者：统一 `run` 入口、统一 run 目录、统一 JSON/HTML 报告。

第一阶段优先解决工程对接问题：让不同 benchmark、不同 Agent、不同记忆系统可以稳定跑起来，并留下足够诊断证据。评分细节、硬件调度和研究型指标可以逐步增强，但不应让平台核心长出大量 benchmark/agent 特判。

## 设计理念

- 平台核心保持薄：只负责加载 Skill、生成计划、驱动执行、采集证据、归档结果和生成报告。
- 特性下沉到 Skill：benchmark、agent、memory backend 的差异通过目录型 Skill 描述和脚本实现。
- 统一到执行层：平台统一 `Run / Case / Step / Trace / Metric / JudgeResult` 等执行对象，具体 benchmark 的数据格式和评分逻辑由 Skill 或外部 runner 适配。
- 结果闭环优先：一次正式 run 至少应包含原始日志、结构化结果、资源采样、阶段耗时、评分结果、分析 JSON 和 HTML 报告。
- 诊断不污染评测：smoke、probe、consistency check 用于阻断明显坏链路或定位问题，不应替代正式 benchmark 结果。
- Python 先行，兼容未来 Go：目录、manifest、run archive、JSON schema 和命令行协议都按跨语言可读格式设计。

## 整体架构

```text
                   +-----------------------------+
                   | memory_bench_platform CLI   |
                   | plan / validate / run       |
                   +--------------+--------------+
                                  |
                                  v
                   +-----------------------------+
                   | Platform Core               |
                   | loader / planner / executor |
                   | monitor / reporter / store  |
                   +------+----------+-----------+
                          |          |
          loads benchmark |          | loads agent / memory / smoke / analysis
                          |          |
                          v          v
          +-------------------+    +-------------------+
          | Benchmark Skill   |    | Agent Skill       |
          | LoCoMo            |    | OpenClaw          |
          | LongMemEval       |    | Generic CLI       |
          | OVTest            |    | Hermes            |
          +---------+---------+    +---------+---------+
                    |                    |
                    |                    v
                    |          +-------------------+
                    |          | Memory Skill      |
                    |          | OpenViking        |
                    |          +---------+---------+
                    |                    |
                    v                    v
          +----------------------------------------+
          | Native workflow or external runner     |
          | cases / steps / locomo_test / scripts  |
          +-------------------+--------------------+
                              |
                              v
          +----------------------------------------+
          | Run Archive                             |
          | run.json / records / logs / artifacts  |
          | reports / monitor / timing / analysis  |
          +----------------------------------------+
```

## 核心组件职责

| 组件 | 职责 |
| --- | --- |
| `cli.py` | 对外命令入口，支持 skill 列举、校验、计划生成、run、smoke、score、analyze。 |
| `loader.py` / `manifests.py` | 发现并加载目录型 Skill，解析 manifest，校验 Skill 元数据。 |
| `planner.py` | 根据 benchmark、agent、memory backend、hardware profile 生成执行计划。 |
| `integration.py` | 串联 Skill 能力，解析 benchmark entrypoint，执行外部 runner 或 smoke。 |
| `workflow.py` / `workflow_operators.py` | 执行平台原生 case/step workflow，分发 Agent、Shell、HTTP、Memory 和 Poll Operator。 |
| `workflow_inputs.py` | 校验 Step 顺序和引用，并在运行时解析 `$ref`、`$template` 输入。 |
| `resource_monitor.py` | 采集 CPU、进程级内存、IO 等资源数据，并写入 `artifacts/monitor/`。 |
| `storage.py` | 建立标准 run 目录，写入 `run.json`、`records/`、`logs/`、`reports/`。 |
| `result_analysis.py` | 对 run 结果做二次分析，生成 `analysis.json`、`analysis.md`、`run_report.html`。 |
| `external_report_import.py` | 将外部 benchmark runner 的结果导入平台统一结果结构。 |
| `adapters/` 和 `*_bridge.py` | 承接具体外部系统的桥接逻辑，例如 LoCoMo/locomo_test 结果、耗时和诊断导入。 |

## Skill 体系

Skill 是平台的插件边界。每个 Skill 是一个目录，通常包含：

```text
skills/<type>/<skill-id>/
  SKILL.md          # 人可读说明：适用场景、运行假设、调试方法
  manifest.yaml     # 机器可读配置：id、版本、入口、能力、版本策略
  scripts/          # 可执行脚本：validate/build_tasks/run_task/score 等
```

当前主要 Skill 类型：

- `skills/benchmarks/`：Benchmark Skill，例如 `locomo`、`longmemeval`、`ovtest-health`。
- `skills/agents/`：Agent Skill，例如 `openclaw`、`generic-cli`、`hermes`。
- `skills/memories/`：Memory Backend Skill，例如 `openviking`。
- `skills/smoke/`：最小链路验证 Skill，例如 `locomo-openclaw-openviking-minimal`。
- `skills/analysis/`：结果诊断和分析 Skill，例如 LoCoMo small 链路诊断、OpenViking 写入诊断。

Benchmark Skill 负责：

- 声明 benchmark id、版本策略、数据集输入和可用 entrypoint。
- 校验数据文件和运行前置条件。
- 将原始样本展开为平台可执行的 case/step，或声明外部 runner。
- 提供评分脚本或结果导入字段映射。
- 定义 benchmark 特有的结果维度，例如类别、问题类型、准确率字段。

Agent Skill 负责：

- 声明 Agent id、版本策略、运行命令、健康检查和输入输出协议。
- 将平台 step 输入转换为 Agent 可接受的 CLI/API 请求。
- 解析 stdout/stderr/artifact，转换成结构化 step result。
- 记录 Agent 侧日志、token usage、错误信息和调试 artifact。

Memory Skill 负责：

- 声明记忆系统的版本策略、健康检查、配置来源和运行依赖。
- 暴露写入、commit、等待完成、recall、usage 读取等能力边界。
- 采集 memory 写入/检索的诊断证据，例如 task/session 状态、search/find 命中、usage。
- 避免让 benchmark 层关心具体记忆系统的内部索引或 chunk 策略。

## 运行数据流

平台支持两类执行路径。

### 1. 平台原生 workflow

```text
Benchmark Skill
  -> build cases / steps
  -> Platform workflow executor
  -> Agent Skill run_task / Memory Skill runner
  -> Trace / Metric / JudgeResult
  -> Analyze / Report
```

适合结构相对简单、可由平台直接展开 case/step 的 benchmark。

原生记忆链路使用串行 `single_path`：

```text
memory.ingest -> poll(memory.status) -> memory.recall -> agent -> judge
```

后续 Step 可以引用同一 Case 中已完成的前序输出：

```json
{"operation": {"$ref": "steps.memory-ingest.output.operation"}}
{"question": {"$template": "Evidence: {{ steps.memory-recall.output.evidence_text }}"}}
```

`$ref` 保留原始类型；`$template` 只接受标量占位值并返回字符串。引用范围限于 `run.*`、`case.*` 和当前 Case 的前序 `steps.*`。平台在执行第一个 Case 前拒绝未来引用、跨 Case 引用、非法 Poll probe 和可重试的 `memory.ingest`。

### 2. 外部 runner 导入

```text
Benchmark Skill entrypoint
  -> external runner, for example locomo_test
  -> external_artifacts/
  -> import_external_result
  -> reports/case_results.json
  -> analysis + run_report.html
```

适合 LoCoMo + OpenClaw + OpenViking 这类已有复杂启动、鉴权、隔离 runtime、memory 诊断和结果文件的链路。此时 `memory_bench_platform` 仍是主入口和归档报告底座，`locomo_test` 是 benchmark 专用执行层，不是并行平台。

## 使用方式

建议从 `memory_bench_platform` 目录运行，并设置 `PYTHONPATH=.`：

```bash
cd /mnt/d/code/Agent/test/memory_bench_platform
PYTHONPATH=. python3 -m memory_bench_platform.cli list-skills
```

### 校验 Skill

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli validate --benchmark locomo
PYTHONPATH=. python3 -m memory_bench_platform.cli validate --benchmark longmemeval
PYTHONPATH=. python3 -m memory_bench_platform.cli validate --agent openclaw
PYTHONPATH=. python3 -m memory_bench_platform.cli validate --agent generic-cli
PYTHONPATH=. python3 -m memory_bench_platform.cli validate --smoke locomo-openclaw-openviking-minimal
```

### 生成运行计划

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli plan-run \
  --benchmark locomo \
  --agent openclaw \
  --memory-backend openviking
```

### 运行 smoke gate

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli run-smoke \
  --smoke locomo-openclaw-openviking-minimal
```

也可以在正式 benchmark 前挂 smoke gate：

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent openclaw \
  --entrypoint locomo_test_remote \
  --smoke-gate locomo-openclaw-openviking-minimal
```

### 运行 LoCoMo + OpenClaw + OpenViking

OpenViking ingest 路径：

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent openclaw \
  --memory-backend openviking \
  --entrypoint locomo_test_remote \
  --run-id locomo-ov-ingest-small
```

### 运行原生 OpenViking 记忆闭环

先配置服务地址、凭据和 identity。凭据只通过进程环境传给 Memory runner，不写入 runtime context：

```bash
export OPENVIKING_API_URL=http://127.0.0.1:1933
export OPENVIKING_API_KEY=<user-or-root-key>
export OPENVIKING_ACCOUNT_ID=<account-id>
export OPENVIKING_USER_ID=<user-id>
export OPENVIKING_AGENT_ID=<agent-id>
export OPENVIKING_BIN=ov

PYTHONPATH=. python3 -m memory_bench_platform.cli run \
  --benchmark ovtest-memory \
  --agent generic-cli \
  --memory-backend openviking
```

该 smoke workload 写入固定事实，轮询写入完成状态，执行 Recall，再把 `evidence_text` 注入 Agent 输入。`records/traces.json` 保存每次 `poll_probe`，`records/metrics.json` 保存 Poll 和 Memory runner 指标。

OpenClaw 完整执行 LoCoMo 路径：

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent openclaw \
  --memory-backend openviking \
  --entrypoint openclaw_import \
  --run-id locomo-openclaw-full-small
```

LoCoMo Native Workflow 默认使用 LLM Judge，不再使用字符串精确或包含匹配。运行前需要提供：

```bash
export LOCOMO_API_KEY=<judge-api-key>
export LOCOMO_BASE_URL=<openai-compatible-base-url>
export LOCOMO_METRIC_MODEL=<judge-model>
```

Judge 配置由 Benchmark manifest 声明并进入 Run Contract。每道 QA 完成后，平台直接生成正式 `JudgeResult`，统一写入 `records/judge_results.json`、`reports/summary.json`、`reports/case_results.json` 和 HTML 报告。缺少 Judge 配置时，Case 会标记为 `judge-config-missing`，不会回退到字符串判断。

### 运行 LongMemEval

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli run \
  --benchmark longmemeval \
  --agent generic-cli
```

### 分析已有 run

```bash
PYTHONPATH=. python3 -m memory_bench_platform.cli analyze-run \
  --run-dir /mnt/d/code/Agent/test/memory_bench_platform/runs/<run-id>
```

## 结果目录和报告

每次 run 默认写入：

```text
runs/<run-id>/
  run.json
  records/
    run_contract.json
    version_selection.json
    cases.json
    steps.json
    step_results.json
    traces.json
    metrics.json
    artifacts.json
    external_entrypoint.json
  logs/
    external_runner.stdout.log
    external_runner.stderr.log
  artifacts/
    monitor/
      samples.jsonl
      summary.json
  external_artifacts/
    <entrypoint>/
  reports/
    summary.json
    case_results.json
    external_result_summary.json
    analysis.json
    analysis.md
    run_report.html
    timing_report.json
    timing_report.html
```

重点报告：

- `reports/run_report.html`：主报告，包含准确率、case 明细、资源摘要、阶段耗时卡片、CPU/内存/IO 曲线、阶段时间轴和关键诊断。
- `reports/timing_report.html`：细化耗时报告，展示 ingest、QA、recall、LLM、consistency check 等阶段的调用层级和耗时分布。
- `reports/analysis.json`：机器可读分析结果，适合后续自动汇总。
- `artifacts/monitor/`：资源采样原始数据，包含进程级 CPU、内存和 IO 采样。

## 当前已接入能力

- Benchmark：`LoCoMo`、`LongMemEval`、`OVTest health/memory/admin-memory`。
- Agent：`OpenClaw`、`Generic CLI Agent`、`Hermes`。
- Memory backend：`OpenViking`。
- Smoke：`locomo-openclaw-openviking-minimal` 最小链路验证。
- 报告：统一 summary、case results、analysis、run report、timing report。
- 监控：运行级资源采样，支持 CPU、进程级内存和 IO 指标归档与图表展示。
- 版本：Skill manifest 中声明版本策略，run 中归档默认策略、选择结果和实际观测版本。

近期验证过的 LoCoMo small 入口：

```text
runs/locomo-openclaw-full-small-20260701g-newkey/
  status: passed
  accuracy: 18/35 = 51.43%
  memories: 18
  entrypoint: openclaw_import
  report: reports/run_report.html
  timing: reports/timing_report.html
```

该结果说明 OpenClaw 完整执行 LoCoMo 的新入口已经可以接入平台闭环，并能输出 QA session、ingest timing、资源采样和 HTML 报告。准确率是否达到目标阈值仍取决于被测链路、模型服务、记忆写入/检索质量和数据口径，不应只凭 run 成功判定质量达标。

## 如何接入新的 Benchmark

1. 新建目录：

```text
skills/benchmarks/<benchmark-id>/
  SKILL.md
  manifest.yaml
  scripts/validate.py
  scripts/build_tasks.py
  scripts/score_predictions.py
```

2. 在 `manifest.yaml` 中声明：

- `id`、`name`、`version`、`description`
- 数据集输入要求
- 支持的 entrypoint
- 输出字段映射
- score/judge 入口
- `version_policy`

3. 如果 benchmark 可以直接展开 case/step，实现 `build_tasks.py`。

4. 如果 benchmark 已有独立 runner，在 manifest 中声明 external runner，并让 runner 输出平台可导入的结果文件。

5. 增加最小测试，至少覆盖：

- manifest 可加载
- validate 可执行
- sample 数据可展开
- score/import 字段完整
- run 目录能生成 `summary.json`、`case_results.json`、`run_report.html`

## 如何接入新的 Agent

1. 新建目录：

```text
skills/agents/<agent-id>/
  SKILL.md
  manifest.yaml
  scripts/healthcheck.py
  scripts/run_task.py
```

2. 在 `manifest.yaml` 中声明：

- Agent id、版本和版本策略
- CLI/API 启动方式
- 输入协议和输出协议
- 需要采集的 artifact
- healthcheck 入口

3. `run_task.py` 应将平台 step 输入转换为 Agent 请求，并输出结构化结果：

- answer / text output
- stdout / stderr
- token usage
- error detail
- artifact paths

4. 若 Agent 自带 memory backend 或复杂生命周期，相关能力应下沉到 Agent Skill 或 Memory Skill，不要写进 Benchmark Skill。

## 如何接入新的 Memory Backend

1. 新建目录：

```text
skills/memories/<memory-id>/
  SKILL.md
  manifest.yaml
  scripts/run_operation.py
```

2. 明确 memory backend 的能力边界：

- 初始化和健康检查
- ingest accepted 信号
- completed/drain 等待
- recall/search/find
- token usage 和 task/session usage 读取
- consistency check 或 reindex/flush 能力

3. 在 manifest 的 `entry.runner` 声明统一入口。Runner 从 stdin 读取：

```json
{
  "task_id": "memory-recall",
  "action": "recall",
  "inputs": {"query": "preferred language"},
  "runtime_context": {},
  "idempotency_key": "run:case:step"
}
```

Runner 向 stdout 写入 `status`、`state`、`operation`、`output`、`metrics`、`artifacts` 和 `error`。脚本必须输出单个 JSON 对象，并从环境读取凭据。错误、Artifact 和默认输出不得包含 API key 或 ingest 原文。

4. Benchmark 层只应知道 session、question、answer、evidence 等执行语义，不应依赖具体 chunk 切分或索引实现。

## 配置和版本策略

真实 benchmark 默认使用被测软件的最新官方 release tag。允许覆盖版本，但必须在 run archive 中记录。

推荐 manifest 结构：

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
      version_source: upstream_release_tag
      upstream: https://github.com/openclaw/openclaw
    - name: openviking
      scope: memory_backend
      version_source: upstream_release_tag
      upstream: https://github.com/volcengine/OpenViking
  record_runtime_version: true
```

运行产物中应至少记录：

- Skill 声明的版本策略
- 默认或覆盖后的版本选择结果
- 实际运行时观测到的软件版本
- 如果使用非 release build，必须在结论或分析报告中显式标注

## 开发和验证

常用轻量检查：

```bash
cd /path/to/openGauss-MemoryWorkload/memory_bench_platform
PYTHONPATH=. python3 -m memory_bench_platform.cli list-skills
PYTHONPATH=. python3 -m memory_bench_platform.cli validate \
  --benchmark locomo \
  --data-path /path/to/locomo.json
PYTHONPATH=. python3 -m memory_bench_platform.cli validate --agent openclaw
PYTHONPATH=. python3 -m memory_bench_platform.cli validate --smoke locomo-openclaw-openviking-minimal
PYTHONPATH=. pytest -q
```

测试文件通过项目路径定位 Skill、工具脚本和 Fixture，不依赖特定机器上的绝对路径。
LoCoMo 数据不随测试包隐式提供，验证和正式运行时应显式传入 `--data-path`。
