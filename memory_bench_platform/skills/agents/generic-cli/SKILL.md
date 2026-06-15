# Generic CLI Agent Skill

负责把统一任务通过 stdin/stdout 方式适配给通用 CLI Agent。

## 接入约束

- 默认优先选择被测 CLI 的当前最新正式 release tag。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- 如果该 CLI 没有正式 tag 体系，run 记录中必须显式保存二进制来源、commit 或制品版本。
