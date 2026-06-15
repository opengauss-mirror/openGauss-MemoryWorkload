# ovtest-health

Native workflow case source for validating OpenViking health through the http
operator.

## 版本约束

- 默认要求被测 `OpenViking` 使用当前最新正式 release tag。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- 若为了兼容性排查临时回退旧版本，必须在 run 记录中显式保存版本和回退原因。
