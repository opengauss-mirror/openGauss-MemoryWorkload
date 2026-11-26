# 跨样本并发

原生 `cli run` / `cli plan` 支持 `--sample-concurrency N`，默认 1。
支持 `backend_direct` 和配置了独立 OpenClaw 运行实例的 `agent_plugin`。
使用统一部署的 OpenClaw 插件测试 LoCoMo 或 LongMemEval 时，在已有的
`combinations[].execution` 中设置 `sample_concurrency: 2`，再照常运行
`memory-bench experiment --config deployment.yaml`。部署最多启动两个独立的 OpenClaw Gateway。实际 Gateway 数量取并发数和
conversation 数量的较小值。省略该字段时仍使用一个 Gateway。

每个 Gateway 同时执行一个完整 conversation。完成 QA、评分和插件状态恢复后，
空闲 Gateway 才领取下一样本。Hindsight 为每个样本绑定独立 bank。OpenViking 的托管模式要求服务支持按用户指定身份，并通过 `/health` 确认实际账号和用户；身份无法确认时在写入前报错。oGMemory 的 Gateway 共享后端地址。串行和并发运行都以 `OGMEM_ACCOUNT_ID`（默认 `memory-bench`）为前缀，为每个样本生成独立账号、用户和 Agent。后端凭据须允许访问这些账号。
恢复失败会记录错误，该 Gateway 不再领取新样本。需要采集独占后端计数器时，
在确认服务仅供本轮使用后设置 `MEMORY_BENCH_OGMEMORY_EXCLUSIVE=1`，
记忆侧用量按整个 run 采集一次。

- JiuwenMemory 的 Gateway 共享官方 MCP 服务，每个样本的写入和搜索携带一致的独立 user 和 session。
- Mem0 OSS 的 SDK 在 Gateway 内运行，可共享 PGVector 存储。每个样本使用独立的 `userId`，每个 Gateway 有自己的插件状态和本地历史库。原生召回超时仍可能触发无记忆回答。
- MemOS Local 的记忆库位于 Gateway 内。每个 Gateway 使用独立的数据目录、SQLite 和 Viewer 端口；接手同一轮的下一样本时，必须先完成上一样本、确认没有未结束的请求，再清理本地历史并验证库为空。

## 运行

在已有服务、模型和 Token 采集配置的基础上：

```bash
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend openviking \
  --memory-integration backend_direct --data-path /path/to/locomo.json \
  --sample-concurrency 2 --qa-concurrency 2 --judge-concurrency 2
```

LongMemEval 使用同一命令，将 benchmark 改为 `longmemeval`，
data-path 改为对应的原生数据集文件。此参数不配置或部署模型、Agent、记忆服务。

- LoCoMo 的一个 conversation（全部历史及 QA）是一个样本。
- LongMemEval 的一个问题及其历史集合是一个样本。
- 只有一个样本的 LoCoMo-small 不会因提高样本并发而加速。
- 也可在已有 execution_spec 中设置 `sample_concurrency`；CLI 显式参数优先。

## 顺序与隔离

每个活跃样本沿用 composer 的依赖链：每段 ingest 后，按记忆适配器能力
执行 flush 和 readiness；完成后才开始下一段。同步写入的适配器以成功返回
作为完成屏障，不额外调用不支持的 flush/status。检查点 QA 和 Judge 完成后，
才推进同一样本的下一个检查点。失败与超时的依赖、跳过或降级规则不变。

不同样本可以处于不同阶段。QA 和 Judge 使用全局共享线程池，
其上限分别是 qa_concurrency 和 judge_concurrency，不乘以样本并发数；
历史准备不占 QA 线程池。sample_concurrency 控制同时活跃的完整样本流程数，
不表示后端内部 LLM 或 Embedding 的请求上限。

并发前要求每个 case 有 sample_id，记忆步骤有显式 scope_id，
不同样本不能共享 scope；Agent session_key 必须唯一，
不支持跨样本依赖。实际后端仍须正确遵守适配器的 scope 隔离配置。
结果按原计划顺序汇总，每个 case 的进度独立落盘。

## 统计边界

沿用现有评分、Token 采集与离线重建。整个 run 只启停一次记忆 Token 采集器：
原生全局计数器或独占代理窗口不按样本重复采集、求和。
请求级证据可保留已有 case/step 关联；只有整轮统计的来源不能据此拆出样本用量。
Cache Read 保留现有归一化口径，不在 total 上重复相加。

独占代理要求本轮开始前没有在途请求，并使用未被其他 run 占用的审计目录。
不同样本属于同一 run，可以共享该独占代理。

步骤时间、Trace 和整轮耗时仍保留。阶段独占窗口 profiling 与
sample_concurrency > 1 不兼容，执行器会拒绝该组合；需要阶段独占分析时使用 1。
并发下写入与 QA 会共享后端资源，耗时应解释为并发负载下的结果，
不能直接当作串行延迟。

本功能不更改历史正文、模型、检索参数、评分规则或记忆系统源码。

OpenViking 并发运行需要 `OPENVIKING_SCOPE_USERS_FILE`。使用原生账号管理接口为每个样本预先创建独立用户，并将凭据保存在权限为 0600 的 JSON 文件中。键是计划里的完整 `scope_id`，值是 `{"user_id": "...", "api_key": "..."}`。每个 scope 使用不同的用户和用户密钥，同一样本的写入、提交、就绪查询和召回复用同一绑定。文件属于运行环境，不应纳入版本控制。使用新 run ID 时同时准备新的用户和绑定。

## OpenClaw agent_plugin

下列命令适用于手动部署的 Gateway。每个样本绑定一个已部署的独立 Gateway。统一部署的 oGMemory 示例使用本文开头的 YAML 配置。

准备私有 `sample-runtimes.json`（权限 0600，不提交到仓库）：

```json
{
  "conv-26": {
    "OPENCLAW_GATEWAY_URL": "http://127.0.0.1:19001",
    "OPENCLAW_GATEWAY_TOKEN": "当前实例密钥",
    "OPENCLAW_CONFIG_PATH": "/absolute/sample-a/openclaw.json",
    "OPENCLAW_STATE_DIR": "/absolute/sample-a/state",
    "OPENCLAW_BIN": "/absolute/sample-a/openclaw-host"
  },
  "conv-30": {
    "OPENCLAW_GATEWAY_URL": "http://127.0.0.1:19002",
    "OPENCLAW_GATEWAY_TOKEN": "当前实例密钥",
    "OPENCLAW_CONFIG_PATH": "/absolute/sample-b/openclaw.json",
    "OPENCLAW_STATE_DIR": "/absolute/sample-b/state",
    "OPENCLAW_BIN": "/absolute/sample-b/openclaw-host"
  }
}
```

键必须与数据集计划的 sample_id 完全对应。LongMemEval 使用 question_id。
每份环境还需包含所选组合原有的环境配置。oGMemory 插件的跨样本测试统一通过部署配置启动。
`OPENCLAW_BIN` 是指向当前实例的 CLI 包装脚本。服务共享时，必须配置不同的
原生账号/用户/记忆 scope；仅分开 Gateway 端口不能保证后端数据隔离。
OpenViking 在各实例原生配置中绑定独立账号/用户。

```bash
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend openviking \
  --memory-integration agent_plugin --data-path /absolute/two-samples.json \
  --sample-concurrency 2 --sample-runtime-config /absolute/sample-runtimes.json \
  --qa-concurrency 2 --judge-concurrency 4
```

其他支持手工绑定的插件更换 memory-backend 和实例配置。LongMemEval 更换 benchmark 与数据文件。
串行对照可保留独立实例绑定，将 sample-concurrency 和 qa-concurrency 均设为 1。
每段历史依次执行 Agent 接收、原生提交（若支持）、就绪检查（若支持），
然后才进入下一段。QA 在线程池中跨样本并发，全局上限为 qa-concurrency，默认每个样本的 Gateway 同时最多一题。Judge 使用已有共享线程池。
阶段切换、配置快照、pending、CLI 状态和 Token transcript 查询都使用当前样本绑定。
没有绑定且并发为 1 的原有入口保持可用。

运行时绑定与阶段独占 profiling 暂不能组合；步骤起止时间与整轮报告仍保留。
记忆 Token 仍只采集一次整轮独占窗口，Agent usage 从各自会话采集，
不会按样本重复累加整个后端计数器。绑定文件可能含凭据，应按私有运行配置管理。

所有实例在运行结束时分别执行原有 finalize，恢复各自配置；某个实例失败不会跳过
其他实例的收尾，汇总报告保留逐实例结果。oGMemory 的只读就绪查询若因另一样本
提取占用服务而触发单次读取超时，会在原有总 readiness 预算内重查，耗尽预算仍失败；
不重提 compact，不重复触发提取，也不重试配置错误或其它 HTTP 错误。

Adapters with externally assigned memory identities may provide
`scripts/sample_identity.py` in their skill. Before writing, Workload passes
each bound or parallel sample's scopes on stdin using that sample's adapter
environment. Return an `identities` array of non-secret native identity
strings. Shared identities or a failed preflight stop the multi-sample run.
The default unbound serial path does not invoke this check.

OpenClaw 插件模式支持通过 `--qa-per-sample` 配置同一样本内的只读 QA 并发，默认值为 1。
例如 `--sample-concurrency 10 --qa-per-sample 3 --qa-concurrency 30 --judge-concurrency 10`。
每题使用独立会话，同一 conversation 共用记忆范围，所有 conversation 共用全局 QA 上限。
mem0、MemOS Local、Hindsight 按题关联并发 QA 请求；不能按会话归属的 Gateway 日志不会算作已验证召回。
MemOS Local 仍审计本题 transcript 中的正文读取，Hindsight 仍检查 bank 操作。

### 插件历史不完整时的 QA

历史 ingest、commit 或就绪步骤失败时，保留该 session 的失败记录，跳过需要其失败输出的操作。下一个拥有独立 session key 和独立输入的历史段继续按计划写入、提交和等待就绪。到达检查点后尝试切换至只读 QA。
已确认只读的样本继续答题，结果单列为降级 QA。后台工作可能晚于客户端超时完成，超时不代表后端最终失败。

若准备步骤报告记忆运行失败，仍调度每道题。只读阶段无法确认，或插件检查失败时，Workload 使用该样本 OpenClaw 配置的回答模型和原始问题直接请求无记忆答案。支持 `openai-completions` 和 `openai-responses`。执行 Workload 的主机需要能访问配置中的模型地址并解析 API Key。报告的 `qa_paths.without_memory` 单列这条路径；各次模型请求独立记账。回答服务本身失败时记录该题执行失败，只有取得答案才评分。

在 backend_direct 模式下，省略 `--qa-per-sample` 时沿用全局 QA 并发；显式指定为 1 时，同一样本一次只执行一道 QA。

Mem0 和 Hindsight 的历史写入回调通过请求身份校验后，即使验证失败也不会一直占用写入租约。下一段开始前先检查旧回执；若旧回执尚未到达，仍可发送后续独立历史，但同一 Gateway 进程中重叠窗口的无请求 ID 日志不作为写入成功证据。Hindsight 仍执行原生 bank 就绪检查。

Mem0 和 Hindsight 在串行 QA 已返回、会话身份匹配且确认只读时，可通过重启本样本的只读 Gateway 隔离超时回执。当前题保留失败记录，恢复成功后下一题重新验证召回。Hindsight 还检查重启前后的 bank 操作集合及状态。

MemOS Local 在历史写入验证失败时保存失败回执。后续同阶段的独立历史只用本 session 的原生 SQLite 写入记录和 trace 核验写入，并检查 embedding 重试队列；无法归属的召回日志标为未验证。
