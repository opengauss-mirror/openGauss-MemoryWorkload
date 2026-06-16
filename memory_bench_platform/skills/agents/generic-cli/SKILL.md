# Generic CLI Agent Skill

负责把统一任务通过 stdin/stdout 方式适配给通用 CLI Agent。

## 接入约束

- 默认优先选择被测 CLI 的当前最新正式 release tag。
- 若没有显式 override，不应默认退回旧 tag、脏工作树或本地开发构建。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- 若该 CLI 已被收敛为正式对接 skill，`manifest.yaml.version_policy.targets` 应声明 `version_source=upstream_release_tag` 与 `upstream`。
- 若该 skill 只是通用包装层，可声明 `version_source=runtime_observed_only`，但不应作为官方 benchmark 基线的最终形态。
- 如果该 CLI 没有正式 tag 体系，run 记录中必须显式保存二进制来源、commit 或制品版本。

## 运行记录要求

- 每次真实对接至少记录：
  - 被测 CLI 的实际运行版本
  - 本次运行是否沿用了 `latest_official_release_tag` 默认策略，还是走了显式 override
  - 来源是正式 tag、commit 还是本地构建制品
  - 若存在上游仓库，则记录对应上游位置
