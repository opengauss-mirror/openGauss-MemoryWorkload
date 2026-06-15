# ovtest-admin-memory

Native workflow case source for exercising OpenViking admin account creation,
memory insertion, and retrieval through the `ov` CLI.

## 版本约束

- 默认要求被测 `OpenViking` 使用当前最新正式 release tag。
- `manifest.yaml` 必须声明 `version_policy.default_selection=latest_official_release_tag`。
- 若使用非正式构建、历史 tag 或本地 commit，必须在 run 记录中显式标记来源与原因。
