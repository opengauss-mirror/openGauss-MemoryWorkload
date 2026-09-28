# OpenClaw + 官方 Mem0 插件（`agent_plugin`）

加载未经修改的 npm 包 `@mem0/openclaw-mem0@1.2.0`（依赖 `mem0ai@3.0.7`）。
使用官方 `open-source` 模式和 TypeScript SDK，直接连接向量库、LLM 和 Embedding 服务。
这条路径不调用 `backend_direct` 的 Python OSS HTTP 服务，也不使用云端 V3。

## 职责和测试边界

- 公共框架负责数据集输入、历史日期、QA 提示词与评分；公共 OpenClaw runner 只提交 `/v1/responses`。
- 官方插件负责抽取、搜索、过滤和记忆注入。Workload 的 lifecycle 只管理配置、重启专用 Gateway、
  隔离身份和检查执行证据，不替插件调用 add/search，不更改官方抽取提示词。
- ingest：`autoCapture=true, autoRecall=false`。一段完整历史 session 作为一次 user 输入，
  官方 `agent_end` 捕获本次 user/assistant 消息（包括 `INGEST_OK`），按官方过滤规则抽取。
  历史日期保留在输入中；官方插件另会加入机器当前日期，适配层不改写这项原生行为。
- QA：`autoCapture=false, autoRecall=true`。官方 `before_prompt_build` 搜索并注入上下文。
  禁用所有工具、心跳和 OpenClaw 内置记忆搜索。QA 只读约束依赖此专用配置，不是 Mem0 的全局写入禁令。
- 每个 run/sample 派生独立 `userId`；同一样本共用记忆身份，每个历史 session、每道 QA 使用新 Agent session。
  目前仅支持专用 Docker Gateway、HTTP transport、main Agent、串行执行；禁止其他客户端同时使用该 Gateway。
- 阶段和样本变更后重启 Gateway，并确认官方注册日志中的身份和开关。尚有待确认请求时不能切换。
  finalize 关闭两个开关并归档检查记录，不删除存储中的测试数据。

## 原生完成证据的限制

官方 1.2.0 的捕获是异步的，返回 Agent 回答不代表写入完成。控制器等待
`auto-captured N memories`；QA 必须出现 `injecting N memories into context`。
检查窗口仅覆盖本次串行请求，同时核对实际 Gateway session handle，并记录 scope、phase、版本和日志来源。
这些原生日志没有 session ID，因此它不是插件提供的逐请求回执，不能用于共享或并发 Gateway。

官方对空抽取、空召回及部分跳过路径没有成功标记。此时适配器报告“无法验证”，使运行失败，
不会把故障计成答错，也不会把合法空结果伪称为后端故障。需要空结果也能正常完成的通用评测时，
应先由上游提供明确完成事件；当前适配不能覆盖这类情况。

官方 1.2.0 还会在成功召回后输出未取消计时器的超时警告。控制器区分“先成功、后警告”和
“先超时、后成功”，并在 QA 调用间等待 9 秒排空该计时器，避免上一请求的警告污染下一请求。
hook 错误、超时前未完成召回、缺少标记及同窗口多次完成均使运行失败。

## 准备环境

需要 Linux、Docker、宿主 Python 3、带 Node.js/npm 的 OpenClaw 镜像（实测 OpenClaw 2026.6.9、Node 24），
以及为本次测试准备的空向量库。先在独立目录安装并保留 `package-lock.json`：

```bash
export MEM0_OFFICIAL_DIR=/absolute/path/to/mem0-official-1.2.0
mkdir -p "$MEM0_OFFICIAL_DIR"
npm install --prefix "$MEM0_OFFICIAL_DIR" --omit=dev --save-exact @mem0/openclaw-mem0@1.2.0
```

安装时 Node 主版本、系统平台须与 Gateway 一致，特别是 `better-sqlite3` 原生依赖。
也可以在相同 Gateway 镜像内安装到挂载目录。插件目录需由运行 Gateway 的 UID 所有。

源 OpenClaw JSON 保留现有 `models`、`agents.defaults.model`，增加官方配置，例如：

```json
{
  "plugins": {
    "entries": {
      "openclaw-mem0": {
        "config": {
          "mode": "open-source",
          "oss": {
            "embedder": {"provider": "openai", "config": {
              "apiKey": "<embedding-key>", "baseURL": "https://embedding.example/v1",
              "model": "<embedding-model>", "embeddingDims": 1024
            }},
            "llm": {"provider": "openai", "config": {
              "apiKey": "<llm-key>", "baseURL": "https://llm.example/v1", "model": "<llm-model>"
            }},
            "vectorStore": {"provider": "pgvector", "config": {
              "host": "127.0.0.1", "port": 5432, "user": "<db-user>", "password": "<db-password>",
              "dbname": "<test-db>", "collectionName": "dedicated_test", "embeddingModelDims": 1024
            }},
            "historyDbPath": "/runtime/mem0-history.db"
          }
        }
      }
    }
  }
}
```

PGVector 用户需有本次测试数据库和 vector 扩展的创建权限，或提前创建好。
请替换实际地址、模型和维度。地址必须能从 Gateway 访问；本例使用 host 网络。
不要配置 `skills`：本适配验证官方自动 hook 路径，不使用 skills/triage 模式。
在项目的 `memory_bench_platform/` 目录执行：

```bash
export MEM0_PLUGIN_RUNTIME=/absolute/path/to/new-mem0-runtime
export MEM0_GATEWAY_CONTAINER=workload-openclaw-mem0
python skills/memory_plugins/openclaw-mem0/scripts/run_lifecycle.py \
  --source-config /absolute/path/to/source-openclaw.json \
  --upstream-plugin "$MEM0_OFFICIAL_DIR/node_modules/@mem0/openclaw-mem0" \
  --plugin-runtime-path /official/node_modules/@mem0/openclaw-mem0 \
  --destination "$MEM0_PLUGIN_RUNTIME" --openclaw-runtime-dir /runtime \
  --gateway-port 18789 --top-k 10 --timeout 300

export MEM0_PLUGIN_STATE_FILE="$MEM0_PLUGIN_RUNTIME/phase.json"
export OPENCLAW_STATE_DIR="$MEM0_PLUGIN_RUNTIME/state"
export OPENCLAW_TRANSPORT=http OPENCLAW_AGENT_ID=main
export OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
export OPENCLAW_GATEWAY_TOKEN="$(python -c 'import json,os; print(json.load(open(os.environ["MEM0_PLUGIN_RUNTIME"]+"/openclaw.json"))["gateway"]["auth"]["token"])')"

docker run -d --name "$MEM0_GATEWAY_CONTAINER" --network host \
  --label "memory-bench.runtime=$MEM0_PLUGIN_RUNTIME" --user "$(id -u):$(id -g)" \
  -e HOME=/runtime -e MEM0_TELEMETRY=false \
  -e OPENCLAW_CONFIG_PATH=/runtime/openclaw.json -e OPENCLAW_STATE_DIR=/runtime/state \
  -v "$MEM0_PLUGIN_RUNTIME:/runtime" -v "$MEM0_OFFICIAL_DIR:/official:ro" \
  --entrypoint openclaw "$OPENCLAW_IMAGE" gateway run
```

目标 runtime 必须不存在。生成文件权限为 0600，原配置不变，官方源码不复制、不修改。
`--timeout` 为等待捕获完成日志的时限（1–600 秒）；不是 SDK 的 HTTP 超时。
默认 top-k=10。准备后修改服务地址、模型或其他固定配置应重新生成 runtime。

## 运行和清理

配置数据集 judge（LoCoMo 的 `LOCOMO_API_KEY`、`LOCOMO_BASE_URL`、`LOCOMO_METRIC_MODEL`）后：

```bash
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend mem0 \
  --memory-integration agent_plugin --data-path /path/to/locomo_small.json \
  --run-id mem0-official-locomo-UNIQUE
```

数据集输入和提示词走公共框架，适配器没有 LoCoMo 专属分支；其他数据集仍需实测，尤其是空抽取/空召回场景。
检查记录在 runtime 的 `receipts/`，finalize 归档到 run 的 `artifacts/memory_plugin/openclaw-mem0/`。
另保留 `docker logs "$MEM0_GATEWAY_CONTAINER"` 和锁文件。测试结束移除专用容器及其独立数据，保留报告。
发生捕获超时后不要复用旧 runtime：停止 Gateway，再使用新的运行 ID 和空存储重测。

注意：实测默认 npm 依赖组合中的 Qdrant 客户端已移除 SDK 使用的 `search()`，会导致捕获失败。
当前示例选择官方支持的 PGVector；不宣称默认 Qdrant 组合已验证通过。

这测量的是官方 OpenClaw 插件路径，不等同于官方 Mem0 LoCoMo benchmark，也不等同于
`backend_direct`：SDK、输入捕获、原生过滤、日期与检索策略都有差别。跨模式比较需记录这些条件。

参考：[官方插件源码](https://github.com/mem0ai/mem0/tree/main/integrations/openclaw)、
[官方安装文档](https://docs.mem0.ai/integrations/openclaw)。
