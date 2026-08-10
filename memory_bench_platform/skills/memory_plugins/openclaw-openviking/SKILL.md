# OpenClaw + OpenViking Memory Plugin Skill

负责将 OpenClaw 的 OpenViking Context Engine 插件接入平台的 `agent_plugin` 测试模式，并管理记忆写入、抽取、召回和配置恢复的完整生命周期。

在该模式下，Benchmark Scenario 只描述原始 Session、Checkpoint 和 QA 问题。记忆捕获、OpenViking 写入、Recall 和上下文组装均由 Agent 插件负责，平台不直接查询记忆并注入答案 Prompt。

## 标准生命周期

```text
validate
-> prepare
-> set_phase(ingest)
-> Agent 接收原始 Session
-> commit
-> wait_ready
-> set_phase(qa)
-> Agent QA
-> finalize
```

每个 Session 都应执行一次 `Agent ingest -> commit -> wait_ready`，全部 Session 完成后再进入 QA 阶段。旧动作 `flush / wait_settle` 仅作为兼容别名保留。

## 生命周期动作

- `validate`：检查 OpenClaw 中的 OpenViking 插件是否存在、是否启用，以及完成生命周期所需的配置是否可用。
- `prepare`：保存测试前的插件配置，并根据 run 和 sample 建立独立命名空间，避免不同测试之间发生记忆串扰。
- `set_phase(ingest)`：开启自动捕获、关闭自动召回，并禁止 `afterTurn` 按阈值自动 Commit。此阶段只接收和抽取测试 Session，不向 Agent 注入历史记忆。
- `commit`：由本 Adapter 调用 OpenClaw 原生 `sessions.compact`，让 Context Engine 插件完成 Session 提交、记忆抽取和上下文重建；平台不直接调用 OpenViking Commit。
- `wait_ready`：把 Adapter 已完成的 Compact 结果作为可用屏障；兼容旧操作时仍可按照准确 `task_id` 查询 OpenViking 状态。
- `set_phase(qa)`：关闭自动捕获、开启自动召回，使 QA 只读取已有记忆，避免问题和答案污染被测记忆。
- `finalize`：恢复测试前保存的插件配置；无论测试成功还是失败，都应执行该动作。

## 阶段配置

写入阶段使用以下语义：

```text
autoCapture = true
autoRecall = false
captureMode = semantic
automaticCommit = false
```

实现上通过提高 `commitTokenThreshold` 并设置 `commitKeepRecentCount=0`，阻止 `afterTurn` 自动 Commit；真正的抽取时机由 Adapter 通过 Agent 原生 Compact 控制，保证每个 Session 只提交一次。

QA 阶段使用以下语义：

```text
autoCapture = false
autoRecall = true
```

## Session 与命名空间约束

- 使用稳定算法把平台的语义 `session_key` 转换成 OpenClaw 可接受的 UUID 格式 `session_id`。
- 使用 run 和 sample 生成独立的 `agent_prefix`，保证不同运行、不同样本的 OpenViking Agent Scope 相互隔离。
- Agent Runner 必须返回实际的 Gateway Session Key，Adapter 使用该句柄调用 Compact，避免把平台语义 `session_key` 错当成 OpenClaw 内部 Session Key。

## 职责边界

该 Skill 通过 OpenClaw 原生 Compact 触发抽取；只有兼容旧任务状态时才允许读取 OpenViking Task 状态。它不得：

- 在 Benchmark 层直接调用 OpenViking Search API。
- 把平台查询到的 Recall 内容直接注入答案 Prompt。
- 让 Benchmark 感知 OpenViking 的 Commit 阈值、保留消息数等插件实现参数。
- 在 QA 阶段继续捕获问题或答案。

如果需要适配其他 Agent 或记忆插件，应复用 `prepare / set_phase / commit / wait_ready / finalize` 通用协议，由对应 Agent-Memory Adapter 决定 `commit` 映射到 Compact、Flush、Commit Hook 还是同步空操作。
