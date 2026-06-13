# 记忆评测平台架构方案

> **For agentic workers:** 必选子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐步执行本计划。步骤使用复选框语法 `- [ ]` 进行跟踪。

**目标：** 在开始实现前，明确记忆评测平台的端到端架构，包括组件职责、交互边界，以及 MVP 的执行模型。

**架构：** 平台围绕一个轻量 orchestrator、一套统一 run protocol、定义“测什么”的 benchmark skill、定义“怎么执行”的 agent skill，以及负责保存运行证据的采集/归档层来组织。`Run Protocol` 除了 `Run / Task / Turn / Artifact / MetricRecord / JudgeResult` 之外，还必须显式承载 `ExecutionSpec`、`RenderedTaskInput` 和 `JudgeInput`。`ClusterBench` 不是平台核心；只有它的主机级资源监控逻辑和运行目录组织模式可以选择性复用。

**技术栈：** Python 3.11+、YAML manifest、JSON record、目录型 skill、主机级资源监控

---

### Task 1: 固定系统上下文与顶层设计约束

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 写清架构目标与范围**

```text
目标:
- 支持多个 benchmark
- 支持多个 agent
- 为未来 memory backend 和 hardware 维度预留扩展点
- 保持平台核心足够薄

MVP 不做:
- 跨 benchmark 的统一评分归一化
- 完整硬件调度器
- 复用 ClusterBench 的 workload driver 模型
```

- [ ] **Step 2: 固定架构风格**

```text
风格:
- 一个中心 orchestrator
- 两侧插件：benchmark skills 和 agent skills
- 一套内部 run protocol
- 一条统一的采集/归档链路
```

- [ ] **Step 3: 写清硬边界规则**

```text
硬规则:
- Benchmark skill 定义“测什么”
- Agent skill 定义“怎么执行”
- Platform core 不能包含 benchmark 特判分支
- Platform core 不能包含 agent 私有 runtime 逻辑
- ClusterBench 的复用仅限资源监控和归档布局思路
```

### Task 2: 定义组件模型

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 定义核心组件**

```text
核心组件:
1. Config / CLI
2. Orchestrator
3. Run Protocol
4. Benchmark Skills
5. Agent Skills
6. Agent Runtime
7. Collector / Judge / Resource Monitor
8. Storage / Run Archive
```

- [ ] **Step 2: 写组件职责表**

```text
Config / CLI
- 接收 run 配置
- 选择 benchmark、agent、split、sample 范围和 runtime 参数

Orchestrator
- 加载 skills
- 校验 manifest 和 schema
- 生成 run plan
- 协调执行和生命周期
- 触发采集和归档

Run Protocol
- 定义内部对象：Run、Task、Turn、ExecutionSpec、RenderedTaskInput、JudgeInput、Artifact、MetricRecord、JudgeResult
- 隔离 benchmark 侧和 agent 侧的差异

Benchmark Skills
- 准备数据集
- 将原始 sample 展开为 Task/Turn
- 声明执行约束并产出 ExecutionSpec
- 产出 RenderedTaskInput 语义内容
- 定义评分语义、JudgeInput 需求和 scorer 入口

Agent Skills
- 启停具体 agent
- 将 Task 执行到 agent runtime
- 管理 session 和 runtime 生命周期
- 把 RenderedTaskInput 落到具体 transport
- 把原始输出适配成 protocol 输出

Agent Runtime
- 被测的具体 service/process/CLI 目标

Collector / Judge / Resource Monitor
- 收集 stdout/stderr/logs/artifacts
- 收集 tokens/time/exit code
- 收集主机资源指标
- 执行被 orchestrator 调用的 judge/scorer 运行时

Storage / Run Archive
- 持久化结构化 records
- 持久化原始 artifacts
- 持久化 reports 和配置快照
```

- [ ] **Step 3: 固定组件边界**

```text
边界规则:
- Benchmark skills 不负责启动或管理 agents
- Agent skills 不负责解析原始 benchmark 数据集
- Orchestrator 驱动两侧，但不拥有任一侧的私有逻辑
- Run Protocol 是唯一共享的内部契约
- Judge/scorer 的调用权属于 orchestrator，不属于 collector 自主决策
```

### Task 3: 定义交互模型

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 写控制流顺序**

```text
1. CLI 接收 run 请求
2. Orchestrator 加载选定的 benchmark skill 和 agent skill
3. Benchmark skill 校验并准备数据集
4. Benchmark skill 将数据集展开为 Task/Turn records，并产出 ExecutionSpec 与 RenderedTaskInput
5. Orchestrator 创建 Run 和执行计划
6. Agent skill 通过具体 runtime 执行 Tasks
7. Agent skill 将 runtime 原始输出适配为 protocol 输出
8. Collector 收集输出和指标
9. Orchestrator 组装 JudgeInput
10. Orchestrator 调用 benchmark skill 提供的 scorer/judge 入口
11. Judge/scorer 产出 JudgeResult
12. Storage 将所有输出持久化到 run 目录
```

- [ ] **Step 2: 写数据流关系**

```text
Benchmark Skill -> Task/Turn/ExecutionSpec/RenderedTaskInput -> Orchestrator
Orchestrator -> Agent Skill -> Agent Runtime
Agent Runtime -> raw outputs/artifacts/metrics -> Agent Skill
Agent Skill -> protocol outputs/artifacts/metrics -> Collector
Benchmark Skill -> scorer entry + JudgeInput rules -> Orchestrator
Orchestrator -> JudgeInput -> Judge/Scorer
Collector + Judge + Resource Monitor -> Storage
Storage -> run records + reports
```

- [ ] **Step 3: 固定编排语义**

```text
语义:
- Orchestrator 负责 run 生命周期
- Benchmark skill 负责测试语义
- Agent skill 负责执行语义
- Collector 负责证据采集
- Storage 负责持久化布局
```

### Task 4: 定义 run protocol 作为共享契约

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 定义最小共享对象**

```text
Run
- 一次 benchmark 对一个 agent、在一组配置下的完整执行

Task
- 从一个 benchmark sample 派生出的一个可执行单元

Turn
- Task 内的一次对话或交互步骤

ExecutionSpec
- Task 或 Run 级执行约束对象，定义单轮/多轮、并发、隔离、stateful 等语义

RenderedTaskInput
- 已经带有 benchmark 语义的统一输入对象，等待 agent skill 传输落地

JudgeInput
- 给 scorer/judge 的统一输入对象，包含 task、agent output、gold 和必要元数据

Artifact
- 一个被持久化的原始输出文件或 trace

MetricRecord
- 一个挂在 run/task/turn 范围上的数值或类别指标

JudgeResult
- 一个针对 Task 或 run 的评分/通过失败解释
```

- [ ] **Step 2: 写清 protocol 存在的原因**

```text
Protocol 目的:
- 防止 benchmark 特有逻辑泄漏进 orchestrator
- 防止 agent 特有 runtime 细节泄漏进 benchmark 处理过程
- 让执行约束、输入模板和评分输入成为显式契约
- 让归档和报告所依赖的输出保持稳定
```

### Task 5: 定义 skill 模型

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 定义 benchmark skill 契约**

```text
Benchmark skill 必须提供:
- 身份与版本
- 数据集准备
- 任务展开
- 执行约束
- RenderedTaskInput 语义内容
- 评分语义
- JudgeInput 需要的字段约定
- scorer/judge 入口
- benchmark 特有 artifact 声明
```

- [ ] **Step 2: 定义 agent skill 契约**

```text
Agent skill 必须提供:
- 身份与版本
- startup/healthcheck/teardown
- task 执行方式
- session 处理模型
- RenderedTaskInput 到 transport 的映射
- 输出采集适配
- runtime artifact 声明
```

- [ ] **Step 3: 定义 skill 目录布局**

```text
skills/
  benchmarks/
    locomo/
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
```

### Task 6: 定义 MVP 架构切片

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 固定 MVP 矩阵**

```text
Benchmarks:
- LoCoMo
- LongMemEval

Agents:
- OpenClaw
- Generic CLI Agent
```

- [ ] **Step 2: 定义 MVP 归档布局**

```text
runs/<run_id>/
  run.json
  records/
  artifacts/
  logs/
  reports/
  config_snapshot/
```

- [ ] **Step 3: 定义 MVP 对 ClusterBench 的复用边界**

```text
允许复用:
- 主机级资源监控逻辑
- 归档目录组织模式

禁止复用:
- workload driver 抽象
- L1/L2 mode 模型
- test_result report schema
```

### Task 7: 定义风险与非目标

**Files:**
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 记录主要风险**

```text
风险:
- benchmark skills 过薄，导致语义泄漏进 orchestrator
- agent skills 过薄，导致 runtime 逻辑泄漏进 orchestrator
- run protocol 过于抽象，丢失必要执行细节
- ClusterBench 的复用越过监控/归档边界，污染核心模型
- judge/scorer 调用边界不清，导致 benchmark 评分规则重新散落到 core
```

- [ ] **Step 2: 记录显式非目标**

```text
非目标:
- 在 MVP 内构建通用集群调度器
- 在 MVP 内统一所有 benchmark 的评分方法
- 强制所有 agent 在接入前都暴露原生 SDK/API
```

### Task 8: 将架构转成实现检查点

**Files:**
- Modify: `docs/superpowers/plans/2026-06-08-memory-benchmark-platform-mvp.md`
- Create: `docs/superpowers/plans/2026-06-09-memory-benchmark-platform-architecture-plan.md`

- [ ] **Step 1: 定义实现顺序**

```text
实现顺序:
1. protocol + storage
2. manifest + loader
3. benchmark skills
4. agent skills
5. orchestrator/executor
6. collector/judge/monitor
7. archive/report
```

- [ ] **Step 2: 将本架构方案绑定到现有 MVP 实现 plan**

```text
映射关系:
- 本文档定义架构边界
- `2026-06-08` 的 MVP plan 定义第一批实现任务
- 后续任务 plan 不能违反本文声明的组件边界
```

### 架构 Review 检查表

- [ ] 检查每个组件是否只有一个清晰职责。
- [ ] 检查每个跨组件交互是否都经过显式契约。
- [ ] 检查 benchmark 语义是否由 benchmark skills 持有，而不是 orchestrator。
- [ ] 检查 runtime 机制是否由 agent skills 持有，而不是 orchestrator。
- [ ] 检查 `ExecutionSpec`、`RenderedTaskInput`、`JudgeInput` 是否为显式对象，而不是隐式临时字段。
- [ ] 检查 ClusterBench 的复用是否仅限监控/归档相关能力。

### Review 结论

当前 review 状态:
- 本轮未发现阻塞性架构冲突。
- benchmark 输入模板语义与 agent 侧输入传输方式的边界，已通过 `RenderedTaskInput` 明确为中间契约。
- judge orchestration 已收敛为 orchestrator 负责组装 `JudgeInput` 并调用 benchmark skill scorer。
- 剩余风险之一是混合评分 benchmark 下 scorer 路由字段的设计。
