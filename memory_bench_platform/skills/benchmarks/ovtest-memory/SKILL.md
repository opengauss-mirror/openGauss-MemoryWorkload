# ovtest-memory

Native workflow case source for validating workflow/case execution with bash and
wait operators.

## 版本约束

- 默认要求被测 `OpenViking` 使用当前最新正式 release tag。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- 若为了复现历史问题固定旧版本，必须在 run 记录中显式写明版本和原因。
