# LoCoMo Small 平台复盘与骨架调整

日期：2026-06-27

更新：2026-06-28

- 当前已验证最新稳定 small 基线：
  - `locomo-test-remote-small-rerun-20260628g`
  - `30/35 = 85.71%`
- 当前平台侧统一导回 run：
  - `memory_bench_platform/runs/locomo-test-import-20260628g`
  - `summary.status = passed`
  - `overall_accuracy = 0.8571`
- 当前平台统一 HTML 报告也已同步修正：
  - `run_report.html` 会直接展示 `QA Mode`
  - 当检测到 `openviking_direct_recall_only_mode` 时，明确标注为“已验证的有效 QA 模式”
  - 不再把这类 run 误解释为 `openviking_tokens_all_zero` 异常
- `qa_direct_recall_only` 现已被重分类为 `openviking_direct_recall_only_mode`：
  - 表示 QA 主要通过 direct recall 命中 memory 后直接回答
  - 不再按 `openviking_tokens_all_zero` 解释为异常链路
- smoke skill 已落成可执行的最小闭环探针：
  - `memory_bench_platform/skills/smoke/locomo-openclaw-openviking-minimal/`
  - 当前已补最小平台支持：
    - `list-skills` 可发现 smoke
    - `validate --smoke` 可做静态前置校验
    - `run-smoke` 可执行 smoke skill 并产出 `smoke_trace.json / smoke_summary.json / smoke_report.html`
    - `run --smoke-gate <smoke-id>` 可在正式 benchmark 前执行 smoke gate
  - 2026-06-28 续接验证：
    - `validate --smoke locomo-openclaw-openviking-minimal` 已通过静态校验
    - smoke runtime 会把空 `gateway.state_dir` 补为 run 目录下的隔离 `openclaw-state`
    - smoke runtime 会把 `data_file` 固定到 `locomo_test/data/locomo_small.json`
    - `run-smoke` 已切到远端隔离 runtime 入口，不再依赖本机常驻 `localhost:19790/2936`
    - `locomo-smoke-20260628e` 已通过 8/8 stage：
      - `session_bootstrap`
      - `message_ingest`
      - `session_commit`
      - `memory_extraction`
      - `reindex_or_consistency`
      - `recall_probe`
      - `answer_probe`
      - `result_parse`
    - `locomo-gated-20260628a` 已验证 smoke gate 会阻断正式 benchmark，并写出 `records/smoke_gate.json`

## 当前结论

LoCoMo `small` 的异常低准确率，不是单一模型效果问题，而是“平台对接问题 + 记忆抽取/检索问题 + 回答约束问题”叠加。

已验证的平台侧关键问题：

- OpenViking ingest commit 以前只发起、不等待 extraction 完成，导致长 session 的 extraction task 在 run 收尾阶段被取消。
- QA 阶段虽然 direct recall 命中，但 recall 证据组织不稳定，容易把低价值 ChatLog 噪声和错误日期暴露给回答模型。
- benchmark 配置没有把 `keep_recent_count=0` 显式固化，长会话前半段事实可能被服务端默认裁剪。
- benchmark runtime 默认让插件 `autoRecall=true`，会在 ingest 阶段对整段原始对话做冗余 recall，既拖慢 run，也会引入额外上下文干扰。
- benchmark runtime 没有把 agent 超时预算显式配置化，长 session 在 OpenClaw 侧可能先超时，导致还没进入记忆抽取就失败。
- 统一报告以前没有把 “ingest 是否真的完成”“memory extraction 覆盖率”“reindex 是否成功” 作为一级门禁展示。

已验证的修复收益：

- `session_3/session_4` extraction 不再在收尾阶段被取消。
- OpenViking ingest token / memories 统计恢复为非 0。
- `small` 准确率从 `34.29%` 提升到 `54.29%`。
- benchmark runtime 已开始验证 `autoRecall=false`，避免 ingest 阶段的冗余 recall 超时和上下文污染。
- OpenClaw timeout 已开始配置化为 runtime 字段，不再只能依赖环境变量热补丁。

这说明平台骨架已经能显著影响 benchmark 结果，不能再把“commit accepted”当成“记忆链路已闭环”。

## 是否需要增加 smoke skill

需要，而且应该作为平台标准组件。

### 目标

`smoke skill` 不是正式 benchmark，也不是 agent 的替代 runner，而是“最小闭环打通器”。

它要解决的问题是：

- 新 benchmark 刚接入时，先验证最小链路是否真的通。
- 新 agent / memory backend / runtime 组合接入时，先验证输入输出协议、生命周期、记忆读写、结果提取是否正确。
- 在正式跑大样本前，先把“平台问题”和“被测链路效果问题”拆开。

### 建议放置层级

新增 skill 类别：

- `memory_bench_platform/skills/smoke/...`

而不是把 smoke 混进 benchmark skill 或 agent skill 内核逻辑里。

理由：

- smoke 是跨 benchmark / agent 复用的诊断闭环，不属于某个单侧 skill 的私有逻辑。
- benchmark skill 和 agent skill 仍应各自声明“如何参与 smoke”。
- 平台核心负责发现 smoke skill、装配 smoke plan、采集结果与输出统一报告。

### 建议 manifest 字段

建议新增 `SmokeManifest`，核心字段至少包括：

- `kind: smoke`
- `id`
- `version`
- `entry.probe_builder`
- `entry.validator`
- `entry.reporter`
- `matrix`
  - `benchmark_ids`
  - `agent_ids`
  - `memory_backends`
- `stages`
  - `session_bootstrap`
  - `message_ingest`
  - `session_commit`
  - `memory_extraction`
  - `reindex_or_consistency`
  - `recall_probe`
  - `answer_probe`
  - `result_parse`
- `required_evidence`
  - `task_status`
  - `memory_diff`
  - `search_find_hits`
  - `usage_nonzero`
  - `artifacts_present`
- `pass_criteria`
  - `all_required_stages_passed`
  - `required_memory_count`
  - `required_recall_hit`
  - `required_answer_contains`

### smoke skill 的最小输出

平台应统一产出：

- `smoke_summary.json`
- `smoke_trace.json`
- `smoke_report.html`

至少回答四个问题：

- session 是否创建成功
- commit/extraction 是否真正完成
- memory 是否写入且可检索
- answer/result parser 是否正确取到结果

## 对测试平台骨架要做哪些调整

### 1. 把 ingest completion 提升为平台级 gate

当前问题：

- 平台默认把“commit 已发起”视作 ingest 完成。

建议：

- 在 `protocol.py` 增加显式阶段状态：
  - `commit_requested`
  - `commit_completed`
  - `memory_extraction_completed`
  - `memory_extraction_failed`
  - `memory_extraction_cancelled`
- 在 `StepResultRecord.structured_output` 中强制记录：
  - `task_id`
  - `task_status`
  - `resource_id`
  - `token_usage`
  - `memories_extracted`
  - `consistency_status`
- benchmark run 默认要求 ingest 所有 session 到达 `memory_extraction_completed`，否则整轮应标记为 `partial` 或 `failed`，而不是继续把结果混入正式 accuracy。

### 2. benchmark skill 要能声明执行不变量

当前问题：

- LoCoMo 实际需要 `keep_recent_count=0`，但这个知识现在只在排障过程中体现，不在 manifest 中显式表达。

建议：

- benchmark manifest 增加 `execution.invariants`：
  - `preserve_full_session_history: true`
  - `required_commit_options.keep_recent_count: 0`
  - `required_plugin_options.autoRecall: false`
  - `required_openclaw_timeout_seconds`
  - `required_reindex_before_qa: true`
  - `required_min_extracted_memories_per_sample`
- 平台在 run 前校验这些 invariants 是否被实际 runtime config 满足。

### 3. 把 memory coverage 纳入统一报告主视图

当前问题：

- 以前 HTML/JSON 报告更偏最终 accuracy，对“为什么错”暴露不够。

建议：

- 统一报告主视图增加：
  - `ingest session count`
  - `completed extraction session count`
  - `memory_diff add/update/delete`
  - `reindex status`
  - `recall hit distribution`
  - `answer token distribution`
  - `failure bucket by chain stage`

### 4. 把 QA usage 和 memory usage 分成两条统计链

当前问题：

- QA token 和 OV token 来源不同，曾经互相覆盖或落零。

建议：

- `qa_usage`
  - source: OpenClaw jsonl / API fallback
- `memory_usage`
  - source: OpenViking task/session usage
- 平台 summary 中分别展示：
  - `qa_total_tokens`
  - `memory_llm_total_tokens`
  - `memory_embedding_tokens`
  - `memory_extracted_count`

### 5. 在 skill 层补“证据探针”，不要只靠最终回答

建议 benchmark/agent skill 都允许注册 probe：

- `post_ingest_probe`
- `post_commit_probe`
- `post_recall_probe`
- `post_answer_probe`

probe 的作用是把“记忆写了没有”“检索命中了没有”“parser 是否取到了 usage/result”这些中间事实标准化落盘。

### 6. 平台要允许 benchmark 显式关闭被测运行时中的“辅助记忆行为”

当前经验说明：

- 如果平台已经在 QA 阶段做 direct recall，再让插件在 ingest/answer 阶段自动 recall，容易形成双重记忆注入。
- 这类行为不一定总是有益，benchmark 应该能显式要求关闭。

建议：

- benchmark manifest 增加 `runtime_overrides` 能力。
- 第一批至少支持：
  - `openviking.autoRecall`
  - `openviking.autoCapture`
  - `openviking.bypassSessionPatterns`
  - `openviking.keep_recent_count`
  - `openclaw.timeout_seconds`

## 如果扩展一个新的 benchmark，会有哪些问题

### 1. 只靠现有 benchmark skill，不足以保证最小链路先打通

问题：

- 新 benchmark 通常先死在字段映射、session 生命周期、结果提取，而不是评分逻辑。
- 现有结构里 benchmark skill 偏“正式任务构建”，缺少 smoke 层。

后果：

- 首轮接入时，容易把平台问题误判成 benchmark 本身效果差。

### 2. 多 benchmark 的时间/上下文约束还没有被统一表达

LoCoMo 暴露的问题说明：

- benchmark 不只是“给题和评分”。
- 有些 benchmark 需要保留完整多轮上下文。
- 有些 benchmark 对相对时间解释敏感。
- 有些 benchmark 需要记忆写入后再检索，不允许仅靠上下文回答。

当前 manifest 里这些约束还只是弱配置，缺少强校验。

### 3. skill 目前缺少“最小可运行样例集”的第一类支持

建议每个 benchmark skill 标准化补：

- `smoke_cases`
- `golden_cases`
- `full_cases`

这样平台才能明确区分：

- 对接打通
- 回归验证
- 正式跑数

### 4. 新 benchmark 的结果提取链容易重复踩坑

当前平台虽然有：

- external runner import
- result analysis
- unified html report

但如果新 benchmark 的原始输出结构不同，仍可能出现：

- usage 字段取不到
- result 标签取不到
- case_id 对不齐
- 统计维度缺失

建议：

- 为 benchmark skill 增加 `result_adapter` 声明
- 平台导入层按 adapter 解析，而不是把 CSV/JSON 结构默认写死

## 下一步建议

### A. 先做 smoke skill MVP

第一版只覆盖 LoCoMo + OpenClaw + OpenViking：

- 单 sample
- 单 session
- 单 recall 问题
- 单 answer 问题
- 输出完整链路证据

### B. 把 benchmark invariant 写进 manifest

优先补：

- LoCoMo `keep_recent_count=0`
- `required_reindex_before_qa=true`
- `requires_completed_ingest_extraction=true`

### C. 让统一报告显式区分三类失败

- 平台链路失败
- 记忆系统失败
- 回答模型/判断失败

这样扩新 benchmark 时，才能先定位失败层，再谈调参和效果。
