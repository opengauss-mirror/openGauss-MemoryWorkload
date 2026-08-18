# ovtest-memory

Native workflow case source for validating workflow/case execution with bash and
wait operators.

## 版本约束

- 默认要求被测 `OpenViking` 使用当前最新正式 release tag。
- 若没有显式 override，不应默认切到历史 tag 或本地开发构建。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- `manifest.yaml.version_policy.targets` 应声明 `OpenViking` 的 `version_source=upstream_release_tag` 与上游仓库位置。
- 若为了复现历史问题固定旧版本，必须在 run 记录中显式写明版本和原因。
