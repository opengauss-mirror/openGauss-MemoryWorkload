# LoCoMo Benchmark Skill

负责将 LoCoMo 数据集整理为平台统一的多轮记忆任务。

## 平台派生关系

- `memory_bench_platform` 是 LoCoMo 评测的主入口与归档/分析框架。
- `locomo_test` 不应继续被视为与平台平行的独立体系，而应视为：
  - 基于 `memory_bench_platform` 的 LoCoMo 专项执行层
  - 负责远端 OpenClaw/OpenViking runtime bootstrap、专项 QA/closure 诊断、以及 LoCoMo 专用结果导出
- 在 skill 层上，这种关系通过 `locomo_test_remote` external runner entrypoint 体现：
  - 平台负责 run 生命周期、版本策略、资源监控、统一报告
  - `locomo_test` 负责 LoCoMo 专项执行实现

## 外部 runner 约束

- `official_small` 这类 external runner 依赖真实 `OpenClaw/OpenViking` 环境。
- `locomo_test_remote` 代表“通过 `memory_bench_platform` 调起 `locomo_test`”的 LoCoMo 专项路径。
- 默认要求被测软件使用“当前最新正式 release tag”。
- 除非 run 配置显式指定允许的 override，否则 benchmark skill 不应自行退回旧版本。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`，让平台能结构化读取该约束。
- `manifest.yaml.version_policy.targets` 应至少声明 benchmark 自身的 `version_source=upstream_release_tag` 与上游仓库；被测 agent stack 的版本约束由 agent skill 单独声明。
- 如果使用非正式版本，必须在 run 记录或分析报告中显式写明原因。

## 版本优先级

1. 用户明确指定的正式版本
2. 上游仓库当前最新正式 release tag
3. 经验证的回退正式 tag

若使用第 1 或第 3 类 override：

- 必须在 run archive / analysis 中写明 override 来源与理由
- 不得继续把结果表述为“默认官方基线”

不应默认使用：

- `dirty` 工作树
- `dev` 版本号
- 只在本地提交未发布的 commit

## 对结果解释的影响

- 若 `LoCoMo small` 出现大面积 `memories=0`：
  - 优先检查 `OpenViking` 版本/配置/写入链路
- 若发现“最新 tag”来源不明确：
  - 归类为 skill 契约不完整，应补 `targets[].upstream`
- 若 `qa_results.csv` 与平台 `summary/case_results` 数量不一致：
  - 归类为 external runner 导出或平台 importer 问题
  - 不应再归因到 `OpenViking tag` 本身

## locomo_test_remote 的职责边界

- `memory_bench_platform`
  - 选择 benchmark / agent skill
  - 落版本策略与 run archive
  - 持续资源监控
  - 统一 `analysis.json` / `run_report.html`
- `locomo_test`
  - 构造 LoCoMo 专项运行配置
  - 启动 isolated OpenClaw/OpenViking runtime
  - 导出 `qa_results.csv` / `qa_diagnostics.json` / `meta.json`
  - 输出 OpenViking closure / recall 专项诊断字段

## OpenViking 闭环要求

- 对接 OpenViking 作为记忆后端时，LoCoMo benchmark 不应只把对话 turn 写入 session。
- 平台或 external runner 必须保证真实链路满足：
  - `messages -> commit -> recall`
- 若平台实现把 `direct_ov_stable` 简化成“跳过 compact/commit”，则该 run 应视为对接不完整，不应直接拿来解释 recall 能力。

## recall=0 的归因优先级

- 若出现以下组合现象：
  - `qa_results.csv` 里 `ov_llm_total_tokens` 已非 0
  - `ov_missing_records` 持续大于 0
  - OpenViking memory 文件系统中已能看到对应 memory 文件
  - 但 LoCoMo 回答仍然大面积 `I don't have any recalled memory...`
- 优先归因为 OpenViking vectordb / index consistency 缺陷，而不是 LoCoMo benchmark 数据或 question prompt 问题。
- 特别是当：
  - 运行态 `observer/vikingdb` 报 `Vector Count > 0`
  - fresh backend 复查却 `collection_exists=False`
- 应明确标记为 SUT 缺陷，避免把该 run 误判为 benchmark skill 未对接完成。
