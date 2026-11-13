# Hindsight 自部署 backend_direct

此适配器使用官方 Hindsight HTTP API，复用 Workload 的场景生成、就绪轮询、回答/Judge、Token 和报告机制。无需修改 Hindsight 源码。当前不含 agent_plugin 适配，不使用 reflect 代替统一回答模型。

## 生命周期与隔离

```text
历史 session 1 → 原生异步 retain → 等待 retain 和 bank 后台任务完成
历史 session 2 → 原生异步 retain → 等待 retain 和 bank 后台任务完成
……
原生 recall → Workload 回答模型 → Judge → 报告
```

`scope_id` 的哈希作为 bank_id；`scope_id + event_id` 的哈希作为 document_id。不同运行使用不同 scope，禁止其他写入者复用实验 bank。正文不增加提示词、不自行切块。已有 `occurred_at`/`timestamp` 映射到 Hindsight timestamp；无时区历史采用固定 UTC 约定，显式无效时间拒绝提交。

`retain` 已触发抽取，无需 flush/commit。就绪检查先验证本次 operation，再分页检查 bank 操作：pending/processing 继续等待，failed/cancelled 判运行失败。采用保守策略，cancelled consolidation 即使可能被后续任务替代也不静默当成功。原生状态和结果保留在步骤输出。无自动写入重试，不能将网络超时视为未写入然后直接重发。

## 配置

| 环境变量 | 默认值/含义 |
|---|---|
| `HINDSIGHT_API_URL` | `http://127.0.0.1:8888`，服务根地址 |
| `HINDSIGHT_API_KEY` | 可选，Bearer 鉴权；所有请求禁止自动重定向 |
| `HINDSIGHT_RUNTIME_VERSION` | 实际官方版本，写入运行证据 |
| `HINDSIGHT_RECALL_BUDGET` | `mid`，支持 low/mid/high |
| `HINDSIGHT_RECALL_MAX_TOKENS` | `4096`，原生召回事实 Token 预算 |

召回结果不调用 reflect，不重排、不额外扩展。原生 max_tokens 与结果条数不同；Workload 的 node_limit 在原生结果上按既有顺序截取（当前标准 QA 为 10）。完整原生返回保留于 backend_result，供审计。空 results 是有效零召回，缺失/损坏 results 是运行失败。

## 启动服务

固定官方 release 镜像版本，不使用滚动 latest。示例使用官方 1.2.1：

```bash
docker run -d --name workload-hindsight --shm-size=1g --stop-timeout 75 \
  -p 127.0.0.1:8888:8888 \
  --env-file ./hindsight.env \
  -v workload-hindsight-data:/home/hindsight/.pg0 \
  ghcr.io/vectorize-io/hindsight:1.2.1
```

`hindsight.env` 使用官方环境变量配置提取模型和 embedding。设置稳定的 `HINDSIGHT_API_WORKER_ID`。若需要记忆 Token 报告，将 **所有** 后端 LLM 配置（包括 retain/consolidation 的独立覆盖项）指向同一个 Workload Token 代理；Agent/Judge 不能经过这个记忆侧代理。

```dotenv
HINDSIGHT_API_LLM_PROVIDER=openai
HINDSIGHT_API_LLM_MODEL=YOUR_MODEL
HINDSIGHT_API_LLM_BASE_URL=http://host.docker.internal:8787/v1
HINDSIGHT_API_LLM_API_KEY=YOUR_LLM_KEY
HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai
HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL=YOUR_EMBEDDING_MODEL
HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL=http://host.docker.internal:8787/v1
HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY=YOUR_EMBEDDING_KEY
HINDSIGHT_API_WORKER_ID=workload-hindsight
```

Linux bridge 网络需要为容器添加 `--add-host=host.docker.internal:host-gateway`，并让代理监听容器可达地址；不要直接把容器内 127.0.0.1 当作宿主代理。自部署鉴权遵循官方配置；适配器的 HINDSIGHT_API_KEY 是调用服务的凭据，和后端调用模型的 LLM_API_KEY 不同。

## 运行

以下命令在仓库的 `memory_bench_platform` 目录执行。先按已有 OpenClaw 文档配置纯回答 Agent（禁用记忆插件），以及 LoCoMo Judge 模型。DATA_PATH 指向 locomo-small JSON；每轮使用新的 RUN_ID。

```bash
export HINDSIGHT_API_URL=http://127.0.0.1:8888
export HINDSIGHT_RUNTIME_VERSION=1.2.1
export MEMORY_BENCH_MEMORY_TOKEN_SOURCE=model_proxy
export MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE=1
export MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR=/absolute/path/hindsight-token-audit-$RUN_ID

python -m memory_bench_platform.cli token-proxy \
  --config /absolute/path/token-proxy.json \
  --audit-dir "$MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR" --port 8787
# 上面代理在独立终端运行。确认服务和 Agent 已就绪后执行：
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw \
  --memory-backend hindsight --memory-integration backend_direct \
  --data-path "$DATA_PATH" --run-id "$RUN_ID" \
  --qa-concurrency 1 --judge-concurrency 4
python -m memory_bench_platform.cli analyze-run --run-dir "./runs/$RUN_ID"
```

每次运行（包括冒烟测试）必须使用新的代理进程和独立审计目录；不能复用上一轮已认领的审计目录。所有服务后台请求结束后再结束统计窗口。

代理配置沿用通用格式：`memory_backend: hindsight`、`llm_base_url`、`embedding_base_url`、`request_timeout_seconds`。凭据由后端请求携带，不写入仓库。

## 统计口径

记忆侧包括通过代理的抽取、consolidation 及其他后台 LLM 请求；不额外叠加 retain 响应中的 usage。回答侧沿用框架采集，Judge 不并入被测系统 Token。cache read 是输入用量子集，不重复相加；上游未返回用量时保留未知状态，不估算成 0。

阶段耗时来自现有 ingest/status/recall/Agent 步骤；status 等待含后台处理，不能把其全部解释为 HTTP 调用时间或某一个内部算法耗时。本适配不新增内部 profiling 探针。

## 本次验证

2026-11-12 使用官方 `hindsight-api-slim==1.2.1` 自部署包（aarch64，官方 Docker 镜像下载失败后采用官方 Python 包），PostgreSQL 16 + pgvector，原生 hs-rerank-1 ONNX 模型。未修改上游源码。LLM 为 deepseek-flash，embedding 为 BAAI/bge-m3；先通过一段历史/一道 QA，再运行 locomo-small（4 段历史、35 道评分问题）。

- 29/35 正确（82.86%），评分覆盖率 100%，运行失败率 0%。
- 记忆 LLM：输入 72,949，输出 22,096，总计 95,045；cache read 37,248 已包含在输入中。
- Agent QA：输入 142,885，输出 2,989，总计 145,874；cache read 116,480 已包含在输入中。
- Embedding 单独计量 5,466 tokens；Judge 排除。框架 call_path_coverage 保持 partial，不声称完整系统成本。
- 实验脚本误复用冒烟审计目录，自动认领被拒绝。依据原始代理日志、冒烟结束字节偏移及正式运行起始时间离线补入 66 个唯一请求（12 LLM、54 embedding），无未完成请求；保留原诊断和恢复证据，未重新调用模型或修改答案/评分。
- 完整测试后仅将召回证据间的字面量换行转义改为实际空行，并通过单元测试及真实只读 recall 验证；上述分数来自修改分隔符前的完整测试，未重评。

59 项适配与既有编排/执行测试通过。结果目录：`memory_bench_platform/runs/hindsight-direct-small-20261112-small/`。
