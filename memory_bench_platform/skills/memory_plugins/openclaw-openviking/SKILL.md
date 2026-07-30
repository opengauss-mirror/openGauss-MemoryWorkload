# OpenClaw + OpenViking Memory Plugin Skill

负责将 OpenClaw 的 OpenViking Context Engine 插件接入平台的 `agent_plugin` 测试模式，并管理记忆写入、抽取、召回和配置恢复的完整生命周期。

在该模式下，Benchmark 只负责把原始 Session 和 QA 问题发送给 OpenClaw。记忆捕获、OpenViking 写入、Recall 和上下文组装均由 Agent 插件负责，平台不直接查询记忆并注入答案 Prompt。

## 标准生命周期

```text
validate
-> prepare
-> set_phase(ingest)
-> Agent 接收原始 Session
-> flush
-> wait_settle
-> set_phase(qa)
-> Agent QA
-> finalize
```

每个 Session 都应执行一次 `Agent ingest -> flush -> wait_settle`，全部 Session 完成后再进入 QA 阶段。

## 生命周期动作

- `validate`：检查 OpenClaw 中的 OpenViking 插件是否存在、是否启用，以及完成生命周期所需的配置是否可用。
- `prepare`：保存测试前的插件配置，并根据 run 和 sample 建立独立命名空间，避免不同测试之间发生记忆串扰。
- `set_phase(ingest)`：开启自动捕获、关闭自动召回，并禁止 `afterTurn` 按阈值自动 Commit。此阶段只接收和抽取测试 Session，不向 Agent 注入历史记忆。
- `flush`：对当前 Agent Session 显式触发一次 OpenViking Commit，并返回对应的 `task_id`。
- `wait_settle`：优先按照 `flush` 返回的准确 `task_id` 等待记忆抽取完成；只有旧调用没有提供任务 ID 时，才使用任务列表轮询作为兼容回退。
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

实现上通过提高 `commitTokenThreshold` 并设置 `commitKeepRecentCount=0`，阻止 `afterTurn` 自动 Commit；真正的 Commit 统一由 `flush` 控制，保证每个 Session 只提交一次。

QA 阶段使用以下语义：

```text
autoCapture = false
autoRecall = true
```

## Session 与命名空间约束

- 使用稳定算法把平台的语义 `session_key` 转换成 OpenClaw 可接受的 UUID 格式 `session_id`。
- 使用 run 和 sample 生成独立的 `agent_prefix`，保证不同运行、不同样本的 OpenViking Agent Scope 相互隔离。
- `flush` 必须与写入该 Session 时使用相同的 Session ID 和 Agent Scope，否则 Commit 和 Recall 可能落入错误的记忆空间。

## 职责边界

该 Skill 可以为生命周期控制调用 OpenViking 的 Commit 和任务状态接口，但不得：

- 在 Benchmark 层直接调用 OpenViking Search API。
- 把平台查询到的 Recall 内容直接注入答案 Prompt。
- 让 Benchmark 感知 OpenViking 的 Commit 阈值、保留消息数等插件实现参数。
- 在 QA 阶段继续捕获问题或答案。

如果需要适配其他 Agent 或记忆插件，应复用 `prepare / set_phase / flush / wait_settle / finalize` 通用协议，把具体配置和接口实现留在各自的 Memory Plugin Skill 中。
