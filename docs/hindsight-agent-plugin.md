# Hindsight 官方 OpenClaw 插件

使用 `--agent openclaw --memory-backend hindsight --memory-integration agent_plugin`。复用现有 Agent runner、组合生命周期、评分、Token 和报告；不修改官方插件或 Hindsight 服务源码，不通过 Workload 直接写入/召回替代插件。

## 已支持的运行边界

- 官方 `@vectorize-io/hindsight-openclaw@1.2.0`，专用串行 OpenClaw HTTP Gateway，main Agent；本轮 OpenClaw 2026.7.1-2、后端 Hindsight 1.2.1。
- `memoryMode=context`，原生自动召回，不开放主动 retain/reflect 或其他工具。
- 历史：整段 session 交给现有 Agent runner，回复 INGEST_OK；官方 agent_end 自动 retain，Workload 检查成功回执并等独立 bank 全部后台任务完成（包括 consolidation）。无需 sessions.compact。
- QA：新会话、同一 bank；autoRetain=false、autoRecall=true。正常零召回有效；超时、缺失/重复回执、后台失败均是运行失败。检查 QA 前后后台操作集合不新增，作为只读核验之一。
- 不手动改变历史正文或官方保留格式。插件默认将会话封装为 JSON，并带自身 retain context；和 backend_direct 纯正文输入不同。默认 recallTypes/预算/超时保持官方配置，不能把两种模式当成完全相同检索算法条件。
- 完成判定依赖启用官方 `debugPerfTiming`、独占串行日志窗口及原生 bank operations。没有通用结构化注入回执，因此这不是多租户并发适配。
- 失败后保留 pending 阻止后续误用，不自动重发写入/QA。需检查后台最终状态后进行明确恢复。

## 专用 Gateway 配置

复用已有部署框架或自行启动官方 OpenClaw，配置文件的相关部分如下。模型、认证和端口沿用现有 OpenClaw 配置。

```json
{
  "tools": {"deny": ["*"]},
  "plugins": {
    "enabled": true,
    "allow": ["hindsight-openclaw"],
    "load": {"paths": ["/official"]},
    "slots": {"memory": "hindsight-openclaw"},
    "entries": {
      "hindsight-openclaw": {
        "enabled": true,
        "hooks": {"allowConversationAccess": true, "allowPromptInjection": true},
        "config": {
          "hindsightApiUrl": "http://YOUR_HINDSIGHT_HOST:8888",
          "dynamicBankId": false,
          "bankId": "mbp-inactive",
          "autoRetain": false,
          "autoRecall": false,
          "memoryMode": "context",
          "retainEveryNTurns": 1,
          "debugPerfTiming": true,
          "logLevel": "info",
          "retainQueuePath": "/runtime/retain-queue.jsonl"
        }
      }
    }
  }
}
```

Gateway 挂载专用宿主目录到 `/runtime`、官方 npm 包及依赖只读挂载 `/official`，设置 `OPENCLAW_CONFIG_PATH=/runtime/openclaw.json`、`OPENCLAW_STATE_DIR=/runtime/state`。容器标签 `memory-bench.runtime` 必须等于宿主运行目录绝对路径。使用与宿主一致的 UID，禁止共享生产 Gateway。关闭自动配置 reload，由阶段切换显式重启专用 Gateway。Agent 使用专用 Gateway 的回环 HTTP 地址。host 网络使用 Gateway 配置端口，Docker 端口映射使用宿主发布端口。

运行目录包含 `openclaw.json`、初始 `phase.json`（`{"active": false}`）和 `provenance.json`。provenance 记录宿主官方包 `package_path`、`version` 及 `files` 相对路径到 SHA256 的映射，至少包含 package.json、openclaw.plugin.json 和 dist/*.js。保存这些文件时使用私有权限。

## 测试命令

在仓库 `memory_bench_platform` 目录运行。先按既有文档配置 OpenClaw HTTP 凭据及 LoCoMo Judge，Hindsight 服务模型请求经独立的通用 Token 代理（每轮新代理审计目录）。

```bash
export OPENCLAW_TRANSPORT=http
export OPENCLAW_AGENT_ID=main
export OPENCLAW_CONFIG_PATH=/absolute/runtime/openclaw.json
export OPENCLAW_STATE_DIR=/absolute/runtime/state
export OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
export HINDSIGHT_PLUGIN_RUNTIME=/absolute/runtime
export HINDSIGHT_GATEWAY_CONTAINER=your-exclusive-gateway
export MEMORY_BENCH_MEMORY_TOKEN_SOURCE=model_proxy
export MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE=1
export MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR=/absolute/new-audit-dir

python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw \
  --memory-backend hindsight --memory-integration agent_plugin \
  --data-path "$DATA_PATH" --run-id "$RUN_ID" \
  --qa-concurrency 1 --judge-concurrency 4
python -m memory_bench_platform.cli analyze-run --run-dir "runs/$RUN_ID"
```

记忆 LLM 通过代理统计，Agent ingest/QA 通过现有响应/会话采集并去重；Judge 不计入。缓存读取是输入组成部分，不另加总。没有新增部署框架和内部 profiling 探针。

## 实际验证（2026-11-12）

先完成 1 段历史＋1 道 QA（答对），再用新 bank 和独立 Token 代理审计目录运行 locomo-small。正式测试使用最终适配器代码，期间没有修改。

- 官方插件 1.2.0 发布包 31 个文件校验无修改；后端 1.2.1 官方 Python 包。宿主复用 `openclaw-openviking:2026.7.1-2` 镜像中的 OpenClaw，配置仅启用 Hindsight，未运行 OpenViking 插件或后端。
- deepseek-flash / BAAI/bge-m3，QA 并发 1、Judge 并发 4。
- 4 段历史成功写入并等后台完成；35 道 QA 独立会话，共 39 个唯一实际 Gateway session。
- 32/35 正确（91.43%），评分覆盖率 100%，运行失败率 0%。
- 35 道 QA 的原生召回完成日志均先于回答模型请求；QA 未新增记忆任务。
- 记忆 LLM：输入 81,343，输出 19,388，合计 100,731，cache read 51,072。
- Agent ingest：输入 18,496，输出 42，合计 18,538，cache read 13,696。
- Agent QA：输入 256,533，输出 3,824，合计 260,357，cache read 123,264。
- Embedding 单列 6,148 tokens；cache read 已包含在输入中。66 个记忆侧代理请求（15 LLM、51 embedding）全部完成，使用原框架自动生成报告，无离线补录。框架覆盖标记保持 partial，以上为已观测用量。
- 73 项适配及编排/执行回归测试通过。

结果目录：`memory_bench_platform/runs/hindsight-plugin-small-20261112-small/`，含准确率、Token、HTML 报告及核验 JSON。
