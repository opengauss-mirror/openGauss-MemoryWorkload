# MemOS Local / OpenClaw agent_plugin

使用未经修改的官方 `@memtensor/memos-local-plugin@2.0.8` 和 OpenClaw 2026.6.9。
这是 Gateway 内的 Local Plugin，不是 `backend_direct` 使用的 MemOS HTTP 后端。
本适配器只接入现有测试生命周期、原生正文读取审计和 Token 统计。

## 部署边界

本 PR 不扩展公共 deploy。原有 `deploy --config`、`deploy status`、
`run --deployment`、`undeploy` 保持上游行为，见
[部署说明](../../../../docs/deployment-yaml.md)。用户仍需准备镜像、原生配置及核验命令。

原框架不会自动生成下面的 MemOS Local runtime，也没有本次已撤除的
`hosted_in_agent`、`runtime_binding`、`model_connections`、`memory_model_proxy`
能力。不要继续使用已删除的 MemOS deployment 模板。Local Plugin 请先准备独占
Gateway 环境，再使用下方显式 `run` 命令；不声称原有 `run --deployment` 已支持
它的自动运行绑定。[backend_direct 配置和命令](../../../../docs/memos-backend-direct.md)
是另一条独立路径。

## 运行前准备

准备全新的专用 runtime，安装官方插件及与 Gateway Node/CPU 架构匹配的依赖。
容器将 runtime 挂载到 `/runtime`，官方包只读挂载到 `/official`，并设置标签
`memory-bench.runtime=<宿主runtime绝对路径>`。不要与其它测试共享存储或 Gateway。

容器环境：

- `MEMOS_HOME=/runtime/memos`
- `OPENCLAW_STATE_DIR=/runtime/state`
- `OPENCLAW_CONFIG_PATH=/runtime/openclaw.json`
- `MEMOS_BEFORE_PROMPT_SOFT_TIMEOUT_MS=30000`（原生外层等待参数，结果须披露）

runtime 包含：

| 文件/目录 | 要求 |
|---|---|
| `openclaw.json` | memory slot 为 `memos-local-plugin`；加载 `/official`；插件启用，`hooks.allowConversationAccess=true`；启用原生 Responses HTTP endpoint；配置实际模型及 Gateway token |
| `memos/config.yaml` | 官方原生配置，lightweight 模式；关闭 hub/telemetry；记忆 LLM/Embedding 指向独占 Token 代理，`llm.fallbackToHost=false` |
| `state/` | Gateway 会话目录 |
| `memos/data/memos.db` | 官方插件创建的 SQLite 数据 |
| `runtime.json` | 记录下述真实运行信息，供适配器核验；不是模型配置 |

`runtime.json` 必需字段：`version: "2.0.8"`、`ingest_recall: "native_enabled"`、
`plugin_path`（宿主官方包绝对目录）、`plugin_hashes`（官方文件相对路径到 SHA256 的映射）、
`memos_config_sha256`、`container`（专用容器名）、`gateway_url`、`viewer_url`。
SHA256 必须从实际文件计算；Viewer 是官方原生接口，通常使用端口 18799。
保护运行配置和密钥，不要提交 Git。

## Token 代理和测试命令

复用原有代理命令，配置 JSON 包含 `memory_backend: "memos"`、`llm_base_url`、
`embedding_base_url`、`request_timeout_seconds`。代理默认监听宿主回环地址，
容器需使用可达的网络安排，例如 Linux host 网络；本 PR 不增加 `--host` 参数。

```bash
# 在 memory_bench_platform 目录执行；代理单独保持运行。
python -m memory_bench_platform.cli token-proxy \
  --config /absolute/proxy.json \
  --audit-dir /absolute/dedicated/token-audit --port 8080
```

在另一个终端设置配置后运行。Agent 和 Judge 直接访问模型上游，不经过记忆代理。

```bash
export MEMOS_LOCAL_RUNTIME_DIR=/absolute/dedicated/runtime
export OPENCLAW_TRANSPORT=http
export OPENCLAW_CONFIG_PATH="$MEMOS_LOCAL_RUNTIME_DIR/openclaw.json"
export OPENCLAW_STATE_DIR="$MEMOS_LOCAL_RUNTIME_DIR/state"
export OPENCLAW_GATEWAY_URL=http://127.0.0.1:<gateway-port>
# 私密设置 OPENCLAW_GATEWAY_TOKEN、LOCOMO_API_KEY、LOCOMO_BASE_URL、LOCOMO_METRIC_MODEL。
export MEMORY_BENCH_MEMORY_TOKEN_SOURCE=model_proxy
export MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE=1
export MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR=/absolute/dedicated/token-audit
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend memos \
  --memory-integration agent_plugin --data-path /absolute/locomo-small.json \
  --qa-concurrency 1 --judge-concurrency 4 --run-id unique-run
```

每次运行使用新的独占 runtime；只支持单 scope、串行 QA，不自动重试失败 QA。
报告位于 `runs/<run-id>/reports/`：`summary.json`、`case_results.json`、
`token_summary.json`、`token_summary.md`。同时核对评分覆盖率和运行失败率，未评分不计答错。

## 输入、原生只读工具及审计

历史输入保持整段 session 和 `INGEST_OK` 回复。官方 2.0.8 不支持关闭历史阶段召回，
因此保留原生召回；这与部分其它系统历史阶段关闭召回的条件不同，报告须注明。
写入由官方 `agent_end` 捕获。QA 原生 `memory_add.enabled=false`，阶段切换重启
专用 Gateway 使配置生效；核验每题结束后记忆数量和写入水位不变。

此版本的 `memos_get` 模型可见内容可能仅为 `INGEST_OK`，历史正文位于
`details.meta.userText`。适配器在阶段边界使用原生 `GET /api/v1/traces/:id`，
仅导出已验证成功的历史 trace 正文到 `/runtime/qa-workspace/history/` 的只读文件。
保留官方存储时已有的截断，不补写原文；不安装辅助插件。

QA 只开放 `memos_search`、`memos_get`、`memos_timeline` 和原生 `read`，
启用 `tools.fs.workspaceOnly=true`。适配器同时设置默认 workspace 和 `main` Agent 的显式
workspace 为 `/runtime/qa-workspace`，避免 Agent 独立配置覆盖审计目录。
无事实的工具说明要求先搜索，再读取本题命中的
trace 文件。通过实际会话 JSONL 核对调用顺序、命中 ID、文件哈希和目录边界；
自动召回或前一道 QA 命中不构成本题文件读取授权。

目录隔离不是预防未命中读取的完整沙箱：成功读取未命中历史、缺失审计证据等情况
会在事后将本题标为无效，保留答案和 Token，不交给 Judge 算答错。
原生回执失败或超时仍属于运行失败；迟到召回不视为有效注入。任一必要控制台回执
缺失时，会在原有等待期限内回退检查同一请求窗口的原生日志文件。
Agent runner 抛出 HTTP/超时等异常时，公共执行器也会调用已声明的 `after_agent`
失败收尾，并保留原始异常及已经采集的 Token；收尾异常不会替换原始异常。
对 owner 和 pending 回执匹配的失败 QA，先保存失败证据，再重启专用只读 Gateway
终止迟到 hook；服务就绪且记忆快照、正文导出校验通过后，才释放 pending 继续下一题。
不重跑失败 QA，失败会话不能复用；重启或校验失败、记忆发生变化、回执身份不符时
保持阻塞，历史写入失败也不执行此恢复。报告须披露
“原生 read + Workload 正文导出/事后审计”、无效 QA 数量和评分覆盖率。

记忆 Token 使用现有代理；Agent Token 使用现有采集器并去重，保留工具循环和失败
尝试消耗。Judge 排除，Embedding 单列，cacheRead 已含于输入，不能再次累加。
正文导出不调用 LLM。记忆侧包含召回请求，不等于纯提取成本；报告 `partial`
表示观测小计，不能声称完整成本。官方插件和 MemOS 源码均不修改。
