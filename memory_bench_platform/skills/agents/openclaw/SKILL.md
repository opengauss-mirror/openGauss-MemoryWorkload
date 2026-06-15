# OpenClaw Agent Skill

负责将统一任务适配到 OpenClaw 运行时。

## 接入约束

- 默认使用正式发布版本，不默认跑脏工作树或本地开发快照。
- 如果未特别说明，`OpenClaw` 与其依赖的 `OpenViking` 都应优先选择“当前最新正式 release tag”。
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
