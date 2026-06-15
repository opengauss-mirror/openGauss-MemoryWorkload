# 记忆测试平台 workflow/case 架构设计说明

## 1. 背景与目标

当前平台最初是一个 benchmark 驱动的最小执行底座：benchmark 展开为 task，agent 负责执行，最后归档结果。

这个模型已经不足以承载以下需求：

- 多 step / DAG 测试流程
- 中间 trace/evidence 逐级传递
- deterministic gate / retry / early-exit
- 基于自然语言 reference 的 case 末端 judge
- CPU/运行资源采集进入主执行链

因此，平台主模型需要升级为：

```text
CaseSource -> Case/Step DAG -> Operator Execution -> Gate/Retry -> Trace/Evidence -> Judge -> Report/Archive
```

本轮固定的技术路线是：

- `Python 过渡实现`
- `面向未来 Go 兼容的模型设计`

这意味着：

- 当前实现继续用 Python 落地
- 核心对象、命名和边界按未来可迁移 Go 的方式设计
- 本轮不把“架构升级”和“语言迁移”绑在一起

## 2. 顶层决策

### 2.1 benchmark 不再是执行中心，而是 CaseSource

- `LoCoMo / LongMemEval` 视为 `BenchmarkCaseSource`
- `ovtest` 这类场景视为 `NativeWorkflowCaseSource`
- 平台主内核不再围绕 `Task / Turn` 展开，而是围绕 `Case / Step / Trace / Judge`

### 2.2 agent 不再是一等业务模型对象

平台不感知“被测对象是不是 Agent”，执行单元统一抽象为黑盒 operator：

- `bash operator`
- `http operator`
- `agent operator`
- `wait operator`

因此：

- `OpenClaw` 只是 operator adapter 的一种实现
- 后续普通 CLI、服务接口、脚本执行都可走同一执行内核

### 2.3 deterministic gate 与 final judge 必须分层

- step 内 `gate` 负责程序化检查、early-exit、retry
- case 末端 `judge` 负责基于自然语言 `reference` 做最终 pass/fail + rationale

不允许：

- 在每个 step 上把 LLM judge 作为常规主路径
- 把 deterministic check 混进 final judge 中

### 2.4 第一阶段先做 builtin judge 闭环

本轮必须优先保证：

- `Case -> Step -> Gate/Retry -> Trace -> Judge -> Report` 闭环可运行

因此：

- 第一阶段先实现 builtin judge
- 同时为外部/LLM judge 预留接口
- 后续再扩展到真实 LLM judge

## 3. 总体架构

```text
                         +-------------------------------------+
                         |            Case Sources             |
                         |-------------------------------------|
                         | BenchmarkCaseSource                 |
                         | - LoCoMo                           |
                         | - LongMemEval                      |
                         | NativeWorkflowCaseSource            |
                         | - ovtest                           |
                         +----------------+--------------------+
                                          |
                                          v
+-------------------+    +-------------------------------------+    +----------------------+
|    Config / CLI   |--->|            Workflow Core            |--->|     Operator Layer   |
|-------------------|    |-------------------------------------|    |----------------------|
| run spec          |    | - load case sources                 |    | bash / http / agent  |
| source selection  |    | - build cases and steps             |    | wait                 |
| runtime options   |    | - execute step DAG                  |    |                      |
+-------------------+    | - apply gate / retry / early-exit   |    +----------+-----------+
                         | - emit traces and step results       |               |
                         +----------------+--------------------+               |
                                          |                                    |
                                          v                                    v
                         +-------------------------------------+    +----------------------+
                         |            Judge Layer              |    |    Monitor Layer     |
                         |-------------------------------------|    |----------------------|
                         | builtin judge                       |    | CPU / memory / proc  |
                         | external / LLM judge adapter        |    | monitor artifacts    |
                         +----------------+--------------------+    +----------+-----------+
                                          |                                    |
                                          +----------------+-------------------+
                                                           |
                                                           v
                                 +--------------------------------------------------+
                                 |              Archive / Report Layer              |
                                 |--------------------------------------------------|
                                 | run.json                                         |
                                 | records/cases.json                               |
                                 | records/steps.json                               |
                                 | records/step_results.json                        |
                                 | records/traces.json                              |
                                 | records/judge_results.json                       |
                                 | records/metrics.json                             |
                                 | reports/summary.json                             |
                                 | reports/case_results.json                        |
                                 +--------------------------------------------------+
```

## 4. 组件职责

| 组件 | 职责 | 不负责 |
|---|---|---|
| `Config / CLI` | 接收 run 配置，选择 case source、operator target、runtime 参数 | 不实现业务执行逻辑 |
| `CaseSource` | 把 benchmark/native workflow 转成标准 `CaseRecord + StepRecord + ExecutionSpec` | 不直接执行 step |
| `Workflow Core` | 管理 run/case 生命周期，调度 step，处理 gate/retry/early-exit，生成 trace 和 step result | 不理解 step 底层具体业务，不写 benchmark/agent 特判 |
| `Operator Layer` | 执行黑盒 step：bash/http/agent/wait | 不决定 case 语义，不做最终判定 |
| `Judge Layer` | 基于 `reference + trace + step results + artifacts` 做 case 末端判定 | 不做 step 级 deterministic check |
| `Monitor Layer` | 在 run 期间采集 CPU/资源信息并输出 metrics/artifacts | 不参与业务判断 |
| `Archive / Report Layer` | 统一落盘结构化 records、artifacts、reports | 不决定业务语义 |

## 5. 关键边界

### 5.0 Skill manifest 的版本策略

所有 benchmark skill 与 agent skill 都必须在 `manifest.yaml` 中声明结构化 `version_policy`，至少包含：

- `default_selection = latest_official_release_tag`
- `resolution_order`
- `allowed_overrides`
- `disallowed_defaults`
- `targets`
- `targets[].upstream`
- `record_runtime_version`

这条规则的目的不是立即替平台自动解析最新 tag，而是先把“默认版本来源”沉淀为可校验、可审计的机器可读协议，避免版本约束只散落在 `README` 或 `SKILL.md` 中。

默认策略：

- 优先用户指定的正式版本
- 否则使用上游当前最新正式 release tag
- 仅在已有验证结论时回退到旧正式 tag

`targets` 用来明确“这条版本策略到底约束哪些软件组件”，避免只知道“要用最新 tag”，却不知道是：

- benchmark 自身工具链
- 被测 agent
- memory backend
- 运行时依赖

`targets[].upstream` 用来明确“最新正式 tag 到底去哪里取”，避免 skill 只表达了策略，却没有表达解析来源。对接平台的 skill 应默认把上游 release/tag 源写清楚；如果没有标准 upstream，则必须在 run 记录里显式保存实际二进制、commit 或制品来源。

例如：

- `locomo` benchmark skill 应至少标记 `locomo-benchmark`
- `openclaw` agent skill 应至少标记 `openclaw` 与其依赖的 `openviking`
- `ovtest-*` benchmark skill 应至少标记其直接测试对象 `openviking`

不允许平台默认选择：

- `dirty worktree`
- `dev build`
- `non-tag commit`

### 5.1 CaseSource 与 Workflow Core 的边界

CaseSource 只负责“定义测试”：

- case 的 goal / capability / reference
- step DAG
- execution spec

Workflow Core 只负责“执行测试”：

- 解析 step 依赖
- 执行 operator
- 运行 gate / retry
- 记录 trace

### 5.2 Gate 与 Judge 的边界

`Gate`

- step 后立即执行
- 程序化检查
- 可决定 retry / early-exit

`Judge`

- case 末端统一执行
- 读取 reference、trace、step results、artifacts
- 输出最终 verdict

### 5.3 Operator 与被测对象的边界

Operator 统一视图：

- `bash`：命令执行
- `http`：接口调用
- `agent`：如 OpenClaw / Generic CLI
- `wait`：sleep / settle / polling

框架本身不区分“这是 agent 测试还是 bash 测试”。

### 5.4 Monitor 与业务执行的边界

Monitor 为 run 级公共能力：

- 不属于 benchmark skill
- 不属于 agent skill
- 仅负责资源事实采集与归档

## 6. 主数据模型

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

### 6.1 RunRecord

描述一次完整执行：

- `run_id`
- `source_id`
- `source_kind`
- `config`
- `environment`
- `started_at / ended_at / status`

### 6.2 CaseRecord

描述一个完整测试 case：

- `case_id`
- `run_id`
- `title`
- `goal`
- `capability`
- `reference`
- `labels`
- `source_metadata`
- `judge_mode`

### 6.3 StepRecord

描述 case 中一个执行节点：

- `step_id`
- `case_id`
- `name`
- `operator_kind`
- `depends_on`
- `retry_limit`
- `timeout_seconds`
- `gate_policy`
- `inputs`

### 6.4 StepResultRecord

描述 step 的一次执行尝试：

- `step_result_id`
- `step_id`
- `attempt`
- `status`
- `exit_code`
- `started_at / ended_at / duration_ms`
- `stdout_ref`
- `stderr_ref`
- `structured_output`
- `gate_passed`
- `gate_detail`

### 6.5 TraceEventRecord

记录过程事件：

- `step_started`
- `step_finished`
- `gate_passed`
- `gate_failed`
- `retry_scheduled`
- `case_judge_started`
- `case_judge_finished`

### 6.6 JudgeInput

不再是问答三元组，而是 case 末端判定输入：

- `case_id`
- `goal`
- `reference`
- `step_results`
- `trace_events`
- `artifacts`
- `facts`
- `resource_summary`

### 6.7 JudgeResult

描述最终判定：

- `judge_id`
- `case_id`
- `passed`
- `label`
- `score`
- `rationale`
- `evidence_refs`

### 6.8 MetricRecord

至少支持：

- `scope = run | case | step`
- `cpu_*`
- `duration_ms`
- `retry_count`
- `token_*`

### 6.9 ReportSummary

描述 run 汇总：

- `run_id`
- `status`
- `case_total`
- `case_passed`
- `case_failed`
- `resource_summary`
- `category_summary`

### 6.10 兼容层

旧模型：

- `TaskRecord`
- `TurnRecord`

保留为兼容层，但不再作为主执行内核中心模型。

## 7. 执行流程

```text
1. CLI 选择 CaseSource 和 runtime 配置
2. CaseSource 生成 Case / Step / ExecutionSpec
3. Workflow Core 按 DAG 调度 step
4. Operator 执行 step
5. Gate 执行 deterministic check
6. 若失败且允许 retry，则重试
7. 所有事件与结果写入 trace / step results
8. Monitor 同步采集 CPU/资源信息
9. case 完成后组装 JudgeInput
10. Judge 产出 JudgeResult
11. Archive / Report 写出结构化记录与汇总报告
```

## 8. LoCoMo / LongMemEval / ovtest 映射

### 8.1 LoCoMo

LoCoMo 不再输出 `Task(question only)`，而是输出标准 `Case`：

- 每个 QA 为一个 `Case`
- 默认包含一个主 step：`agent_query`
- `reference` 中包含：
  - question
  - gold answer
  - category
  - sample_id
  - split / metadata

### 8.2 LongMemEval

LongMemEval 作为第二个 `BenchmarkCaseSource`，也对齐到同一 Case 模型。

### 8.3 ovtest

ovtest 场景属于原生 `WorkflowCase`：

- 直接定义多 step DAG
- `reference` 是 expected trace 的自然语言描述
- 最终 judge 针对整条 trace 做 verdict

## 9. 资源监控接入

本轮最小要求：

- run 开始时启动 monitor
- 周期采集 CPU 信息
- run 结束时停止 monitor

必须输出：

- `artifacts/monitor/cpu_status.csv`
- `records/metrics.json` 中的 CPU 统计
- `reports/summary.json` 中的 CPU 摘要

如实现成本可控，可补充：

- memory snapshot
- process snapshot

## 10. 新归档结构

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

## 11. 当前实现与目标架构的差距

当前代码已经具备：

- skill 加载
- benchmark validator / task builder
- agent healthcheck / runner
- 最小 run archive
- 资源监控基础类

当前代码尚未具备：

- `Case / Step / Trace / Judge` 主模型
- 多 step / DAG / retry / gate 执行内核
- case 末端 builtin judge 闭环
- CPU monitor 主链接入
- 新归档结构

因此当前状态不能误判为“完整 benchmark 平台已完成”，只能认定为“最小底座已存在”。

## 12. P1/P2 Review

### P1：旧文档仍以 benchmark/task 为中心

影响：

- 会误导后续实现继续围绕 `Task / Turn` 打补丁
- 无法自然容纳 ovtest 场景

修正：

- 本文已将主路径改写为 `CaseSource -> Case/Step DAG -> Judge`

### P2：judge 边界过窄

影响：

- 只能支持 `question/expected/answer`
- 无法统一 trace 判定和 workflow case

修正：

- `JudgeInput` 已升级为 case trace 级输入
- `Judge` 明确为 case 末端最终判定器

## 13. 实现顺序

建议顺序：

1. 升级协议模型
2. 升级 LoCoMo skill 到 `CaseSource`
3. 新增 workflow executor
4. 接 builtin judge
5. 接 CPU monitor
6. 重构 archive/report
7. 本地测试
8. 外部最小集成验证
