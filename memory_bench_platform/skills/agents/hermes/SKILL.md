# Hermes Agent Skill

负责通过 `hermes chat -q -Q` 的单轮查询模式把平台统一的 task 输入交给 Hermes Agent。

## 运行前提

- `hermes` CLI 可执行
- Hermes 已完成基础模型配置
- 若需要外部记忆，需先完成 `memory.provider` 配置并保证后端服务可用

## 输入

- `system_prompt`
- `messages`
- `attachments`
- `metadata.model`
- `metadata.provider`

## 输出

- 最终文本回答
- 原始 stdout / stderr
- 执行耗时

## 当前限制

- 当前 runner 通过 prompt 展平方式传递多轮消息，不是 Hermes 原生 session continuation 模式
- 若 `hermes chat -q -Q` 返回空 stdout，或返回鉴权错误，应视为异常，不应视为通过
