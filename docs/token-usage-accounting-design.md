# Workload Token 消耗统计设计与使用说明

实现核对：2026-10-08，基于 Workload `3d9955d` 与 `2926a7f` 两个实现提交。原始事件格式为 `token-usage/1`，
汇总格式为 `token-summary/2`。本文统一描述当前实现、使用方式和限制；不把后续设想列为已实现能力。

## 1. 目标与范围

在 Workload native 的 `backend_direct` 和 `agent_plugin` 流程中，分别统计记忆系统内部、
Agent 历史接收和 Agent QA 的已观测用量。Agent 历史接收是 Agent 阅读历史并返回确认的模型消耗，
不包含记忆后端的提取消耗。输入包含缓存，缓存分项不重复加总。

统计默认开启，不改变模型参数、输入、召回、评分、并发或重试策略，不修改记忆系统插件及后端源码。
统计异常写入诊断或 warning，不据此把业务步骤判为失败。缺失用量保持未知，不按文本长度估算。

**Judge 不采集、不计入合计。** Judge 仍正常评分；离线重建也会排除旧分片里的 Judge 记录。
外部 entrypoint 的旧导入报表没有统一迁移到本账目。独立执行、未进入采集上下文的请求也不自动纳入。

## 2. 成本分类与当前覆盖

| 分类 | 内容 | 模式差异 |
|---|---|---|
| `memory_llm` | 所选原生证据或模型代理已报告的记忆侧 LLM 用量 | 两种模式共用；覆盖取决于实际接入的来源 |
| `agent_ingest` | Agent 接收历史时的模型用量 | 插件模式采集；直接模式没有对应记录时为 `not_applicable` |
| `agent_qa` | Agent 回答问题时的模型用量 | 两种模式均采集 |
| `agent_other` | 无法归为 ingest/QA 的 Agent 用量 | 保留在系统合计，避免漏记 |
| `memory_embedding` | 记忆侧已观测 Embedding 用量 | 单列，不加入 LLM 合计 |

主指标：

```text
observed_memory_llm_tokens = 记忆侧已知 LLM 总量小计
observed_agent_ingest_tokens = Agent ingest 已知 LLM 总量小计
observed_agent_qa_tokens = Agent QA 已知 LLM 总量小计
observed_system_llm_tokens = 记忆侧已知 LLM 小计 + 全部 Agent 已知 LLM 小计
```

合计排除 `preflight` 和 Judge，不把不同模型的 Token 数量解释为金额。
完全没有已知总量时返回 `null`；部分来源未知时可以展示已知小计，但不是完整成本。
`complete_system_llm_tokens` 和 `system_tokens_per_correct` 当前固定为 `null`。

| 来源 | 已实现的采集方式 | 尚未覆盖或需要满足的条件 |
|---|---|---|
| OpenClaw Agent | 优先读取会话 JSONL 中本次新增 assistant usage，响应 usage 作为回退 | 隐藏子 Agent、SDK 内部重试及未归档请求可能不可见 |
| 其他 Agent | 从原始响应读取 OpenAI 兼容 usage | 视作一次 Agent attempt 的汇总，不假设只发生一次模型调用 |
| OpenViking | 读取流程原本已查询的明确 task 的 `result.token_usage.llm/embedding` | 主要覆盖历史任务；召回内部用量没有统一采集，不为统计额外查询任务或发起检索 |
| oGMemory | 显式启用的独占实例 run 窗口计数器差值 | 默认不查询；必须声明独占实例，不能拆成逐 QA 实测消耗 |
| 通用模型请求采集 | 所有记忆系统可配置同一 OpenAI 兼容模型代理，逐请求读取 usage；后端名称仅作运行身份绑定 | 只覆盖经过代理的 LLM/Embedding；需单轮独占、端点可配置；不是任意协议或托管黑盒的自动适配 |
| mem0 OSS 内部 | 使用上述通用采集器，无 mem0 专用代理或 CLI 分支 | Python 后端与官方 Node 插件分别通过原生端点配置接入 |

当前没有任意协议的通用模型代理、价格换算、文本 Token 估算、逐题摊销或延迟分位数成本报表。
不能将三套系统覆盖范围不同的小计直接作为完整成本排名。

## 3. 采集数据流与代码职责

```text
Agent 响应 / 会话新增消息 ─┐
OpenViking 任务结果        ├→ 字段归一化 → 原始分片 → 去重及来源选择 → 明细与报告
oGMemory 独占窗口差值     ┤
通用模型代理 usage       ┘
```

路径均相对于仓库根目录：

| 位置 | 当前职责 |
|---|---|
| [token_usage.py](../memory_bench_platform/memory_bench_platform/token_usage.py) | 采集、归一化、原始落盘、去重、覆盖率和报告重建 |
| [cli.py](../memory_bench_platform/memory_bench_platform/cli.py) | 统一调用采集生命周期；`token-summary` / `token-proxy` 命令，无 mem0 分支 |
| [token_collectors.py](../memory_bench_platform/memory_bench_platform/token_collectors.py) | `Collector.start/observe/finish` 接口、原生采集器注册、按上下文选择记忆侧来源 |
| [token_proxy.py](../memory_bench_platform/memory_bench_platform/token_proxy.py) | 通用 `token-proxy` 服务；透传模型请求，仅保留数值 usage 与请求身份 |
| [token_proxy_audit.py](../memory_bench_platform/memory_bench_platform/token_proxy_audit.py) | 通用独占窗口、后端身份核验、请求归账和重复导入检查 |
| [workflow.py](../memory_bench_platform/memory_bench_platform/workflow.py) | 为每个 step attempt 设置统计上下文 |
| [workflow_operators.py](../memory_bench_platform/memory_bench_platform/workflow_operators.py) | 包装 Agent 调用，接收记忆 adapter 返回的任务用量 |
| [reporter.py](../memory_bench_platform/memory_bench_platform/reporter.py) | 将 Token 报告和摘要接入整体测试报告 |
| [OpenViking direct adapter](../memory_bench_platform/skills/memories/openviking/scripts/run_operation.py)、[plugin adapter](../memory_bench_platform/skills/memory_plugins/openclaw-openviking/scripts/run_lifecycle.py) | 透传原生 `task_result` 及状态，不增加业务模型调用 |
| [test_token_usage.py](../memory_bench_platform/tests/test_token_usage.py) | 字段映射、去重、失败记录、计数器、并发及重建验证 |
| [test_token_proxy.py](../memory_bench_platform/tests/test_token_proxy.py) | 真实 HTTP 透传、流式快照、截断、并发、缓存、窗口隔离与去重 |
| [test_token_collectors.py](../memory_bench_platform/tests/test_token_collectors.py) | 采集器生命周期、扩展注册、来源互斥、故障隔离及 Agent 边界 |

### 3.0 通用采集器与来源选择

模型响应、task 聚合、实例计数器都是用量证据；统计核心统一负责归一化、去重和报表。
记忆侧采集通过 `token_collectors.start()`、`observe()`、`finish()` 接入执行流程，
不在 CLI 或工作流中分支调用 mem0 等系统的专用统计脚本。Agent 用量仍由独立的
`capture_agent()` 在 Agent 调用边界采集。一个记忆侧采集器可实现其中任意钩子：

- OpenViking 原生采集器在 `observe` 读取操作返回的 task usage。
- oGMemory 原生采集器在 `start/finish` 读取独占窗口计数器。
- 通用模型代理采集器在 `start/finish` 导入逐请求 usage，适用于任何可配置兼容端点的后端。
- 新的原生证据来源通过 `register_native(backend, Collector(...))` 注册，不改 CLI 或工作流。
  采集器只报告原始 usage 和来源身份，不自行求系统合计或按题摊销。

`MEMORY_BENCH_MEMORY_TOKEN_SOURCE` 选择记忆侧来源；应在启动前设置，并在整轮运行期间保持不变：

| 值 | 行为 |
|---|---|
| `native`（默认） | 使用注册的原生采集器；mem0 没有原生 usage reader 时内部用量为未知 |
| `model_proxy` | 对任意后端使用同一通用代理采集器；不启动原生计数器，不累计原生 task usage |
| `off` | 不采集记忆侧用量，Agent 统计仍开启 |

启动采集器时，来源选择写入运行上下文；每个步骤创建统计上下文时也会读取当时的环境变量。
当前实现不会阻止运行中途修改该变量，因此整轮来源一致依赖运行者保持配置不变。
记账入口按各记录上下文检查互斥关系，非所选来源即使提交记录，也只保留为
`selected=false` 的证据，不能进入总量；报表给出 `excluded_memory_source_observations`。
代理缺失、配置错误或超时不会自动回退并混入原生计数，以免部分重叠再次计费。
未知配置值产生统计诊断，不中止业务测试。`MEMORY_BENCH_TOKEN_USAGE=0` 仍关闭全部 Token 统计。

### 3.1 Agent：增量读取，响应回退

`capture_agent()` 在调用前记录会话文件的 inode 和字节游标；调用后从原始 artifacts、
session ID、session key 对应的 `sessions.json` 等证据定位会话，只读取新增的 assistant 消息。
相同文件路径先归一化，避免显式路径、索引和 artifact 重复指向同一文件。
文件被替换或截短时记录诊断，不把整份旧会话重新累计。

会话记录以文件路径与消息 ID 标识；缺消息 ID 时用字节位置。新增 assistant 消息即使没有 usage，
也保留未知记录。一旦本次读到新增 assistant 消息，就选用会话来源，不再把响应汇总加进合计；
缺失的会话 usage 不会由重叠响应逐条补齐。响应保留为未选中的证据，并记录 `superseded_by`。
没有新增会话消息时才采用响应 usage。

每次 Agent attempt 有独立 ID，调用前写 `started` 记录，结束记录使用更高 revision。
采集在 `finally` 中执行，Agent 失败或后续 `after_agent` 校验失败也不删除已观测用量。
记录中的 `outcome` 表示对应调用结果，不等于整个步骤、评分或整轮测试成功。

### 3.2 OpenViking：精确 task，而非轮询次数

采集器读取 adapter 返回的 `task_id`、`task_result` 和状态，将 LLM 与 Embedding 分成两条记录。
记录 ID 使用 `task_id + model_kind`，阶段为 `ingest`，粒度为 `task`。
重复查询同一 task 产生的快照不会相加；失败任务已返回的用量也会保留。
任务没有返回用量时标为缺失，不能据此认定实际消耗为零。

### 3.3 oGMemory：独占实例计数器窗口

native 执行前和插件 finalize 后分别读取 `/api/v1/token_stats`。
每次读取前后用 Docker 检查容器 ID、启动时间、运行状态，确认读取期间没有切换实例。
有效窗口按字段做差，记录为 `memory / unknown`，粒度为 `run`，不强行归到 ingest 或具体 QA。

- 容器重启、负差值、快照缺失会使窗口失效，用量记录为未知，不能把负数裁剪成零。
- 某字段只有开始和结束两次快照都提供时才可做差；开始时未初始化的计数器不能假设为零。
- `calls` 保留在原始差值中，不能把一个 run 聚合记录视作一次模型请求。
- 同代容器内计数器若重置后又增长超过旧值，仅靠两个快照无法识别。
- 独占性及 API 与容器的映射由运行者保证；代码不能发现所有共享请求。
- 不额外等待后台任务或调用 compact 补统计。运行者需确保开始前空闲、结束时相关异步工作完成。
- 每次 Docker 命令和 HTTP 读取的超时为 3 秒。采集失败只影响统计。

### 3.4 通用模型入口与独占窗口

mem0 的 SDK 通常只把内容、工具调用结果或向量返回给调用者，业务结果不等于模型 usage。
Python LLM 响应回调不能统一覆盖 Embedding 和官方 Node 插件，因此不修改 SDK 或插件源码，
而是在测试侧配置通用的 OpenAI 兼容代理，mem0 只是使用方之一。支持 `/v1/chat/completions`、`/v1/responses`、
`/v1/embeddings`，分别转发至配置的 LLM/Embedding 上游；鉴权和 JSON 请求体原样转发。
没有新增重试、参数覆盖、并发限流或分词估算。代理不保存提示词、答案、向量及请求鉴权头。

每次实际 HTTP 请求分配独立 ID，先记 `started`、结束记 `finished`。SDK 重试若实际经过代理，
会成为另一条请求，不能因为 provider response ID 相同就合并。SSE 只采用最终累计 usage，
不把多个 usage 快照相加；流没有正常结束、传输截断或没有 usage 时保留未知。成功返回明确
零值才记零。统计写入失败不改变正常上游响应；网络转发失败仍是模型请求失败。

代理配置声明 `memory_backend`，导入前必须与 Workload 本轮后端一致，不能把另一套系统的
日志归入当前账目。没有后端白名单；新后端无需修改代理。Workload 开始前记录审计文件身份
和游标，finalize 后导入窗口内开始的请求。开始时代理必须空闲；
`run-owner.json` 以独占创建方式将一个审计目录绑定到唯一 run，不能被另一轮或并行 run 复用。
崩溃后也保留占用和原证据，新一轮应新建代理和审计目录。跨 generation、文件替换/截短、重复
事件、窗口外请求完成会记录统计诊断。结束时未完成的请求保留未知，不等待或重复业务调用。

导入记录为 `component=memory`、`phase=unknown`、`operation=unknown`、`granularity=request`；
LLM 与 Embedding 单列，无法可靠归因到某道 QA 时不按时间猜测。按代理实例和请求 ID 去重，
重复导入同一证据不会重复收费。Agent、Judge 必须使用原来的独立模型入口，不经过该代理。
独占声明和路由配置由运行者保证；代理无法从相同协议的请求判断调用者是否确实属于指定后端。

## 4. 字段归一化与缓存口径

记录保留 `raw_usage`、`reported_total_tokens`、`normalization_profile`、`cache_semantics`，
以及归一化后的输入、输出、总量、CacheRead、CacheWrite 和 reasoning 字段。
只接受非负整数；缺失或无效字段为 `null`，明确返回的 0 才是零。不使用本地分词器补齐。

| profile | 输入及缓存处理 |
|---|---|
| `openai-compatible-v1` | 读取 input/prompt、output/completion；若两套完整别名冲突，优先使用与服务报告的 total 一致的一组并记录 `conflicting_usage_aliases`；无唯一匹配时沿用 input/output 优先且保留不一致诊断。缓存是输入子集，不再相加，支持嵌套与平铺缓存字段 |
| `openclaw-v1` | 完整输入为 `input + cacheRead + cacheWrite`；三项缺任一项则完整输入未知 |
| `openclaw-responses-v1` | 在 input、output、total 都存在且 `total >= input + output` 时用 `total - output` 恢复完整输入；缓存读写明细未知 |
| `ogmemory-counter-v1` | 输入、输出采用后端差值；`cache_read/cache_write` 只作为分项 |
| `anthropic-v1` | 归一化函数支持将缓存读写加到基础输入；当前没有专用采集器自动接入此 profile，缺少可选缓存字段时不增加输入 |

未知 profile 不生成猜测的归一化数值。上述 OpenClaw 映射针对已验证的部署格式，其他版本需重新核对。

```text
LLM 总 Token = 完整输入 Token + 输出 Token
Embedding 总 Token = 输入 Token（没有输入字段时可使用服务明确报告的总量）
```

CacheRead、CacheWrite 已计入完整输入，reasoning 已属于输出，均不能再次加到总量。
原始总数与归一化总数冲突时产生 `reported_total_mismatch` 诊断。
`availability=complete` 仅表示该记录总 Token 可计算，不表示所有缓存字段已知或所有调用路径已覆盖。

## 5. 去重、落盘与失败处理

原始事件按进程/线程写独立 JSONL 分片，避免并发写同一文件。记录带运行上下文、来源、
逻辑记录 ID、观察事件 ID和时间戳；能取得时保留 case、step、attempt、session、task、模型及证据位置。
不会仅凭时间相近把后端请求分配给某一道 QA。

重建以 `(run_id, record_id)` 为逻辑键：

1. 更高 revision 替换同一次 Agent attempt 的开始记录。
2. 普通请求/消息不重复累计；同身份数值冲突记录 `source_conflict`。
3. task/run 聚合采用后续快照；后续快照完全缺失时保留此前可用快照并记录诊断，不把多个累计快照相加。
4. `selected=false` 的重叠响应不进入合计。
5. 旧 Judge 记录在重建时排除，原始分片保持原样。

重建使用原始 usage 重新计算派生字段，写入明细和报告时采用临时文件加原子替换。
损坏分片行记录诊断并跳过，其他有效记录仍可汇总。进程异常终止后，已写入且无结束证据的
attempt 保留为未知。分片不是断电事务日志，磁盘不可写或未落盘的调用无法保证恢复。

所有小计包含已观测的失败尝试；未返回 usage 的实际消耗仍未知。SDK 隐藏重试和迟到后台请求
可能超出采集范围，因此不能声称已捕获全部失败成本。

## 6. 覆盖率与报告含义

`cost_categories` 与按 component、phase、model_kind、model、source 分组的 `breakdown`
均提供逐字段小计和 `field_coverage`：

- `observed_*`：该字段在已采集记录中的已知小计；没有已知值时为 `null`。
- 同名无前缀字段：只有该组全部已采集记录都提供该字段时才有值，否则为 `null`。
- `field_coverage`：`known`、`missing`、`fraction`，统计对象是所选记录。
- `request_usage_coverage`：只对粒度为 `request` 的记录计算；task、run、Agent 响应聚合不冒充单请求。
- `source_coverage`：说明 Agent、OV task、OG 独占窗口等是否被观测；通用 `memory_model_proxy` 有代理请求记录时为 `partial`，否则为 `unobserved`。即使用量字段齐全，也不宣称所有内部路径都已覆盖。旧 `mem0_model_proxy` 记录仍可离线重建，不重复执行请求。

例如，两条记录只有一条提供 CacheRead=0：已知缓存小计为 0，字段覆盖率为 1/2，完整缓存字段为
`null`。不能显示成“全部请求缓存消耗为零”。Markdown 展示已知小计，JSON 提供完整字段覆盖信息。

这些覆盖率只描述已采集证据。即使字段覆盖率为 100%，也不证明外部后端全部调用都已采集。
当前不生成独立的任务覆盖率、归因覆盖率或全系统完整请求数。

## 7. 使用和输出

原有 `memory-bench run ...` 默认采集，无需额外参数。临时关闭：

```bash
MEMORY_BENCH_TOKEN_USAGE=0 memory-bench run ...
```

`false`、`off` 也可关闭。新建 run 关闭采集后不会新增统计事件；已有分片不会因此被删除。

| run 内相对路径 | 内容 |
|---|---|
| `records/token-usage/context.json` | 本轮模式、后端及运行身份 |
| `records/token-usage/shards/*.jsonl` | 原始事件、重复快照、未选中来源和诊断 |
| `records/token-usage/model-proxy-start.json`、`model-proxy-window.json`、`model-proxy-window.jsonl` | 通用模型代理独占窗口身份、范围与数值请求证据；启用后才有 |
| `records/token_usage.jsonl` | 归一化、去重后选入合计的记录 |
| `records/token_usage_diagnostics.json` | 采集失败、字段冲突、文件损坏等诊断 |
| `reports/token_summary.json` | 成本分类、模型/来源分组、覆盖率及限制 |
| `reports/token_summary.md` | 可阅读的已观测用量表 |
| `reports/summary.json` | `resource_summary.token_usage` 保存摘要及报告引用 |

离线重建：

```bash
memory-bench token-summary --run-dir /absolute/path/to/run
```

该命令只读取已采集分片，不调用模型、不重新评分、不修改原测试状态，也不重新读取后端计数器。
没有分片的旧 run 无法凭最终答案还原历史消耗，会生成未知用量报告。
正常报告生成会更新 `summary.json` 的引用摘要；单独离线重建以 `token_summary.json` 为准。

### oGMemory 独占计数器开关

```bash
export MEMORY_BENCH_OGMEMORY_EXCLUSIVE=1
export MEMORY_BENCH_OGMEMORY_CONTAINER=my-dedicated-ogmemory
export OGMEM_API_URL=http://127.0.0.1:8090
memory-bench run ...
```

地址和容器名必须替换为本轮实际专用实例，不可指向有其他业务流量的共享服务。
这三个变量只控制用量观测，不设置提取阈值、捕获开关或后台处理方式。
oGMemory 的原生 ingest/compact 配置见 [插件运行说明](../memory_bench_platform/skills/memory_plugins/openclaw-ogmemory/SKILL.md)。

### 通用代理用量开关（以 mem0 OSS 为例）

此功能需要明确配置，不能仅打开统计开关就观测到已经部署的任意 mem0 服务内部请求。
首先创建代理配置，URL 必须是模型 API 的 base URL，不是 mem0 服务自身的 URL：

```json
{
  "memory_backend": "mem0",
  "llm_base_url": "https://your-llm-provider.example/v1",
  "embedding_base_url": "https://your-embedding-provider.example/v1",
  "request_timeout_seconds": 900
}
```

代理配置不存 API key；各 SDK 继续使用各自的原密钥。上游 URL 不接受内嵌用户名、密码、
query 或 fragment。启动代理（前台命令，需单独终端或进程管理器）：

```bash
memory-bench token-proxy \
  --config /private/mem0-model-proxy.json \
  --audit-dir /experiments/new-run/mem0-model-audit \
  --port 46661
```

审计目录必须尚不存在，代理会创建私有目录、manifest 和 `requests.jsonl`。代理仅监听
`127.0.0.1`；容器中的 SDK 必须能访问宿主此端口，例如使用 host 网络，不能把普通容器内的
`127.0.0.1` 误当宿主。部署连通性应先预检，预检请求在 Workload 开始游标之前，不计入本轮。

然后仅修改本轮专用 mem0 实例的两个原生配置字段，保留 provider、model、api key 等其他配置：

| 接入路径 | LLM base URL | Embedding base URL |
|---|---|---|
| Python OSS 后端 | `llm.config.openai_base_url` | `embedder.config.openai_base_url` |
| 官方 OpenClaw mem0 插件，OSS 模式 | `plugins.entries.openclaw-mem0.config.oss.llm.config.baseURL` | `plugins.entries.openclaw-mem0.config.oss.embedder.config.baseURL` |

二者都指向 `http://127.0.0.1:46661/v1`（假设 SDK 可访问宿主回环地址）。Python 后端按自己的
原生配置加载方式重启，插件按原生 Gateway 配置加载；Workload 不热改运行中的共享服务。
这些字段按 Python SDK 2.0.14、官方插件 1.2.0 / Node SDK 3.0.7 核对。

最后为原有 Workload run 命令增加环境变量：

```bash
export MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE=1
export MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR=/experiments/new-run/mem0-model-audit
export MEMORY_BENCH_MEMORY_TOKEN_SOURCE=model_proxy
memory-bench run ... --memory-backend mem0 --memory-integration backend_direct
# agent_plugin 使用原有插件运行命令，只切换 memory-integration；同样设置上面两个变量。
```

示意中的 `...` 是已有 benchmark/Agent 参数，并非完整可复制的 run 命令。一次代理仅供一个 run，
两种模式应各自新建。Agent、Judge 不改端点；关掉 `MEMORY_BENCH_TOKEN_USAGE` 会停止归账，
但不会自动停止独立代理进程。测试结束后由运行者停止专用代理。

OpenViking、oGMemory 或新后端使用相同命令与环境变量，只需将 `memory_backend` 设为该后端
ID，并通过后端自身配置把模型端点接入代理。`model_proxy` 与原生 task/counter 互斥，不能
同时相加。统一采集入口不代表已经为各后端自动改好配置；原生配置方法和连通性需单独核对。

旧 mem0 专用入口已移除，迁移关系如下：

- `mem0-token-proxy` → `token-proxy`，代理配置新增 `memory_backend`。
- `MEMORY_BENCH_MEM0_EXCLUSIVE` → `MEMORY_BENCH_TOKEN_PROXY_EXCLUSIVE`。
- `MEMORY_BENCH_MEM0_TOKEN_AUDIT_DIR` → `MEMORY_BENCH_TOKEN_PROXY_AUDIT_DIR`。
- 另显式选择 `MEMORY_BENCH_MEMORY_TOKEN_SOURCE=model_proxy`。旧审计目录不用于新 run；
  已归档的 `token-usage/1` 分片仍可通过 `token-summary` 重建。

网络边界：代理默认 900 秒 socket 超时，部署时应不小于已有客户端等待要求；请求与响应
各限制 64 MiB，只接受带唯一 Content-Length 的 JSON 请求。SSE 可透传，但只有正常结束且
服务返回 usage 才有数值。请求 Accept-Encoding 使用 identity；上游仍压缩响应时原样透传，
该请求 usage 标为未知。当前不代理 Anthropic/Gemini 原生协议，不覆盖托管 mem0 内部黑盒、
其他绕过端点的图模型/重排服务或窗口结束后的异步调用。`token-summary` 只重建已导入的分片，
不会再次读取外部代理日志或扩大运行窗口。

## 8. 验证记录与现存限制

通用采集框架重构验证（2026-09-30）：全量 **731 项测试通过**，其中 Token/采集器/OG
相关 120 项通过。同一个代理测试覆盖 mem0、OpenViking、oGMemory、自定义后端及两种
integration 模式；原生/代理来源互斥、错误配置不回退、跨后端身份不符拒绝归账均已验证。
这些通用 HTTP 测试不等于 OpenViking/oGMemory 的代理端到端基准测试。

真实 mem0 Python 2.0.14 和 Node 3.0.7 SDK 在通用入口下重新完成短历史 `add/search`：
Python 的 LLM 7845 Token、Embedding 52 Token；Node 的 LLM 7840 Token、Embedding 72 Token。
逐请求原始数值与报告一致、重复重建不增量；本轮服务未返回的缓存分项仍保留未知。
旧 mem0 专用采集分片在副本上用新代码重建后，总量与记录数不变，原目录未改。
证据：`/home/taoying/memory_workload/token-framework-sdk-probe-20260930-080625`，
包含 `validation.json`、`legacy-rebuild-check/validation.json` 和 `shutdown.json`。
这属于真实 SDK 小规模验证，未重新执行完整 LoCoMo 或 OpenClaw 插件生命周期。

实现测试覆盖字段映射、缓存与 reasoning 不重复加总、真实零与缺失、共享会话增量、重叠来源、
Judge 排除、OV 失败任务、计数器重置/重启、并发分片、损坏尾行、业务重试和 after_agent 失败。
同时检查统计启停不改变模型调用次数和执行结果。代码验证入口为 `tests/test_token_usage.py`。

2026-09-30 系统消耗版曾通过 705 项回归测试；并入 oGMemory 阶段配置后，Token 与 OG adapter
相关 94 项测试通过。这些是已有运行记录，不代表每次文档编辑都重新执行整套测试。

真实 OpenViking `agent_plugin` 的 `locomo-small` 验证包含 4 个历史 session、35 个独立 QA：
39 条唯一 Agent 消息与 4 个唯一任务对应 47 条所选记录（任务分别记录 LLM 和 Embedding），
39 份重叠响应汇总没有重复加入，原始字段对账和重复离线重建一致，源事件中没有 Judge 用量。
证据目录：`/home/taoying/memory_workload/openviking-plugin-small-tokens-v2-20260930-093659`。
该轮只能证明上述采集及对账，不能证明 OpenViking 召回内部成本已覆盖。

2026-09-30 mem0 增补验证：新增 HTTP/窗口测试 11 项通过，与原 Token 测试合计 48 项通过。
全套 716 项中首次 714 项通过；两项 production replay 子进程因测试环境未设置 PYTHONPATH
无法导入包，补齐该环境变量后这两项通过，未改动 replay 代码。

真实 SDK 使用相同的短历史，完成一次 `add` 和一次 `search`（不是完整 LoCoMo 准确率测试）：

| 原生 SDK | LLM 请求数 | LLM 输入 | LLM 输出 | CacheRead（已包含在输入中） | Embedding 请求数 / Token |
|---|---:|---:|---:|---:|---:|
| Python mem0ai 2.0.14 | 1 | 7777 | 68 | 7777 | 3 / 52 |
| 官方插件使用的 Node mem0ai 3.0.7 | 1 | 7772 | 56 | 未知（服务未返回） | 5 / 72 |

两套都写入、召回了 2 条记忆，逐请求归一化求和与报告一致，重复离线重建不增加 Token。
测试使用全新本地向量库，未修改 SDK/官方插件源码；Node SDK 哈希前后一致。没有启动完整
OpenClaw 插件生命周期，因此 SDK 验证不能替代插件端到端基准测试。用于 kimi-k2 的既有
参数策略代理单独部署在测试目录，保持 temperature=.6/top_p=.95/thinking disabled；
生产的用量代理本身不覆盖这些参数。

证据目录：

- Python 成功验证：`/home/taoying/memory_workload/mem0-token-sdk-probe-20260930-065007`。
- Node 成功验证：`/home/taoying/memory_workload/mem0-token-sdk-probe-20260930-065119`。
- 先前探针的 Python search 参数错误、Node 模块入口路径错误及其已发生用量保留在原目录，
  没有覆盖为成功，也没有合入后续新 run 的消费。新增探针代理均已停止。

当前限制汇总：

- mem0 未经过专用代理的内部请求、OpenViking 无 task 用量的召回路径、隐藏子 Agent 和未观测 SDK 重试没有完整覆盖。
- oGMemory 只提供独占 run 窗口；后端自身漏报 usage 或把缺失记为零时，Workload 无法补救。
- oGMemory 初始快照缺少 Embedding 计数器时，最终计数不能直接当作本轮差值。
- 跨独立恢复 run 的用量不会自动合并成完整实验账单；需要另行核对来源，不能重复导入。
- 当前不提供 Judge 用量、价格、完整成本排名或完整系统 Tokens/correct。

## 9. 参考来源

本实现沿用原设计调研的思路：从本地 `locomo-test` 参考会话 usage、OV task 和 OG 原生计数器；
从外部 benchmark 参考按来源分组、响应 usage 采集和原始证据保留。参考项目的能力不等于
Workload 已实现的能力；本文第 2～8 节及对应代码才是当前范围。

- 本地参考：`/home/taoying/code/locomo-test`，调研快照 `10b9983ad19e7a6954a8c46de48a6835b6e45302`。
- [OmniMemEval TokenTracker](https://github.com/MemTensor/OmniMemEval/blob/0b1ea8d28aa2d3e03ac4a6aee17b3006a131da7d/scripts/utils/token_tracker.py)。
- [mem0 memory-benchmarks schema](https://github.com/mem0ai/memory-benchmarks/blob/4b61c5d31b9c668a12b4f5e78064248a02c82d2b/benchmarks/common/schema.py)。
- [OmniMemEval 模型客户端](https://github.com/MemTensor/OmniMemEval/blob/main/scripts/utils/llm_client.py)：响应 usage 采集；其文本估算回退未引入 Workload。
- [mem0 官方 Python LLM](https://github.com/mem0ai/mem0/blob/main/mem0/llms/openai.py)、[Embedding](https://github.com/mem0ai/mem0/blob/main/mem0/embeddings/openai.py)：核对原生端点配置与 usage 返回边界。LLM 回调只覆盖该客户端，不等同于插件与 Embedding 的统一统计。
- [mem0 官方 benchmark 模型客户端](https://github.com/mem0ai/memory-benchmarks/blob/main/benchmarks/common/llm_client.py)：回答客户端与记忆系统内部请求的边界分开核对，不假设 benchmark 的回答 usage 覆盖后端成本。
- [ProsusAI MemEval token tracker](https://github.com/ProsusAI/MemEval/blob/807ae6d7d8a5b76f6fe964d5a581d96c036e2ac4/src/agents_memory/token_tracker.py)。
