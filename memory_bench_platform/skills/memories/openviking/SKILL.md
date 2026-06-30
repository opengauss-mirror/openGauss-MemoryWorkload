# OpenViking Memory Skill

负责把 memory backend 相关策略从 benchmark 层剥离出来。

## 当前职责

- benchmark ingest 单位固定为 `session`
- benchmark 不负责 chunk 切分
- 框架也不负责 chunk 切分
- 若需要 chunking，应由被测 agent / memory system 自身决定
- 平台和 benchmark 只观测：
  - session accepted
  - session completed
  - recall / consistency / token / memory count

## 后续方向

- benchmark 侧只保留 session 顺序、完成信号、诊断期望
- 若被测系统未来提供原生长文本 ingest 策略配置，可由 skill 只声明能力，不实现 chunk 算法
