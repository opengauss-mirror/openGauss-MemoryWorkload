# Integration Skill 标准生成与接入设计

## 1. 背景

本项目的长期目标是：当新的 Benchmark、Agent 或 Memory 系统出现时，平台维护者可以在一天内完成可用级接入，并且原则上不修改平台核心代码。

当前仓库已经具备 Benchmark Builder/Scorer、Agent Runner、Memory Runner、原生 Workflow 和 external runner 等基础能力，但接入过程仍主要依赖自然语言说明与 Agent 自由生成代码。Agent 需要阅读较多平台源码，并自行理解目录、Manifest、Case/Step、结果导入和评分约定。即使流水线能够运行，也缺少统一机制证明数据映射、评分语义和能力组合是正确的。

本设计增加一个独立的 Integration SDK 和 Onboarding Kit。Agent 继续负责理解外部系统和编写适配逻辑；平台负责生成确定性骨架、约束协议并验证接入结果。

## 2. 术语

本文中的 **Integration Skill** 专指本项目的三类扩展：

- Benchmark Integration Skill：生成 Case/Step，并评价预测结果。
- Agent Integration Skill：执行 Benchmark 生成的任务并返回标准回答。
- Memory Integration Skill：执行 `ingest`、`status`、`recall` 等记忆操作。

它与 Codex 或其他 Agent 产品中的 Prompt Skill 无关。现有目录继续使用：

```text
skills/benchmarks/
skills/agents/
skills/memories/
```

## 3. 目标与非目标

### 3.1 目标

1. 平台维护者可以编写少量 Python Adapter，在一天内达到可用级接入。
2. 新增 Integration Skill 只修改自身目录，不修改平台核心代码。
3. 接入结果至少通过配置校验、类型契约、Golden Test、能力匹配和端到端 Smoke Test。
4. 接入失败能够定位到阶段、文件、字段或外部连接，并提供可执行的修改建议。
5. Benchmark 允许使用不同评判标准，但评分输入、Metric 表达和可追溯信息保持统一。

### 3.2 非目标

1. v1 不要求第三方开发者只写 Manifest 即可自助接入。
2. v1 不建设远程 Skill Registry 或独立包管理系统。
3. v1 不负责启动、重置或销毁 Agent/Memory 服务，只连接已经运行的 CLI 或 API。
4. v1 不用同一套业务指标评价所有 Benchmark。
5. v1 不取消 Agent 生成代码，而是限制 Agent 可以生成和修改的边界。

## 4. 核心原则

接入系统采用以下职责划分：

```text
平台模板决定 Integration Skill 必须长什么样
Agent 决定新系统如何映射到平台协议
Conformance Harness 判断映射是否合格
Benchmark 决定被测系统的回答如何评分
```

统一的是插件形式、执行协议、Metric Envelope、验证证据和诊断输出；不统一不同 Benchmark 的领域语义、评分规则和质量阈值。

### 4.1 平台侧的两个组成部分

本文所说的“平台负责”实际分为两层：

1. **Integration SDK**：Python 公共库，定义请求/响应模型、协议版本、能力字段、Evaluation Profile 接口、标准异常和 Runner 辅助函数。
2. **Onboarding Kit**：基于 SDK 实现的 CLI 和测试工具，包括 Scaffolder、Static Validator、Conformance Harness、Compatibility Resolver、Smoke Runner 和 Diagnostic Reporter。

Agent 可以调用 Onboarding Kit，也可以在 Adapter 中导入 Integration SDK 提供的公开接口，但不能修改这两层来适配某个具体 Skill。

### 4.2 SDK、Agent 与验证器责任矩阵

| 接入内容 | Integration SDK / Onboarding Kit 负责 | Agent 负责 | 最终判断方 |
|---|---|---|---|
| Integration Skill 目录 | 提供版本化模板并创建全部固定文件 | 不自行发明目录或入口文件 | Static Validator |
| `manifest.yaml` | 定义 schema、默认值、协议版本和合法能力字段 | 根据新系统填写 ID、entrypoint、requirements、capabilities、Profile 和环境变量名称 | Static Validator / Compatibility Resolver |
| `SKILL.md` | 生成固定章节和协议说明 | 填写外部系统、配置、限制和运行示例 | 文档校验与维护者评审 |
| `build_tasks.py` | 生成函数签名、类型、Runner 包装和标准错误处理 | 实现原始样本到 Case/Step 的业务映射 | Type Contract / Golden Test |
| `score_predictions.py` | 提供 Metric Envelope 和预置 Profile scorer | 映射 prediction/reference；为 `custom@1` 实现自定义评分 | Profile Contract / Golden Test |
| `validate.py` | 提供校验入口、结果模型和公共校验工具 | 实现 Benchmark 特有的数据、资源和样本关系检查 | Conformance Harness |
| `run_task.py` | 提供 Agent 请求/响应模型、Runner 包装和错误 Envelope | 实现平台任务到外部 Agent CLI/API 的映射 | Agent Type Contract / Golden Test |
| `run_operation.py` | 提供 Memory action、请求/响应模型和 Runner 包装 | 实现外部 Memory 的 ingest/status/recall 调用与字段映射 | Memory Type Contract / Capability Contract |
| `healthcheck.py` | 提供无副作用的健康检查协议 | 实现目标 CLI/API 的实际连通性检查 | Smoke Runner |
| Evaluation Profile | 定义标准 Profile、Metric 语义、方向、值域和聚合方式 | 为 Benchmark 选择 Profile 并填写必要映射或 rubric | Profile Contract |
| requirements/capabilities | 定义字段词汇、schema 和匹配算法 | 根据外部系统源码声明需要或提供的能力 | Capability Contract / Compatibility Resolver |
| Golden Fixture | 创建固定目录、schema 和示例占位 | 构造小型输入并起草预期 Case/Metric | Golden Test；预期语义由官方实现或维护者确认 |
| 错误与诊断 | 定义错误码、脱敏规则和结构化报告 | 根据诊断修改当前 Skill | Diagnostic Reporter |
| Smoke 组合 | 提供参考 Skill、选择规则和执行器 | 发起命令；必要时从兼容候选中选择目标组合 | Compatibility Resolver / Smoke Runner |
| `integration_receipt.json` | 自动汇总并生成，不接受 Skill 自报测试结果 | 不直接编辑 | Onboarding Kit / CI |

“Agent 负责”表示 Agent 填写平台模板中的外部系统适配逻辑，不表示 Agent 可以自由改变公共协议。协议模型、模板框架、Profile 公式、匹配算法、Conformance 规则、错误码和接入凭证都只能由平台提供。

Golden Fixture 是特殊的协作项：Agent 可以自动生成小型样本和预期结果草稿，但不能用待测试实现的输出自我证明正确。`expected_tasks.json` 和 `expected_metrics.json` 必须能由 Benchmark 官方实现、官方样例或维护者手工推导确认。

## 5. 总体架构

平台新增接入平面，现有 Workflow、Builder、Operator Dispatcher 和执行器继续作为运行平面。下图中的平台步骤都是 Onboarding Kit 执行，只有“Agent 填写”步骤由 Agent 生成外部系统适配代码：

```text
新系统源码或文档
  → Integration Scaffolder
  → Agent 填写 Adapter 与 Golden Case
  → Static Validator
  → Conformance Harness
  → Compatibility Resolver
  → Smoke Runner
  → Integration Receipt
  → 现有 memory-bench 运行平面
```

接入平面包含五个组件：

1. **Integration SDK**：提供版本化请求/响应模型、公开辅助函数和标准异常。
2. **Integration Scaffolder**：按类型生成确定性目录、入口代码和 Golden Fixture。
3. **Conformance Harness**：执行公共契约、类型契约、能力契约和 Golden Test。
4. **Compatibility Resolver**：比较 Benchmark requirements 与 Agent/Memory capabilities。
5. **Diagnostic Reporter**：生成统一错误码、定位信息和接入凭证。

运行平面只依赖公开协议和能力声明，不允许按照具体 Skill ID 分支。

## 6. 确定性骨架与 Agent 分工

接入流程先做最小预判，只确定 Integration Skill 类型、ID，以及 Benchmark 的初步 Evaluation Profile。随后由平台生成骨架，再由 Agent 深入分析外部系统并填写代码。

```text
用户指定接入目标
  → 最小预判
  → 平台生成骨架
  → Agent 分析新系统
  → Agent 填充业务适配
  → 平台验证
  → Agent 根据诊断修复
```

平台生成固定函数签名、SDK import、stdin/stdout 协议、异常转换和未实现占位。Agent 只实现外部数据或 API 到平台模型的映射，不重新设计入口协议，也不重复实现子进程通信和结果 Envelope。生成骨架、执行校验、计算能力匹配和生成接入凭证始终是平台行为，即使这些命令由 Agent 自动发起。

## 7. Integration Skill 骨架

### 7.1 Benchmark

```text
skills/benchmarks/<benchmark-id>/
├── SKILL.md
├── manifest.yaml
├── scripts/
│   ├── build_tasks.py
│   ├── score_predictions.py
│   └── validate.py
└── tests/
    └── golden/
        ├── source_sample.json
        ├── expected_tasks.json
        ├── predictions.json
        └── expected_metrics.json
```

- `build_tasks.py`：把原始样本转换为平台 Case/Step。
- `score_predictions.py`：调用预置 Evaluation Profile，或实现 custom scorer，并返回标准 Metric Envelope。
- `validate.py`：检查该 Benchmark 特有的数据字段、资源文件和样本关系。平台的通用 Manifest/schema 校验不放在这里重复实现。
- Golden Case：证明数据转换和评分字段映射忠实保留了原 Benchmark 语义。

### 7.2 Agent

```text
skills/agents/<agent-id>/
├── SKILL.md
├── manifest.yaml
├── scripts/
│   ├── run_task.py
│   └── healthcheck.py
└── tests/
    └── golden/
        ├── task_input.json
        └── expected_output.json
```

Agent 使用一个稳定执行契约。Golden Case 只验证请求和回答的结构映射，不评价回答质量。`healthcheck.py` 检查已运行服务是否可访问，不负责启动服务。

### 7.3 Memory

```text
skills/memories/<memory-id>/
├── SKILL.md
├── manifest.yaml
├── scripts/
│   ├── run_operation.py
│   └── healthcheck.py
└── tests/
    └── golden/
        ├── requests.json
        └── expected_outputs.json
```

Memory 统一通过 `run_operation.py` 实现声明支持的 action。Golden Case 验证 operation ID、状态、identity、memories 和 evidence 的结构映射；Recall 内容质量由 Benchmark 评价。

平台统一规定 `ScenarioSample = Memory Episode`。Runtime 为每个 Episode 生成 `run_id + sample_id/namespace_hint` 的 `scope_id`：同一 Episode 内的 Session、Checkpoint 和时间推理共享记忆，不同 Episode 与不同 Run 默认隔离。`backend_direct` 由 Memory Adapter 将 Scope 映射到后端身份或 Namespace；`agent_plugin` 由插件的 `prepare` 映射到 Agent 上下文 Namespace。两种模式必须声明并通过 Scope 能力校验。

## 8. Manifest 与能力匹配

所有 Integration Skill 声明协议和 SDK 版本：

```yaml
integration:
  type: benchmark
  protocol_version: benchmark/1
  sdk_version: ">=1.0,<2.0"
```

Benchmark 的运行要求由 Agent 根据 Benchmark 源码填写，但字段定义和合法值由 SDK 提供：

```yaml
requirements:
  agent:
    structured_input: true
  memory:
    actions: [ingest, status, recall]
    async_ingest: true
```

Memory 的实际能力由 Agent 根据 Memory 系统接口填写，但能力是否真实可用必须由 Capability Contract 验证：

```yaml
capabilities:
  actions: [ingest, status, recall]
  async_ingest: true
```

Agent 使用同一机制声明 `structured_input`、`multi_turn`、`tool_calling` 等执行能力，但不声明 Memory action。Compatibility Resolver 由平台执行，Agent 不能通过自行输出“匹配成功”跳过验证。

执行前必须满足：

```text
Benchmark requirements ⊆ Agent/Memory capabilities
```

能力不兼容时应在 Workflow 启动前失败，不能等到运行中缺少 action 才暴露问题。Agent 和 Memory 不使用 Evaluation Profile；能力字段只用于兼容性判断，不用于评分。

Benchmark 的评测契约拆成 Observation Extractor 与 Scorer/Judge：Extractor 根据 Evaluation Target 提取 `qa_answer`、`evidence_text` 等标准观察值，Profile 决定评分 Prompt、rubric 和 Metric。Profile 与 Prompt 属于 Benchmark Skill；未知 Profile、缺失 Prompt 或未知 Extractor 必须失败，禁止静默退回其他 Benchmark 的规则。

### 8.1 公共执行 Envelope

Integration Skill 入口统一使用 JSON stdin/stdout。Agent 和 Memory 的请求至少包含 `task_id`、`inputs`、非敏感 `runtime_context` 和 `idempotency_key`；Memory 请求额外包含 `action`。响应统一包含：

```json
{
  "status": "completed",
  "output": {},
  "metrics": {},
  "artifacts": [],
  "error": null
}
```

Benchmark Builder 接收数据源和构建选项，返回 Case/Step 集合；Benchmark Scorer 接收 predictions、references 和 evaluation 配置，返回 Metric Envelope。具体 Python 模型由 Integration SDK 版本化，Skill 不直接依赖平台内部 Record 实现。

## 9. Benchmark Evaluation Profile

只有 Benchmark 拥有 Evaluation Profile，因为 Benchmark 决定测什么以及怎样评价结果。v1 提供：

| Profile | 标准 Metric |
|---|---|
| `exact_match@1` | `exact_match`、`accuracy` |
| `token_f1@1` | `precision`、`recall`、`f1` |
| `classification@1` | `accuracy`、`precision`、`recall`、`f1` |
| `retrieval_ranking@1` | `recall_at_k`、`mrr`、`ndcg_at_k` |
| `llm_judge@1` | `judge_score`、`pass_rate` |
| `custom@1` | Benchmark 自定义 Metric |

每个 Profile 定义标准评分输入、Metric 名称、值域、单位、优化方向、Case 级评分和默认聚合方式。Benchmark 声明 Profile、primary metric 和需要输出的 metrics：

```yaml
evaluation:
  profile: llm_judge@1
  primary_metric: judge_score
  metrics: [judge_score, pass_rate]
```

预置 Profile 下，`score_predictions.py` 只负责把 Benchmark 字段映射为 Profile 标准输入。`custom@1` 下，Benchmark 实现完整评分逻辑，但仍返回统一 Metric Envelope。

Metric Envelope 至少包含：

```json
{
  "metrics": [
    {
      "name": "judge_score",
      "value": 0.8,
      "direction": "higher_is_better",
      "scope": "run",
      "unit": "ratio"
    }
  ],
  "primary_metric": "judge_score",
  "judge": null,
  "artifacts": []
}
```

使用 LLM Judge 时，`judge` 必须记录模型、Judge Prompt/rubric 版本和必要的可复现配置，但不得记录凭据。

正确性、F1、Recall@K 等属于 Benchmark Evaluation Metrics；延迟、Token、请求数和错误率属于平台 Runtime Metrics，两者分开存储和展示。

## 10. Golden Test

Golden Test 防止出现“流水线能够运行，但任务生成或评分结果静默错误”的情况。

Benchmark 至少覆盖两条链路：

```text
source_sample.json
  → build_tasks.py
  → expected_tasks.json
```

```text
predictions.json
  → score_predictions.py
  → expected_metrics.json
```

`expected_metrics.json` 是固定小样本的预期 Benchmark Metric，不是 Benchmark Skill 自身的质量分。Benchmark Skill 的接入结果是 Conformance `PASS/FAIL`。

Golden 输入必须小型、确定性、可提交并能人工确认。预期结果优先来自官方参考实现、官方示例或简单可手算样本，不能由待测试 scorer 生成后未经确认直接提交。预置 Profile 的公共公式由平台测试，Benchmark Golden 主要验证字段映射和聚合输入；custom scorer 必须提供可人工确认的评分样本。

对于 LLM Judge，Golden Test 使用固定 Judge 响应验证解析、归一化和聚合。真实 LLM 调用放入 integration/smoke test，只校验协议、值域、Judge 版本和可追溯性，不要求每次返回完全相同的分数。

## 11. 分层校验

统一校验框架分为六层：

1. **Static Validate**：目录、Manifest、entrypoint、协议版本、环境变量声明和依赖边界。
2. **Type Contract**：Benchmark、Agent 或 Memory 的请求响应协议。
3. **Capability Contract**：Manifest 声明的 action 或能力确实可调用。
4. **Evaluation Profile Contract**：仅用于 Benchmark，检查 Metric 和聚合规则。
5. **Golden Test**：检查 Integration Skill 特有的数据和字段映射。
6. **Compatibility Smoke**：选择已验证的其他 Skill，执行代表性 Case。

所有 Integration Skill 共用目录、版本、错误、日志和结果 Envelope 校验；不同类型使用不同契约；不同 Benchmark 使用自己的 Profile 和 Golden Case。平台统一验证形式和证据，不用一个业务分数衡量所有 Skill。

## 12. 标准生成与接入流程

### 12.1 生成骨架

Agent 根据用户指定的类型和 ID 调用平台命令。命令可以由 Agent 自动发起，但目录和代码框架由平台 Scaffolder 生成：

```bash
memory-bench integration create benchmark locomo \
  --evaluation-profile llm_judge@1
```

如果 Profile 无法提前确定，先使用 `custom@1`，深入分析后再修改 Manifest。

### 12.2 分析并填充

Agent 同时阅读新系统源码、文档和平台生成的实现占位，填写 Manifest、Adapter、Benchmark 特有 validator 和 Golden Fixture。Agent 可以生成这些外部系统特有内容，但不能修改 SDK、Onboarding Kit、公共 Profile 或 Conformance 规则。修改范围必须限制在新 Integration Skill 目录内。

### 12.3 静态校验

```bash
memory-bench integration validate skills/benchmarks/locomo
```

该命令运行平台通用校验，并调用 Benchmark 自己的 `scripts/validate.py` 检查数据语义。

### 12.4 契约与 Golden 测试

```bash
memory-bench integration test skills/benchmarks/locomo
```

测试按公共契约、类型契约、能力契约、Profile 契约和 Golden Case 的顺序执行。所有 PASS/FAIL 由平台测试工具计算；Agent 只能根据结构化诊断迭代修复，不能在 Manifest 或 Skill 输出中自行声明通过。

### 12.5 隔离 Smoke Test

每次只引入一个新变量：

- 新 Benchmark 搭配已验证的参考 Agent/Memory。
- 新 Agent 搭配平台最小 Benchmark。
- 新 Memory 搭配 `ovtest-memory` 和参考 Agent。

```bash
memory-bench integration smoke \
  --benchmark locomo \
  --agent reference-agent \
  --memory openviking
```

先完成 requirements/capabilities 匹配，再执行代表性 Workflow。

### 12.6 接入凭证

全部通过后生成 `integration_receipt.json`，记录 Skill 与协议版本、Profile、能力、Golden Case 哈希、测试结果、已验证组合、环境摘要和警告。该文件供 CI 和回归测试使用。

## 13. 错误与安全

标准错误码至少包括：

```text
MANIFEST_INVALID
PROTOCOL_INCOMPATIBLE
DEPENDENCY_MISSING
CONFIG_MISSING
CONNECTION_FAILED
CAPABILITY_MISSING
ADAPTER_TIMEOUT
OUTPUT_INVALID
GOLDEN_MISMATCH
SMOKE_FAILED
```

诊断信息必须给出失败阶段、Skill ID、文件或 Manifest 字段、期望值、实际值和修改建议。API Key、完整敏感请求和未经处理的 Memory ingest 原文不得进入错误、日志、Artifact 或 Integration Receipt。

## 14. 接入完成标准

Integration Skill 达到“可用”必须同时满足：

```text
Static Validate       PASS
Type Contract         PASS
Capability Contract   PASS
Golden Test           PASS
Compatibility Check   PASS
Smoke Run             PASS
Core Code Changes     0
```

`Core Code Changes 0` 指每次接入新的 Benchmark、Agent 或 Memory 时，变更应限制在该 Integration Skill 目录及其接入凭证；建设 Integration SDK 和 Onboarding Kit 本身不受此条限制。

如果 Agent 发现现有协议无法表达新系统，应停止接入并提出独立的协议扩展设计，不能通过修改核心代码或增加 Skill ID 特判绕过契约。

## 15. 后续演进

v1 验证稳定后，可以独立设计以下能力，但不纳入本次实现范围：

1. 面向第三方开发者的配置化、自助式接入。
2. 远程 Integration Skill Registry 和包签名。
3. Integration SDK 自动迁移和兼容矩阵。
4. Agent 辅助选择 Benchmark Profile 和自动生成更多 Golden Fixture。
5. 被测服务的 prepare/start/reset/teardown 生命周期托管。
