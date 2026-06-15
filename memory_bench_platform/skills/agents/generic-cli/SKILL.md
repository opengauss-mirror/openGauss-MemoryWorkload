# Generic CLI Agent Skill

负责把统一任务通过 stdin/stdout 方式适配给通用 CLI Agent。

## 接入约束

- 默认优先选择被测 CLI 的当前最新正式 release tag。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- 若该 CLI 有明确上游仓库或发行页，`manifest.yaml.version_policy.targets` 应同时声明 `upstream`。
- 如果该 CLI 没有正式 tag 体系，run 记录中必须显式保存二进制来源、commit 或制品版本。

## 运行记录要求

- 每次真实对接至少记录：
  - 被测 CLI 的实际运行版本
  - 来源是正式 tag、commit 还是本地构建制品
  - 若存在上游仓库，则记录对应上游位置
