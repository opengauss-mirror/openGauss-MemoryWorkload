# OpenClaw + JiuwenMemory 官方 MCP

历史与 QA 都通过 OpenClaw。历史阶段由 `jiuwen-ingest` Agent 调用 JiuwenMemory v0.2.1 官方 MCP 的 `memory_add`。Workload 核对工具参数中的完整历史和日期、调用次数及回执，再进入 QA。每道 QA 使用独立 `main` 会话，由 Agent 主动调用 `memory_search`，随后使用所选 benchmark 的 Workload Judge。原生 HTTP `backend_direct` 入口见 [独立后端模式](jiuwenmemory-backend-direct.md)。

## 准备服务

1. 按 [JiuwenMemory v0.2.1 MCP 文档](https://github.com/openJiuwen-ai/agent-memory/blob/v0.2.1/docs/features/F03-mcp-surface.md) 安装官方 MCP 依赖。为独立评测实例配置 LLM、Embedding 和抽取器。让记忆侧模型地址指向本轮独占 [Workload Token 代理](token-usage-accounting-design.md)，代理配置的 `memory_backend` 为 `jiuwenmemory`。启动 Streamable HTTP，例如在本地隔离环境中运行：

   ```sh
   MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8139 \
     JIUWEN_MEMORY_MCP_AUTH_MODE=dev scripts/run-mcp.sh ./jiuwen-config.yaml
   ```

2. 启动同主机的专用 OpenClaw Gateway（已验证 2026.6.9）。沿用现有模型和 Gateway 认证配置，开启 `gateway.http.endpoints.responses.enabled`。把以下键合并到 Gateway 的实际配置，移除旧薄插件的加载配置：

   ```json
   {
     "plugins": {"slots": {"memory": "none"}},
     "tools": {"allow": ["jiuwen__memory_add", "jiuwen__memory_search"]},
     "agents": {
       "list": [
         {"id": "main", "default": true, "tools": {"allow": ["jiuwen__memory_search"]}},
         {"id": "jiuwen-ingest", "tools": {"allow": ["jiuwen__memory_add"]}}
       ]
     },
     "mcp": {
       "servers": {
         "jiuwen": {
           "url": "http://127.0.0.1:8139/mcp",
           "transport": "streamable-http",
           "toolFilter": {"include": ["memory_add", "memory_search"]},
           "timeout": 600
         }
       }
     }
   }
   ```

   Gateway 必须能访问 MCP 端点。使用独立 Docker 容器和 host networking，以 `gateway run` 启动。用 bind mount 挂载配置文件和 state 目录，在容器中显式设置 `OPENCLAW_CONFIG_PATH` 与 `OPENCLAW_STATE_DIR`。先完成配置再启动 Gateway，修改配置后重启。正式认证场景在 Gateway 的 MCP 配置里提供凭据。两个 Agent 使用同一个记忆范围，各自只开放对应工具。

## 执行 LoCoMo 或 LongMemEval

在仓库的 `memory_bench_platform` 目录执行。密钥由启动环境提供。

```sh
python -m pip install -e .
export JIUWENMEMORY_MCP_URL=http://127.0.0.1:8139/mcp
export JIUWEN_MCP_GATEWAY_CONTAINER=your-openclaw-container
export OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
export OPENCLAW_TRANSPORT=http
export OPENCLAW_AGENT_ID=main
export OPENCLAW_CONFIG_PATH=/path/to/openclaw.json
export OPENCLAW_STATE_DIR=/path/to/gateway-state
export MEMORY_BENCH_MEMORY_TOKEN_SOURCE=model_proxy
export MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE=1
export MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR=/path/to/fresh-memory-proxy-audit
export LOCOMO_BASE_URL=https://model.example/v1
export LOCOMO_METRIC_MODEL=your-judge-model
export MEMORY_BENCH_JUDGE_MAX_TOKENS=2048
# 从私有环境加载 OPENCLAW_GATEWAY_TOKEN 和 LOCOMO_API_KEY。
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend jiuwenmemory \
  --memory-integration agent_plugin --data-path /path/to/locomo-small.json \
  --run-id jiuwen-mcp-locomo-small
```

跑 LongMemEval 时，改用 `--benchmark longmemeval --data-path /path/to/longmemeval.json`，并配置 `LONGMEMEVAL_BASE_URL`、`LONGMEMEVAL_METRIC_MODEL` 和 `LONGMEMEVAL_API_KEY`。每轮使用新的 `--run-id` 和独占代理审计目录。

原生本地 dev 身份默认为 `local/developer`。可通过 `JIUWENMEMORY_ORG` 和 `JIUWENMEMORY_USER` 设置实际身份。QA 和 Judge 并发沿用统一 CLI 的限制和默认值。

结果位于 `runs/<run-id>/reports/`。`summary.json` 含准确率和评分覆盖率，其中 `resource_summary.memory_plugin_finalize.output.recall_verification` 分别记录工具调用覆盖率、成功回执与非空记忆，同级 `ingest_verification` 记录历史写入核验覆盖率。逐题评分见 `case_results.json`，两侧 Token 见 `token_summary.md`。Cache Read 已计入输入，Embedding 单列，Judge 用量排除。Token 报告分别列出记忆侧 LLM、Agent 历史写入和 Agent QA。报告中的 `recall_mode: agent_tool` 表示 MCP 主动检索。
