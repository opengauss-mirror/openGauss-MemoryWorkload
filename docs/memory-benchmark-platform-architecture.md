# 记忆评测平台架构设计说明

## 1. 背景与目标

本平台用于基于 Agent 构建记忆系统测试能力，目标是支持不同 benchmark、不同 Agent、不同记忆系统、不同硬件平台的自动化测试，并统一收集过程证据和结果指标。

本文明确区分两层含义：

- `目标架构`：平台最终应该稳定收敛到的职责分层与协议边界
- `MVP 当前实现`：当前代码已经打通的最小链路，以及尚未落地的接口

第一版采用工程对接优先的 MVP 路线，先覆盖：

- Benchmark: `LoCoMo`、`LongMemEval`
- Agent: `OpenClaw`、`Generic CLI Agent`
- 结果: 统一保存 `Run / Task / Turn / Artifact / MetricRecord / JudgeResult`
- 扩展点: 预留 memory backend 与 hardware profile 字段

MVP 暂不做：

- 跨 benchmark 的统一评分归一化
- 完整硬件调度器
- 把所有 Agent 强制改造成同一 SDK/API
- 复用 `ClusterBench` 的 workload driver 主模型

## 2. 总体架构

平台采用“中心编排器 + 双侧 skill 插件 + 统一运行协议 + 采集归档层”的结构。

```text
                    +----------------------------------+
                    |          Benchmark Skills        |
                    |----------------------------------|
                    | LoCoMo / LongMemEval / ...       |
                    | - 数据准备                       |
                    | - 任务展开                       |
                    | - 执行约束声明                   |
                    | - 评分语义与评分入口             |
                    +----------------+-----------------+
                                     |
                                     v
+-------------------+    +----------------------------------+    +-------------------+
|   Config / CLI    |--->|           Orchestrator           |<---|    Agent Skills   |
|-------------------|    |----------------------------------|    |-------------------|
| run spec          |    | - 加载 skills                    |    | OpenClaw / CLI... |
| matrix selection  |    | - 校验 manifests/schemas         |    | - 启动/停止 agent |
| runtime options   |    | - 生成 run plan                  |    | - 执行 task       |
+-------------------+    | - 驱动 benchmark + agent         |    | - 会话管理        |
                         | - 协调采集/评分/归档             |    | - 输出采集适配    |
                         +----------------+-----------------+    +---------+---------+
                                          |                                |
                                          v                                v
                              +---------------------------+
                              |       Run Protocol        |
                              |---------------------------|
                              | Run / Task / Turn         |
                              | ExecutionSpec             |
                              | RenderedTaskInput         |
                              | JudgeInput / JudgeResult  |
                              | Artifact / MetricRecord   |
                              +-------------+-------------+
                                            |
                                            v
                              +---------------------------+      +-------------------+
                              |        Agent Skill        |----->|   Agent Runtime   |
                              |---------------------------|      |-------------------|
                              | transport / session       |      | service/process   |
                              | output adaptation         |      | http/cli/stdin    |
                              +-------------+-------------+      +---------+---------+
                                            |                              |
                                            +---------------+--------------+
                                                            |
                                                            v
                                   +----------------------------------------------+
                                   |         Collector / Judge / Monitor          |
                                   |----------------------------------------------|
                                   | - stdout/stderr/logs                         |
                                   | - tokens/time/exit_code                      |
                                   | - host resource monitor                      |
                                   | - judge/scorer                               |
                                   +-------------------+--------------------------+
                                                       |
                                                       v
                                   +----------------------------------------------+
                                   |             Storage / Run Archive            |
                                   |----------------------------------------------|
                                   | runs/<run_id>/run.json                      |
                                   | records/  artifacts/  logs/                 |
                                   | reports/  config_snapshot/                  |
                                   +----------------------------------------------+
```

核心原则是：benchmark 差异放在 `Benchmark Skill`，agent 差异放在 `Agent Skill`，平台核心只做装载、编排、统一协议和归档。

需要强调：

- 上图描述的是 `目标架构`
- 当前 MVP 已打通的是 `validator / task_builder / runner / healthcheck / archive` 最小链路
- `ExecutionSpec` 调度、`RenderedTaskInput` 由 benchmark skill 显式产出、`JudgeInput` 组装与 scorer 编排仍属于下一阶段补齐项

## 3. 组件职责

| 组件 | 职责 | 不负责 |
|---|---|---|
| `Config / CLI` | 接收 run 配置，选择 benchmark、agent、split、sample 范围、runtime 参数 | 不解释 benchmark 数据，不管理 agent runtime |
| `Orchestrator` | 加载 skills，校验 manifest/schema，生成 run plan，协调执行、采集、评分、归档 | 不写 benchmark 特判，不写 agent 私有交互逻辑 |
| `Run Protocol` | 定义平台内部统一对象：`Run / Task / Turn / ExecutionSpec / RenderedTaskInput / JudgeInput / Artifact / MetricRecord / JudgeResult` | 不承载 benchmark 原始格式，不承载 agent 私有协议 |
| `Benchmark Skill` | 定义“测什么”：数据准备、任务展开、执行约束、评分语义、评分入口 | 不启动 agent，不管理进程/服务 |
| `Agent Skill` | 定义“怎么执行”：启动、健康检查、任务执行、session 管理、输出适配 | 不解析 benchmark 原始数据，不定义评分语义 |
| `Agent Runtime` | 被测的具体 agent 运行实体，例如服务、进程、CLI | 不参与平台调度决策 |
| `Collector / Judge / Monitor` | 收集 stdout/stderr/logs/artifacts，收集 tokens/time/exit code，执行 judge/scorer，采集主机资源 | 不拥有 benchmark 或 agent 私有业务规则 |
| `Storage / Run Archive` | 按统一目录持久化 records、artifacts、reports、config snapshot | 不决定评测语义 |

## 4. 关键边界

这些边界是架构正确性的核心。

| 事项 | Benchmark Skill | Platform Core | Agent Skill |
|---|---|---|---|
| benchmark 身份、版本、split 定义 | 负责 | 读取和校验 | 不负责 |
| 原始数据定位、校验、预处理 | 负责 | 调用入口 | 不负责 |
| 原始数据转换为 `Task/Turn` | 负责 | 提供目标协议 | 不负责 |
| 执行约束：单轮/多轮、隔离、并发、stateful | 声明 | 调度落实 | 不决定 |
| `ExecutionSpec` 字段定义 | 声明所需执行语义 | 定义 schema 并调度落实 | 读取并执行 |
| agent 输入内容模板中的 benchmark 语义 | 负责产出 `RenderedTaskInput` 语义内容 | 传递统一字段 | 负责传输落地 |
| gold answer、category、评分语义 | 负责 | 不负责 | 不负责 |
| `JudgeInput` 组装规则 | 定义需要哪些 benchmark 侧字段 | 负责组装并传给 scorer/judge | 不负责 |
| scorer/judge 入口 | 提供 | 调用和编排 | 不负责 |
| 启动/停止/健康检查 agent | 不负责 | 调度 | 实现 |
| session/runtime/stdin/http/file 交互 | 不负责 | 不实现细节 | 负责 |
| stdout/stderr/trace/answer 收集 | 声明 benchmark 特有 artifact | 统一收口 | 产出和适配 |
| host 资源监控 | 不负责 | 挂载监控 | 不负责 |
| run 目录与 records schema | 不负责 | 负责 | 不负责 |

硬规则：

- `Benchmark Skill` 不能直接启动或管理 Agent。
- `Agent Skill` 不能解析 benchmark 原始数据。
- `Platform Core` 不能出现 `if benchmark == locomo` 这类业务特判。
- `Run Protocol` 是两侧插件之间唯一共享的内部契约。

## 5. 交互流程

一次 run 的控制流如下：

```text
1. CLI 接收 run 请求
2. Orchestrator 加载选定的 Benchmark Skill 和 Agent Skill
3. Benchmark Skill 校验并准备数据集
4. Benchmark Skill 将数据展开为 Task/Turn records
5. Orchestrator 创建 Run 和执行计划
6. Agent Skill 通过具体 Agent Runtime 执行 Tasks
7. Agent Skill 将 runtime 原始输出适配为 protocol 输出
8. Collector 收集输出、日志、artifacts 和指标
9. Orchestrator 基于 `Task + RenderedTaskInput + agent output + benchmark metadata` 组装 `JudgeInput`
10. Orchestrator 调用 Benchmark Skill 提供的 scorer/judge 入口
11. Judge/Scorer 产出 JudgeResult
12. Storage 将所有输出持久化到 run 目录
```

数据流如下：

```text
Benchmark Skill -> Task/Turn -> Orchestrator
Benchmark Skill -> ExecutionSpec / RenderedTaskInput -> Orchestrator
Orchestrator -> Agent Skill -> Agent Runtime
Agent Runtime -> raw outputs/artifacts/metrics -> Agent Skill
Agent Skill -> protocol outputs/artifacts/metrics -> Collector
Benchmark Skill -> scorer entry + scoring semantics -> Orchestrator
Orchestrator -> JudgeInput -> Judge/Scorer
Collector + Judge + Resource Monitor -> Storage
Storage -> run records + reports
```

### 5.1 MVP 当前实现边界

当前代码已经完成的链路：

- 加载 benchmark / agent skill manifest
- 调用 benchmark validator 与 task builder
- 调用 agent healthcheck 与 runner
- 写出 `run.json`、`records/tasks.json`、`artifacts/agent-output.json`、`reports/summary.json`
- 对 `LoCoMo + Generic CLI`、`LoCoMo + OpenClaw` 跑通最小执行链路

当前代码尚未完成但已在目标架构中固定的链路：

- benchmark skill 直接产出标准化 `RenderedTaskInput`
- orchestrator 根据 `ExecutionSpec` 落实单轮/多轮/隔离/并发语义
- orchestrator 组装 `JudgeInput`
- orchestrator 调用 benchmark scorer/judge 并写出 `JudgeResult`
- collector 将 stdout/stderr/resource monitor 统一写入结构化 records

因此，当前实现应理解为：

- `架构方向已固定`
- `MVP 执行主链已打通`
- `评分编排与执行约束编排尚未闭环`

## 6. Run Protocol

`Run Protocol` 是平台内部稳定契约，用于隔离 benchmark 和 agent 的差异。

| 对象 | 含义 |
|---|---|
| `Run` | 一次 benchmark 对一个 agent、在一组配置下的完整执行 |
| `Task` | 从 benchmark sample 派生出的一个可执行单元 |
| `Turn` | Task 内的一次对话或交互步骤 |
| `ExecutionSpec` | 一个 task/run 级执行约束对象，定义多轮、隔离、并发、stateful 等语义 |
| `RenderedTaskInput` | 一个已经带有 benchmark 语义的统一输入对象，等待 Agent Skill 传输落地 |
| `JudgeInput` | 一个给 scorer/judge 的统一输入对象，包含 task、agent output、gold 和所需元数据 |
| `Artifact` | 一个被持久化的原始输出文件、日志、trace 或中间文件 |
| `MetricRecord` | 一个挂在 run/task/turn 范围上的数值或类别指标 |
| `JudgeResult` | 一个针对 Task 或 Run 的评分、标签或通过失败解释 |

协议存在的原因：

- 防止 benchmark 特有逻辑泄漏进 orchestrator。
- 防止 agent runtime 细节泄漏进 benchmark 处理过程。
- 让执行约束、输入模板和评分输入成为显式契约，而不是散落的临时字段。
- 让归档和报告依赖稳定输出，而不是依赖具体插件内部格式。

## 7. Skill 模型

Skill 使用目录型混合结构，包含人可读说明和机器可读配置。

```text
skills/
  benchmarks/
    locomo/
      SKILL.md
      manifest.yaml
      scripts/
      schemas/
    longmemeval/
      SKILL.md
      manifest.yaml
      scripts/
      schemas/
  agents/
    openclaw/
      SKILL.md
      manifest.yaml
      scripts/
      schemas/
    generic-cli/
      SKILL.md
      manifest.yaml
      scripts/
      schemas/
```

`Benchmark Skill` 必须提供：

- 身份与版本
- 数据集准备
- 任务展开
- 执行约束（产出 `ExecutionSpec`）
- 输入模板语义（产出 `RenderedTaskInput`）
- 评分语义
- scorer/judge 入口
- `JudgeInput` 所需字段约定
- benchmark 特有 artifact 声明

`Agent Skill` 必须提供：

- 身份与版本
- startup/healthcheck/teardown
- task 执行方式
- session 处理模型
- `RenderedTaskInput` 到具体 transport 的映射
- 输出采集适配
- runtime artifact 声明

### 7.1 当前 manifest 与目标 manifest 的关系

当前代码中的 manifest 已经承载：

- 身份与版本
- 基本 entrypoints
- dataset / execution / judging / runtime / io / lifecycle / collection 的最小声明

但还没有强约束以下接口，因此这部分仍属于 `目标 manifest`：

- benchmark 侧 `render_input` 入口
- benchmark 侧 `judge_input_mapper` 或等价字段约定
- benchmark 侧 scorer 路由与产出 schema
- agent 侧显式 `transport_mode` / `session_selector` / `output_schema`
- 两侧对 artifact 类型和结构化 metric 的 schema 约束

下一阶段建议把 manifest 从“自由 dict + 脚本路径”收敛到“显式字段 + 可校验 schema”，否则 orchestrator 仍会被迫依赖脚本隐式约定。

## 8. 存储与归档

MVP 的 run 目录结构如下：

```text
runs/<run_id>/
  run.json
  records/
  artifacts/
  logs/
  reports/
  config_snapshot/
```

各目录含义：

| 路径 | 内容 |
|---|---|
| `run.json` | 本次 run 的元信息、配置、状态 |
| `records/` | 结构化协议记录，例如 task、turn、metric、judge result |
| `artifacts/` | 原始产物，例如回答文件、trace、临时 JSON、CSV |
| `logs/` | 平台、skill、agent runtime 的日志 |
| `reports/` | 汇总报告，例如 `summary.json`、统计结果 |
| `config_snapshot/` | 本次运行相关配置快照 |

这个布局借鉴 `ClusterBench` 的“每次 run 归档所有证据”的思路，但不复用它的 `test_result` schema。

## 9. ClusterBench 复用边界

`ClusterBench` 可以提供参考，但不能成为平台核心。

允许复用：

- 主机级资源监控逻辑
- CPU、内存、磁盘、网络采集思路
- CSV 落盘方式
- run 目录组织模式

禁止复用：

- workload driver 抽象
- L1/L2 mode 模型
- tile / QoS / score metric 主模型
- `test_result` report schema

原因是 `ClusterBench` 的主模型是基础设施 workload 压测，而本平台的主模型是 benchmark 到 agent 的评测执行。

## 10. 实现顺序

建议实现顺序如下：

1. `protocol + storage`
2. `manifest + loader`
3. `benchmark skills`
4. `agent skills`
5. `orchestrator / executor`
6. `collector / judge / monitor`
7. `archive / report`

这个顺序优先固定共享契约和边界，再接具体 benchmark 与 agent，最后补采集、评分和报告。

## 11. 风险与处理

| 风险 | 影响 | 处理方式 |
|---|---|---|
| Benchmark Skill 太薄 | benchmark 语义泄漏进 orchestrator | manifest 必须声明数据准备、任务展开、执行约束、评分语义 |
| Agent Skill 太薄 | agent runtime 逻辑泄漏进 orchestrator | agent manifest 必须声明启动、执行、session、输出适配 |
| Run Protocol 太抽象 | 丢失执行所需细节 | 增加 `ExecutionSpec`、`RenderedTaskInput`、`JudgeInput` 并用 LoCoMo、LongMemEval、OpenClaw、Generic CLI 反向校验 |
| ClusterBench 复用过宽 | 平台被 workload 压测模型污染 | 只复用资源监控和归档布局，不复用 workload/report 模型 |
| 混合评分 benchmark | judge 编排边界模糊 | 后续在 benchmark manifest 中加入 scorer 路由规则 |

## 12. Review 结论

当前架构方向没有阻塞性冲突，但原说明存在两处需要修正的 P1/P2 问题，现已在文档中明确：

- `P1`：文档原先把 benchmark skill 的 `ExecutionSpec / RenderedTaskInput / JudgeInput / scorer` 说成已经由 manifest 契约承载，但当前实现还没有把这些能力收敛成强约束接口
- `P2`：文档原先没有区分 `目标架构` 和 `MVP 当前实现`，容易让读者误解当前 orchestrator 已经完成执行约束编排和评分编排

修正后结论：

- 目标架构仍然成立，双侧 skill + 统一 run protocol 的主线正确
- 当前 MVP 已足以作为最小对接底座
- 下一阶段需要优先补的是 “manifest 强约束 + RenderedTaskInput/JudgeInput/scorer 闭环”，而不是继续堆 benchmark/agent 特判

需要在下一阶段继续细化的点：

- benchmark manifest 的强约束字段与 schema
- 混合评分 benchmark 的 scorer/judge 路由字段
- memory backend 与 hardware profile 在 run 配置中的最小表达方式
