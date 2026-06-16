# Hermes Agent Skill

负责通过 `hermes chat -q -Q` 的单轮查询模式把平台统一的 task 输入交给 Hermes Agent。

## 运行前提

- `hermes` CLI 可执行
- Hermes 已完成基础模型配置
- 若需要外部记忆，需先完成 `memory.provider` 配置并保证后端服务可用

## 版本约束

- 默认优先选择 Hermes 当前最新正式 release tag。
- 除非 run 配置显式指定允许的 override，否则不应默认回退到旧 tag 或开发构建。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- `manifest.yaml.version_policy.targets` 应声明 `hermes` 对应的 `version_source=upstream_release_tag` 与上游仓库位置。
- 如果使用非正式构建，必须在 run 记录中显式标记。

## 运行记录要求

- 每次真实对接至少记录：
  - Hermes 运行版本
  - 是否采用默认最新 tag，还是显式 override 到指定正式 tag
  - 版本来源对应的上游仓库
  - 是否为正式 tag / release
- 若使用旧 tag 复现历史问题，必须在 run 结论中写明回退原因。

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
