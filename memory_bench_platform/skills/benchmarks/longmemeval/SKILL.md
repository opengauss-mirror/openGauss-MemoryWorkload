# LongMemEval Benchmark Skill

负责把 LongMemEval 的带时间戳多 Session 历史转换为平台通用 Benchmark Scenario。

Benchmark Skill 只保留 Session、时间戳、问题、标准答案和题型，不生成 Memory 写入、Commit、等待或 Agent QA 步骤；这些步骤由 Runtime Composer 根据运行模式生成。

## 接入约束

- 默认要求被测软件使用“当前最新正式 release tag”。
- 除非 run 配置显式指定允许的 override，否则不应默认固定旧 tag。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- `manifest.yaml.version_policy.targets` 应声明 LongMemEval 自身的 `version_source=upstream_release_tag` 与上游仓库位置。
- 如果为了复现历史问题而固定旧版本，必须在 run 记录中显式记录版本和原因。

## 运行记录要求

- 每次真实对接至少记录：
  - LongMemEval benchmark 版本
  - 被测 agent / memory backend 的实际运行版本
  - 是否沿用默认最新 tag，还是显式 override 到指定版本
  - 若使用回退 tag，记录回退原因
