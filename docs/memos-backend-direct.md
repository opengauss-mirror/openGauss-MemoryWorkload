# MemOS 自部署版 backend_direct

本适配器使用 MemTensor/MemOS 的 product API，不修改 MemOS 源码，不支持云 API 或 agent_plugin。

## 服务要求

使用独立 MemOS 实例和独立存储，启用原生 scheduler。同步 `/product/add` 完成提取和落库后仍可能提交后台整理，适配器会查询 `/product/scheduler/allstatus`，等待 waiting/in_progress/pending 全部为零；失败、取消或状态不可观测均报运行失败。该状态接口是实例级，因此不能与其他负载共享实例。HTTP 写入没有自动重试；会话 ID 用于来源关联，不代表服务提供幂等保证。

使用官方 Docker 部署所需的 Neo4j、Qdrant 和模型配置。固定源码 commit/镜像摘要，记录原生配置。不要复用别人的数据库。官方参考：https://github.com/MemTensor/MemOS/tree/main/docker

## Workload 配置

从仓库的 memory_bench_platform 目录执行：

```bash
export MEMOS_API_URL=http://127.0.0.1:8000
export MEMOS_EXCLUSIVE_INSTANCE=1
export MEMOS_RUNTIME_VERSION=<实际源码commit或版本>
# 如服务启用鉴权，通过环境设置 MEMOS_API_KEY；发送 Authorization: Token ...
export MEMORY_BENCH_MEMORY_TOKEN_SOURCE=model_proxy
export MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE=1
export MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR=/absolute/path/token-audit

python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw \
  --memory-backend memos --memory-integration backend_direct \
  --data-path /absolute/path/locomo-small.json \
  --run-id memos-small-unique-id \
  --qa-concurrency 4 --judge-concurrency 4 \
  --profiling-config /absolute/path/profiling.yaml
```

OpenClaw 和 Judge 沿用现有部署配置；上述命令不自动创建服务或启动 Token 代理。

## Token 代理

复用 `python -m memory_bench_platform.cli token-proxy --config proxy.json --audit-dir ... --port ...`。
配置包含 memory_backend=memos、llm_base_url、embedding_base_url、request_timeout_seconds。将 MemOS 的主 LLM、reader、general、preference、feedback、scheduler 等原生模型配置的 base URL 统一指向此代理，Embedding 也指向代理；密钥由原生客户端传递，不能写进仓库。

代理默认只监听 127.0.0.1，Docker 服务须使用可达的网络方式（本机测试使用 host 网络）。Agent/Judge 不经过记忆侧代理。LLM input 已包含 cache_read 时不得重复加总；Embedding 单列；Judge 排除。未返回 usage 的请求仍标记未知。不存在另一个 MemOS Token 计数器。

## 语义和限制

- 每个 scope_id 派生独立 user_id/mem_cube_id；写入/检索限制到同一 Cube。
- 每个历史 session 原文封装为一条 user message，保留说话人和日期，不重新划分输入。
- 同步写入使用原生默认 fine 提取；检索使用 fast、context_format=memory、偏好记忆启用且最多6条，工具/技能记忆关闭。文本 top_k 默认20，可由框架 node_limit/top_k 或 MEMOS_TOP_K 指定（1–1000）；额外偏好条数不算在文本 top_k 内。
- 正常空数组及原生可空结果有效；结果列表的对象、布尔值、空字符串等非法类型报运行失败，不能转换为空召回。错误响应、未知结构、未确认完成不能当空结果。
- 原始服务返回保留在 backend_result。HTTP 等待默认850秒，框架步骤上限900秒；MEMOS_HTTP_TIMEOUT_SECONDS 只允许 (0,850]。
- 性能复用现有 profiling 框架。首版采集客户端写入/召回阶段与配置的硬件指标，不声称已有内部阶段归因。MemOS 原生阶段日志可以另行保存，不能直接当作标准 trace。
- 输出沿用 reports/summary.json、token_summary.json/md 和 profiling 报告。没有成功评分的题目作为运行失败或未评分，不计成答错。

## 官方评测参考

接口和返回格式参考 OmniMemEval 的 scripts/client_factory/memos_client.py；Workload 保留自己的数据、回答提示词和 Judge，不能把结果声称为官方 benchmark 复现。

## 本轮验证（2026-10-16）

- Workload 基线：60e7b55；MemOS 官方源码 a7367d07e55db61099f7b4e2c1108bc5831a24f3，未修改源码。
- 官方 Python 依赖安装在独立镜像；构建时只更换 Debian/PyPI 下载镜像，不修改依赖版本。Neo4j 5.26.6 / Qdrant 1.18.2。
- MemOS 原生请求包含 dimensions=1024。SiliconFlow BGE-M3 对此返回400；初次尝试已停止保留。实际完成轮使用独立自部署 TEI 1.9.3 / BAAI/bge-m3（1024维、最大8192 tokens），支持该请求字段；MOS_EMBEDDER_TIMEOUT=60。
- 完成 run：memos-native-small-20261016-r2，4次历史写入、35次独立QA，21/35正确。一次 Judge 非JSON返回已仅补评现有答案，原始 reports/summary.json 保留 invalid，最终评分见 evaluation-recovery/effective-summary.json。
- 观测 Token：memory LLM 48,440；Agent QA 154,636；Embedding 6,000。CacheRead 分别为7,040、116,480，已包含于输入。统计报告仍标记覆盖范围 partial，不宣称完整系统成本。
- 项目阶段窗口：ingest 176.710秒；recall 45.629秒。ingest 的1 request表示一个包含4个写入步骤的setup case；recall包括检索与Agent回答，不能解释为纯服务端检索耗时。后台等待包含在ingest中，drain为not_applicable。
- 性能硬件采集成功；未接入内部trace，因此内部阶段归因 unavailable。API健康版本1.0.1只是接口版本，后端版本以源码commit和镜像摘要为准。

测试与独立部署材料保存在 `/home/taoying/memory_workload/memos-native-small-20261016`；完成后专用服务已停止，数据保留。


### 历史时间映射

`backend_direct` 写入时将通用输入 `occurred_at`（缺失或为 null 时回退到
`timestamp`）映射到 MemOS 原生 `messages[].chat_time`。支持 ISO 日期/时间和
LoCoMo session 时间（如 `7:55 pm on 9 June, 2023`），统一输出 ISO 格式；
日期值使用当日零点，已有时区保留，未指定时区时不补机器时区。
现有历史正文及日期前缀保持不变，仍按整段 session 单条 user 消息写入。
没有历史时间时省略 `chat_time`；显式空值字符串、非法日期或非字符串值在写入前
返回 `InvalidHistoricalTime`，不会用当前时间替代。这一映射仅适用于 MemOS
backend_direct，不改变官方源码、公共框架或 Local Plugin 的 agent_plugin 路径。
