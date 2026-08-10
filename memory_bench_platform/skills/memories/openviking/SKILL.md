# OpenViking Memory Skill

负责把 memory backend 相关策略从 benchmark 层剥离出来。

## 当前职责

- 使用 `ingest` 把原始 Session 写入 OpenViking，但不隐式触发抽取
- 使用 `flush` 把通用落盘动作映射为 OpenViking Session Commit
- 使用 `status` 按 Commit 返回的任务句柄等待抽取完成
- 使用 `recall` 直接查询 OpenViking，并向平台返回召回证据
- benchmark ingest 单位固定为 `session`
- benchmark 不负责 chunk 切分
- 框架也不负责 chunk 切分
- 若需要 chunking，应由被测 agent / memory system 自身决定
- 平台和 benchmark 只观测：
  - session accepted
  - session completed
  - recall / consistency / token / memory count

标准执行顺序为：

```text
ingest -> flush -> wait_ready(status) -> recall
```

其他记忆系统可以把 `flush` 映射为自己的 Commit/Finalize 接口；如果写入天然同步完成，也可以把它实现为幂等空操作。

## 后续方向

- benchmark 侧只保留 session 顺序、完成信号、诊断期望
- 若被测系统未来提供原生长文本 ingest 策略配置，可由 skill 只声明能力，不实现 chunk 算法
