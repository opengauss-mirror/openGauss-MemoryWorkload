# Hindsight backend_direct

通过官方 HTTP API 接入自部署 Hindsight；不修改官方源码，不调用 reflect 生成答案。

- `ingest`：整段历史原文提交到原生异步 retain，映射历史时间和 document_id。
- `status`：验证真实 retain operation，并等待该独立 bank 的后台操作（含 consolidation）结束。
- `recall`：调用原生召回，保持返回排序，按 Workload node_limit 截取证据。
- 无独立 flush；现有 composer 生成每段 `ingest → wait`，全部就绪后 QA。
- 每个运行样本的 scope_id 映射到独立 bank；禁止其他客户端向该 bank 写入。

配置和运行步骤见仓库 `docs/hindsight-backend-direct.md`。
