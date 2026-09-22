# OpenClaw + oGMemory (`agent_plugin`)

直接加载原生 `og-memory-context-engine`，需要包含 `autoCapture` / `autoRecall`
的 oGMemory 版本（例如提交 a44d92e9）。不再包装或替换引擎方法。

## 阶段控制

| 阶段 | autoCapture | autoRecall |
|---|---|---|
| ingest | true | false |
| QA | false | true |
| 初始化 / finalize | false | false |

- 历史通过原生 `afterTurn` 自动写入，QA 通过 `assemble/compose` 召回。
- 基准测试使用 HTTP `/v1/responses`：回答/写入指令放 `instructions`，用户内容放 `input`。
  指令不再拼入检索文本。带基准指令的任务不支持 CLI `--message` 回退。
- `run_lifecycle.py` 更新专用配置、重启专用 Docker Gateway 并等待就绪，再记录已加载的配置哈希。
  需要在同一 Docker 主机上运行 Workload；不支持共享 Gateway 或并发使用同一 runtime。
- HTTP 会话 ID 从 Gateway 的 `sessions.json` 读取，不使用 CLI 的哈希值代替。
- 每个 run + sample 使用独立 account/user/agent，同时设置匹配的 `authAccountId`。
  历史与 QA 使用相同记忆身份，每个问题使用独立 OpenClaw session。
- 原生插件不再注入 `wait=true`。历史输入结束后先等待后台空闲，再核对实际 session 的
  message_count / commit_count；已归档历史还必须等待 outbox 清空。缺失/空会话、索引错误
  和超时不能当作成功。
- 未归档历史记录 `history_archived=false, reason=buffered_without_archive`，不强制 compact。
  这通常是输入未达到后端阈值；仅凭此结果不能区分所有抽取失败，需结合后端日志。
  idle / commit_count 也不能证明抽取内容的质量。
- 不修改后端 `memory.after_turn_threshold`（测试可配置为 200）。本模式不主动 compact；
  Agent 运行期间若检测到原生 hook 错误，则拒绝把该次调用作为有效结果。
  超长输入仍可能触发 OpenClaw 原生 compact，不能把这类运行标成纯 afterTurn 对比。
  正在抽取时的 `after_turn` 503 processing 交给后续 readiness 检查处理。
- `autoCapture=false` 不是全局只读：显式 compact、session_end 的已有缓冲清理仍是原生行为。
  工具禁用、工具结果外置关闭、trace 内容采集关闭；测试后端应关闭 skill sedimentation。
  QA 前完成历史就绪检查，不复用历史 OpenClaw session。

## 准备独立运行配置

在 `memory_bench_platform` 目录执行；原配置只读取，目标目录必须不存在。
生成配置包含模型密钥，权限为 0600。插件原样复制到专用目录。

```bash
python skills/memory_plugins/openclaw-ogmemory/scripts/run_lifecycle.py \
  --source-config "$HOME/.openclaw/openclaw.json" \
  --upstream-plugin /path/to/oGMemory/openclaw_context_engine_plugin \
  --destination /path/to/dedicated-ogmem-runtime \
  --api-url http://127.0.0.1:8090 \
  --openclaw-runtime-dir /runtime \
  --gateway-port 18789

export OPENCLAW_CONFIG_PATH=/path/to/dedicated-ogmem-runtime/openclaw.json
export OPENCLAW_STATE_DIR=/path/to/dedicated-ogmem-runtime/state
export OGMEM_PLUGIN_STATE_FILE=/path/to/dedicated-ogmem-runtime/phase.json
export OPENCLAW_TRANSPORT=http
export OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
export OGMEM_GATEWAY_CONTAINER=benchmark-openclaw-ogmemory
export OPENCLAW_GATEWAY_TOKEN="$(python -c 'import json,os; print(json.load(open(os.environ["OPENCLAW_CONFIG_PATH"]))["gateway"]["auth"]["token"])')"
unset OG_AUTH_ACCOUNT_ID MEMORY_BENCH_AGENT_LOCAL
```

如需 API Key，生成配置前设置 `OGMEM_API_KEY`，Workload 保留相同值；后端授权应允许
按测试 account 隔离。生命周期脚本会覆盖专用配置中的身份和开关，不依赖开关环境变量。
后端地址必须同时能被 Workload 和 OpenClaw 访问。脚本不启动或修改后端。

启动支持原生 context engine 的 OpenClaw 镜像，例如：

```bash
# OPENCLAW_IMAGE 设为实际安装的 OpenClaw 镜像。
docker run -d --name "$OGMEM_GATEWAY_CONTAINER" --network host \
  --label memory-bench.runtime=/path/to/dedicated-ogmem-runtime \
  --user "$(id -u):$(id -g)" \
  -e HOME=/runtime -e OPENCLAW_CONFIG_PATH=/runtime/openclaw.json \
  -e OPENCLAW_STATE_DIR=/runtime/state \
  -v /path/to/dedicated-ogmem-runtime:/runtime \
  --entrypoint openclaw "$OPENCLAW_IMAGE" gateway run
```

路径替换为同一个绝对路径；标签和挂载用于确认该容器属于此专用 runtime。
`OGMEM_PLUGIN_STATE_FILE` 和 `OPENCLAW_STATE_DIR` 使用 Workload 所在主机可见路径。
Gateway 仅绑定 loopback，并使用准备命令生成的 token；配置自动重载和心跳关闭，
由生命周期在阶段边界明确重启。此 HTTP 模式不需要 `OPENCLAW_BIN` 包装脚本。
容器用户 UID 应与专用目录所有者一致，否则 OpenClaw 可能拒绝插件。不得指向用户日常配置。

## 运行

```bash
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend ogmemory \
  --memory-integration agent_plugin \
  --data-path /path/to/locomo_small.json --run-id ogmem-plugin-example
```

LongMemEval 用 `--benchmark longmemeval --data-path /path/to/longmemeval.json`。
评分分别使用 `LOCOMO_*` / `LONGMEMEVAL_*` 的 API_KEY、BASE_URL、METRIC_MODEL。
Agent 模型来自专用 OpenClaw 配置，抽取模型和阈值来自后端；judge 配置不替换它们。
数据集公共 ingest / QA 提示词保持不变。`backend_direct` 不受此适配修改影响。

## 日志与清理

`phase.json.events.jsonl` 记录生命周期、身份和后端就绪检查，不再伪装为原生 hook 回执。
`provenance.json` 记录插件代码和 schema 哈希。Workload 归档输入、回答和 readiness 结果；
后端日志用于确认模型抽取错误及内容质量。finalize 关闭两个开关并归档生命周期日志。
插件通过可选的 `before_agent` / `after_agent` 生命周期操作检查每次 Agent 调用。
调用前验证阶段、身份和配置哈希；调用后检查本次请求期间的 Gateway 日志及真实会话 ID，
发现原生插件 hook 错误（包括 `compose returned null`）则拒绝把调用计为成功。
QA 还必须在本次调用时间窗口内找到连续的原生 `assemble return` 和会话身份日志，
核对实际 session ID 及预期 Gateway session key；缺失或不匹配时判为运行失败。
成功但召回为空仍然有效，不要求 `ws_injected=true`。此校验依赖上述原生日志格式及
专用 Gateway 串行运行约束；不支持该日志格式的插件版本会拒绝通过校验。
公共 OpenClaw runner 不读取 oGMemory 配置。
删除本次专用 runtime/容器和隔离测试数据，不修改共享后端或用户已有配置。
