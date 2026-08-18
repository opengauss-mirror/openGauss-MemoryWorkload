# OpenClaw Agent Skill

Agent 任务支持两种传输方式：

- 配置 `OPENCLAW_GATEWAY_URL`（或显式设置 `OPENCLAW_TRANSPORT=http`）时，通过 OpenClaw Gateway 的 `/v1/responses` HTTP Body 传递完整上下文，适合 Recall 证据较大的任务。
- 未配置 Gateway URL 时保留原有 CLI 调用，兼容已有环境。

HTTP 模式可使用 `OPENCLAW_GATEWAY_TOKEN` 认证。插件的配置、Commit 和生命周期管理仍由 Memory Plugin Adapter 负责，不由本 Agent Runner 处理。

负责将统一任务适配到 OpenClaw 运行时。

## 接入约束

- 默认使用正式发布版本，不默认跑脏工作树或本地开发快照。
- 如果未特别说明，`OpenClaw` 与其依赖的 `OpenViking` 都应优先选择“当前最新正式 release tag”。
- 只有 run 配置显式声明允许的 override，才可以偏离默认最新 tag 选择。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`，不能只写在说明文档里。
- `manifest.yaml.version_policy.targets` 应明确写出受该策略约束的软件组件、`version_source=upstream_release_tag`，以及对应上游仓库位置。
- 只有在以下情况才允许偏离最新 tag：
  - 用户明确指定版本
  - 为了复现历史问题而需要固定旧版本
  - 最新 tag 已知不可用，且有明确回退结论

## 版本记录要求

- 每次对接真实环境时，至少记录：
  - `OpenClaw` 版本
  - `OpenViking` 版本
  - 本次运行是否使用默认最新 tag，还是使用了显式 override
  - 两者各自的上游来源
  - 是否为正式 tag / release
- 若实际运行的不是正式 tag，需要在 run 结论里显式标记：
  - `dirty worktree`
  - `dev build`
  - `non-tag commit`

## 问题归因提醒

- 若出现 `memories=0`、大量 `retrieval_miss`、或 `small` 跑分异常偏低：
  - 先确认当前运行版本是否为正式 tag
  - 再确认最新 tag 是否是从 skill 声明的上游仓库解析出来的
  - 不要直接把问题归因到平台对接层

## 对接约束

- 当 `OpenClaw` 以 `OpenViking` 远端 context-engine 运行时，默认应显式写入：
  - `accountId`
  - `userId`
  - `isolateUserScopeByAgent=true`
  - `isolateAgentScopeByUser=true`
- 并保证插件实际发出的 `X-OpenViking-Agent` 与 canonical namespace 对齐：
  - 推荐效果等价于 `accountId + "_" + sessionAgentId`
- 若这层 account-scoped agent id 未对齐，可能出现：
  - 手工 direct probe 能命中
  - 平台自动 recall 仍落到错误 agent scope
  - `official_small` 回答大量表现为 `retrieval_miss`

## Session / Chunk 边界

- benchmark 层只应把完整 `session` 交给 OpenClaw / OpenViking ingest。
- 若长 session 需要内部 chunking，这属于 agent 或 memory backend 的实现策略，不属于 benchmark skill 责任。
- 当前仓库中过渡实现采用 memory skill manifest 驱动 OpenViking ingest chunk 参数，目标是逐步把 chunking 从 `locomo_test` benchmark 逻辑中抽离。

## OpenViking 评测闭环

- 对接 `LoCoMo` / `LongMemEval` 这类多轮记忆 benchmark 时，不要把 `direct_ov_stable` 理解成“跳过 commit”。
- 平台侧应保证 OpenViking 至少走通以下闭环：
  - `messages`
  - `commit`
  - `recall`
- 也就是说，每轮通过 OpenClaw 产出 user/assistant turn 后，必须显式触发一次 OpenViking session commit，至少满足：
  - `wait=false`
  - `keepRecentCount=10`
- 若只写 session messages、不做 commit，常见后果是：
  - memory extraction 未真正执行
  - recall 只能读到 session live context，读不到稳定 memory
  - `qa_results.csv` 回答表现为大面积 `retrieval_miss`
  - token 统计与 official wrapper / locomo-test-kit 口径不一致

## OpenViking vectordb 分叉识别

- 若已经确认：
  - `messages -> commit` 已执行
  - `ov_llm_total_tokens` / `ov_embedding_tokens` 已非 0
  - memory 文件在 `viking://user/.../memories` 下可见
  - 但 `/search/find` 仍然为 0
- 则下一优先级不要继续怀疑 OpenClaw 发送链，而应先排查 OpenViking local vectordb 分叉：
  - 运行态 `observer/vikingdb` 有 `Vector Count`
  - fresh backend / 新进程 `collection_exists=False`
  - `collection_meta.json` 缺失
- 该类问题应归类为被测系统索引持久化缺陷，不应误记为 benchmark skill 或 token 提取链故障。
