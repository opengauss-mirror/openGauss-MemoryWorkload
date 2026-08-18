# 记忆测试平台 workflow/case 架构计划

> 执行路线固定为：`Python 过渡实现 + 面向未来 Go 兼容的模型设计`

## 1. 目标

把当前“benchmark 驱动的最小执行底座”升级为“以 workflow/case 为中心的完整 benchmark/test 平台”。

本轮目标不是迁移语言，而是先把以下闭环做成平台主路径：

```text
CaseSource -> Case/Step DAG -> Operator Execution -> Gate/Retry -> Trace/Evidence -> Judge -> Report/Archive
```

## 2. 关键决策

### 决策 1：保持 Python 实现，按未来 Go 兼容设计

推荐结论：

- 当前执行内核、技能目录、测试和外部验证都已经在 Python 上
- 本轮核心风险在执行模型重构，不在语言
- 因此先用 Python 跑通 workflow/case 闭环
- 所有核心对象和接口命名按未来 Go 迁移友好方式收敛

不做：

- 本轮不直接切换 Go 作为主执行器
- 本轮不因为 Go 方向而推迟平台闭环

### 决策 2：benchmark 降级为 CaseSource，而不是继续作为主执行中心

推荐结论：

- `LoCoMo / LongMemEval` 负责把样本转换为标准 `CaseRecord + StepRecord`
- `ovtest` 类场景属于原生 `WorkflowCase`
- 平台主内核只理解 `Case / Step / Trace / Judge`

这意味着：

- benchmark 不再直接驱动 agent
- agent 不再是一等业务对象，而是一种 operator adapter
- 最终判定不再是“题目评分器”，而是“case 末端判定器”

### 决策 3：deterministic gate 与 final judge 必须分层

推荐结论：

- step 内 gate 负责程序化检查、early exit、retry
- case 末端 judge 负责基于自然语言 reference 进行 pass/fail + rationale

禁止：

- 在每个 step 上直接挂 LLM judge 作为常规执行路径
- 把 deterministic check 混进 final judge 里

### 决策 4：第一阶段先做 builtin judge 闭环，LLM judge 作为扩展点

推荐结论：

- 第一阶段必须先保证平台闭环可运行、可测试、可集成
- 因此先实现 builtin judge 接口和最小规则集
- 再为外部/LLM judge 预留 adapter

原因：

- 先把 `JudgeInput -> JudgeResult` 路打通
- 降低外部模型依赖带来的不稳定性
- 为后续 OpenViking / ovtest 场景扩展保留接口

## 3. 重构后的平台分层

### 3.1 CaseSource Layer

职责：

- 输入来源转换为标准 `CaseRecord + StepRecord + ExecutionSpec`

来源类型：

- `BenchmarkCaseSource`
  - `LoCoMo`
  - `LongMemEval`
- `NativeWorkflowCaseSource`
  - `ovtest`

### 3.2 Workflow Core

职责：

- 管理 run / case 生命周期
- 按 `depends_on` 调度 step
- 处理 gate / retry / early-exit
- 生成 trace 与 step result

限制：

- 不关心 step 底层是 agent / bash / http
- 不直接理解 benchmark 特有语义

### 3.3 Operator Layer

职责：

- 黑盒执行 step

最小 operator 集合：

- `bash operator`
- `http operator`
- `agent operator`
- `wait operator`

### 3.4 Judge Layer

职责：

- 在 case 末端对整条 trace 做最终判定

输入：

- `reference`
- `step_results`
- `trace_events`
- `artifacts`
- `resource_summary`

输出：

- `JudgeResult`

### 3.5 Monitor Layer

职责：

- run 级资源监控
- 至少采集 CPU 信息
- 如可行补充 memory / process snapshot

输出：

- monitor artifacts
- `MetricRecord`

### 3.6 Archive / Report Layer

职责：

- 写结构化记录
- 写原始 artifacts
- 输出 run summary 和 case results

## 4. 主数据模型

平台主模型改为：

- `RunRecord`
- `CaseRecord`
- `StepRecord`
- `StepResultRecord`
- `TraceEventRecord`
- `ExecutionSpec`
- `JudgeInput`
- `JudgeResult`
- `ArtifactRecord`
- `MetricRecord`
- `ReportSummary`

兼容策略：

- 旧 `TaskRecord / TurnRecord` 降级为兼容层
- 新执行主链不再以 `Task / Turn` 为中心

## 5. 执行语义

### 5.1 主路径

```text
1. CLI 选择 CaseSource 和 operator targets
2. CaseSource 产出 Case / Step / ExecutionSpec
3. Workflow Core 调度 step
4. Operator 执行 step
5. Gate 做 deterministic check
6. 若失败且允许 retry，则重试
7. 所有事件写入 TraceEvent
8. case 结束后组装 JudgeInput
9. Judge 产出 JudgeResult
10. Archive/Report 写出结构化 records 与 summary
```

### 5.2 Gate 规则

- `hard`
  - 失败即终止 case 或终止后续依赖分支
- `soft`
  - 失败仅记录，不中断执行
- `none`
  - 不执行 gate

### 5.3 Retry 规则

- retry 属于 step 级能力
- retry 次数显式记录到 trace 和 metrics
- retry 只在 operator 失败或 gate 失败且策略允许时触发

## 6. LoCoMo / LongMemEval / ovtest 映射

### 6.1 LoCoMo

- 每个 QA 转成一个 `Case`
- 默认包含一个主 step：`agent_query`
- `reference` 必须包含：
  - question
  - gold answer
  - category
  - sample_id / split / metadata

### 6.2 LongMemEval

- 作为第二个 `BenchmarkCaseSource`
- 输出结构与 LoCoMo 对齐到统一 Case 模型

### 6.3 ovtest

- 原生输出多 step DAG
- `reference` 为自然语言 expected trace
- 最终 judge 按整条 trace 判定，而不是单答案比对

## 7. 资源监控接入

第一阶段最小要求：

- run 开始时启动 monitor
- 周期采集 CPU 信息
- run 结束时停止 monitor
- 写出：
  - `artifacts/monitor/cpu_status.csv`
  - `records/metrics.json` 中的 CPU 摘要
  - `reports/summary.json` 中的资源摘要

## 8. 新归档结构

```text
runs/<run_id>/
  run.json
  records/
    cases.json
    steps.json
    step_results.json
    traces.json
    judge_results.json
    metrics.json
  artifacts/
    step-stdout/
    step-stderr/
    judge/
    monitor/
  logs/
  reports/
    summary.json
    case_results.json
  config_snapshot/
```

## 9. 第一阶段实现顺序

1. 重写架构文档到 workflow/case 模型
2. 升级协议模型
3. 重构 LoCoMo skill 为 `CaseSource`
4. 新增 workflow executor
5. 接 builtin judge 闭环
6. 接 CPU monitor 到主链
7. 重构 archive / report
8. 本地测试
9. 外部最小集成验证

## 10. 第一阶段代码范围

必须改：

- `memory_bench_platform/memory_bench_platform/protocol.py`
- `memory_bench_platform/memory_bench_platform/cli.py`
- `memory_bench_platform/memory_bench_platform/storage.py`
- `memory_bench_platform/memory_bench_platform/reporter.py`
- `memory_bench_platform/memory_bench_platform/integration.py`
- `memory_bench_platform/memory_bench_platform/executor.py`
- `memory_bench_platform/skills/benchmarks/locomo/scripts/build_tasks.py`

建议新增：

- `memory_bench_platform/memory_bench_platform/workflow.py`
- `memory_bench_platform/memory_bench_platform/judges.py`

## 11. P1/P2 Review

### P1

如果第一阶段同时追求：

- 完整并行 DAG
- 外部 LLM judge
- ovtest 原生 case
- LoCoMo/LongMemEval 全量闭环

风险过高。

修正：

- 第一阶段只要求跑通 `LoCoMo -> CaseSource -> Workflow -> Builtin Judge -> Report`
- operator、trace、gate、retry、CPU monitor 必须进入主链

### P2

如果新平台继续把 `tasks.json` 当主结构，只是额外增加 case 文件，会长期形成双主模型。

修正：

- 第一阶段即把 archive 主结构切换到 `cases / steps / step_results / traces / judge_results / metrics`
- 旧 task 输出只保留兼容或直接废弃

## 12. 完成标准

本轮完成不是“文档改完”，而是以下四类证据共同成立：

1. 文档：
   - plan
   - 架构说明
   - 决策记录
2. 代码：
   - workflow/case 主链实现
   - builtin judge
   - CPU monitor 主链接入
3. 测试：
   - 本地测试覆盖 case/workflow/gate/retry/judge/archive
4. 外部验证：
   - LoCoMo -> CaseSource -> 执行 -> judge -> report
   - OpenClaw -> agent operator
   - OpenViking -> workflow/native case 或 memory backend 验证
