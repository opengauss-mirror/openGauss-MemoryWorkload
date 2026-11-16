# JiuwenMemory backend_direct

Workload 使用 JiuwenMemory v0.2.0 的原生 HTTP API。每段历史原文通过 `POST /v1/add` 同步写入，`system_metadata.infer="true"` 启用原生抽取。QA 通过 `POST /v1/search` 取得原生排序的记忆，再交给独立的 OpenClaw 会话回答。历史时间通过 `occurred_at` 传入，日期同时映射到原生 `system_metadata.observation_date`，供抽取器解析相对时间。历史正文保持原样。适配器使用同一 episode 的稳定后端 session 隔离数据，历史 session ID 另记在写入元数据中。

按 [JiuwenMemory v0.2.0 发行说明](https://github.com/openJiuwen-ai/agent-memory/releases/tag/v0.2.0) 启动原生 HTTP 服务。为后端配置真实的 LLM、Embedding 和抽取器。评测时可使用官方本地内存存储。隔离开发环境可以使用原生 dev 认证，并将服务绑定到回环地址。其他环境使用对应的正式认证配置。

| 环境变量 | 含义 |
| --- | --- |
| `JIUWENMEMORY_API_URL` | 服务根地址，默认 `http://127.0.0.1:8137`，不含 `/v1` |
| `JIUWENMEMORY_ORG`、`JIUWENMEMORY_USER` | 与认证身份匹配，原生本地 dev 默认是 `local`、`developer` |
| `JIUWENMEMORY_API_KEY` | 服务要求 Bearer 认证时设置 |
| `JIUWENMEMORY_RUNTIME_VERSION` | 记录实际运行版本，例如 `0.2.0` |

在 `memory_bench_platform` 目录执行。OpenClaw、Judge 和 Token 代理沿用现有配置。需要记忆侧 Token 时，将 JiuwenMemory 的 LLM 和 Embedding 请求指向本轮独占代理，并按 [Token 统计说明](token-usage-accounting-design.md) 让 Workload 读取该代理的审计目录。

```bash
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend jiuwenmemory \
  --memory-integration backend_direct --data-path /path/to/locomo-small.json \
  --run-id jiuwenmemory-small
python -m memory_bench_platform.cli analyze-run --run-dir runs/jiuwenmemory-small
```

评测报告沿用 Workload 的准确率、评分覆盖率、Token 和阶段耗时。官方 LoCoMo 评测脚本逐轮写入，Workload 按历史 session 整段写入，两种运行的准确率与消耗应结合输入粒度比较。

## 可选的来源 ID 提示

在 JiuwenMemory 独占 Token 代理的 JSON 配置中设 `"memory_backend": "jiuwenmemory"` 和 `"jiuwen_source_id_guidance": true`，并将后端模型地址指向该代理的 `/v1`。默认关闭。开启后，代理只给识别到的 JiuwenMemory v0.2.0 原生抽取请求补一句说明，要求 `source_id` 使用外层 UUID。代理保留抽取请求原生的 `temperature=0`，历史正文不变，模型响应由 JiuwenMemory 原生校验。上游如果还有参数代理，应保留该温度。

每个请求只转发一次，用量仍由现有代理记账。比较跑分时记录是否启用了提示词补充。本轮单段历史 A/B 两组均为 6/6 次首轮完全匹配，补充提示词的 LoCoMo-small 无额外重试跑通一次。
