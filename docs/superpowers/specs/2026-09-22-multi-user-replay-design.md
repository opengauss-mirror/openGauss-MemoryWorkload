# 多用户、多实例真实负载回放设计

状态：用户已确认设计及 OpenClaw 输入映射，进入实现。

## 目标与边界

用户通过 YAML 配置指定每个独立用户的 JSONL 负载及目标实例。支持多个用户共享一个 OpenClaw 实例、分配到多个 OpenClaw 实例，以及 backend_direct 下共享或分配到多个记忆系统实例。输出每用户、每实例和整轮性能数据。

沿用现有 add/search JSONL 数据作为默认输入，每用户显式指定 add_file 和 search_file，不再依赖文件名发现。保留现有单数据集命令和归档格式。

一个 run 选择一种执行模式：backend_direct 或 openclaw。两种模式的耗时语义不同，不在同一总计中混合。默认保留现有 add → drain → search 全局阶段屏障；本版不复刻原始请求间隔，不实现动态迁移用户或自动扩缩容。

## 现有代码约束

- production-http-replay 仅允许 backend_direct，调用 run_memory_task；传入 --agent openclaw 不会自动经过 Agent。
- OpenClaw runner 支持 HTTP /v1/responses、agent ID、session key 和请求关联 header，可复用其公开调用边界。
- 当前 OpenClaw/OpenViking 插件生命周期会修改实例级 autoCapture、autoRecall、agent_prefix。不能为每个用户并发执行现有 prepare/set_phase/finalize。
- 插件 userId 是实例配置；不同 session 或 X-Trace-User-ID 不能直接证明记忆用户隔离。共享实例的接入必须验证插件把用户/agent 身份带入写入和检索作用域。
- 当前记忆系统适配器的端点从环境变量读取。实例环境必须通过调用参数/子进程环境独立传入，不能在线程中切换全局 os.environ。
- 当前仓库内的 OpenViking Memory Skill 没有声明 openmem-v1。backend_direct 继续要求适配器真正支持原始请求协议，不通过修改声明绕过契约。

## 方案选择

采用单协调器管理用户、实例及阶段，共用两种执行适配器。

不采用每用户启动一个现有 benchmark 进程：这会使共享 OpenClaw 的全局插件配置相互覆盖，而且缺少统一时间窗口和实例统计。也不为两种模式分别实现调度器，以免超时、用户路由和指标算法分叉。

## 配置契约

增加 --workload-config，和旧 --data-path 互斥；路径相对 YAML 文件所在目录解析。两种模式共用 workloads、instances、users 结构。

OpenClaw 示例：

```yaml
schema: multi-user-replay/1
mode: openclaw
model_mode: real-model

defaults:
  add_rate: 1
  search_rate: 2
  request_timeout_seconds: 120
  drain_timeout_seconds: 600

workloads:
  support:
    format: openmem-agent/1
    add_file: data/support/add.jsonl
    search_file: data/support/search.jsonl
  developer:
    format: openmem-agent/1
    add_file: data/developer/add.jsonl
    search_file: data/developer/search.jsonl

instances:
  claw-a:
    kind: openclaw
    deployment: external
    environment_from:
      OPENCLAW_GATEWAY_URL: BENCH_CLAW_A_URL
      OPENCLAW_GATEWAY_TOKEN: BENCH_CLAW_A_TOKEN
    max_inflight: 8
  claw-b:
    kind: openclaw
    deployment: external
    environment_from:
      OPENCLAW_GATEWAY_URL: BENCH_CLAW_B_URL
      OPENCLAW_GATEWAY_TOKEN: BENCH_CLAW_B_TOKEN
    max_inflight: 8

users:
  - id: user-a
    workload: support
    instance: claw-a
    agent_id: bench-user-a
  - id: user-b
    workload: developer
    instance: claw-a
    agent_id: bench-user-b
  - id: user-c
    workload: support
    instance: claw-b
    agent_id: bench-user-c
```

mode=openclaw 要求 CLI 选择 --agent openclaw，并走真实 Agent 入口。此配置模式决定回放链路；不能因为存在 --memory-backend 而静默降级为直连。

backend_direct 使用相同的 users 映射，workload.format 改为 openmem-v1，实例 kind=memory。实例声明 memory_skill，并通过 environment_from 为该适配器绑定端点和凭据。例如 OpenViking 的端点变量为 OPENVIKING_API_URL；实际能否使用原始 OpenMem JSONL 取决于该 Skill 的协议能力。该模式不要求 user.agent_id。

users[].id 和实例 ID 使用非敏感测试别名，要求唯一。每个用户可覆盖默认速率和请求超时。用户固定绑定一个实例，避免同一用户状态被分散到不同实例。

## 启动一个或多个实例

deployment=external：连接已启动的实例，验证健康状态，不停止外部服务。

deployment=managed：配置提供 start_command（argv 数组）、healthcheck_command（argv 数组）、startup_timeout_seconds 和实例专属环境。协调器每实例启动一次，健康检查通过后放行流量，在 finally 中停止本轮启动的进程组。命令不经 shell 解释。

OpenClaw managed 实例要求不同的 gateway 端口、配置路径和 state directory；配置模板应事先定义对应测试 agents 和记忆插件路由。记忆系统 managed 实例也需要独立端口和数据路径。该接口支持用户自己的启动脚本，不假设所有记忆系统都有同一 Docker 镜像或启动参数。

预检重复端口、state/config/data 路径和重复实例引用。实例启动失败时，关联用户显示 setup_failed，不能漏出报告。运行结束保留数据与产物，不自动清空用户数据库。

## 身份与调度

1. 从 run_id 和用户别名派生匿名运行身份。多用户模式以配置用户为身份权威，明确替换请求 user_id；同一个文件分配给不同用户时也获得不同身份。
2. request_id、idempotency_key 包含 run/user/操作/源行标识，独立于实例分配。同一轮重试稳定，跨用户或跨轮不冲突。
3. session 由 run/user/conversation 派生。源 conversation_id 缺失时按用户使用独立历史会话；检索阶段使用新的会话，避免直接从 Agent 历史上下文回答而绕过记忆检索。
4. 用户间并发，同一会话内保持输入顺序。使用有界调度与每实例 max_inflight，防止一次提交整个 JSONL 或积累无界 future。
5. 每用户拥有独立节流器。调度器公平取下一条可执行请求，慢用户不占用其他实例的全部执行槽。
6. 所有用户 add 完成后进入 drain，再统一进入 search。单请求失败不停止其他用户，失败用户保留自己的 partially_written 状态。
7. OpenClaw 插件 prepare/set_phase/finalize 按实例执行一次，实例快照保存到独立路径。禁止请求期间以全局插件 userId 切换当前用户。

## 两种执行适配器

### backend_direct

完整 OpenMem 请求通过 Memory Skill ingest/status/recall 调用，保持原始字段，除配置明确声明的运行身份替换。每个调用传递所属实例环境和用户作用域。继续执行原始请求协议、异步任务 ID、超时与完成状态校验。

### OpenClaw

OpenClaw /v1/responses 接收对话输入，并没有与 OpenMem add/search 完全等价的接口。因此用显式 openmem-agent/1 转换协议：

- add：将源 messages 作为历史内容交给该用户的 Agent 会话，由记忆插件完成捕获和写入；记录 Agent 接收耗时，并单独记录写入完成/drain 耗时。
- search：把源 query 作为该用户新会话中的问题，经 OpenClaw Agent 和记忆插件检索得到回答。
- 原响应不发送。源角色和正文按已定义的历史内容渲染规则保留，但这不再声称是 OpenMem HTTP 请求原样回放。
- 过滤条件、memory_limit_number、租户扩展字段等需要适配器显式支持；无法表达的字段在预检中报 unsupported_field，不能静默丢弃。
- 同实例多用户默认绑定不同测试 agent 和独立 session。还必须通过插件能力与隔离 probe 验证记忆读写作用域；仅有不同 session 不算通过。
- 对于只能使用全局固定 userId、且无法证明独立记忆作用域的插件，同实例多用户配置在流量开始前失败，并指出需要扩展的插件能力。不能通过串行改全局身份冒充独立用户并发。
- 回答非空仅说明 Agent 有输出，不代表命中记忆。检索命中数、记忆检索延迟、用户隔离指标只从插件/内部 trace 的真实证据获得，缺失时报告 unavailable。

这项 OpenClaw 输入映射需要在实现前确认：使用现有 add/search JSONL 做历史写入与检索问答，还是直接提供原生 OpenClaw 对话请求 JSONL。本文推荐前者作为兼容现有数据的第一版，但必须保留其转换语义。

## 每用户性能数据

每条事件包含匿名 user_id、instance_id、request_id、操作、输入行号、payload hash/大小、排队耗时、调用耗时、完成状态、错误类型及可用的 token/结果数量。原身份、请求正文、回答正文和凭据不归档。

输出：

- request_events.jsonl：逐请求事件，在执行期间增量落盘。
- user_summary.json/csv：每用户 add/search 请求数、成功数、成功率、实际 QPS、p50/p95/p99/max、超时数、字节数、可用 token/结果数量、数据集和归因状态。
- instance_summary.json/csv：每实例所承载用户、请求量、实际 QPS、错误及延迟分布。
- summary.json 和 HTML：整体概览及可按用户、实例、操作查看的明细。
- run_config.json：脱敏配置快照、输入文件指纹、用户到负载/实例的映射、版本和运行模式。

延迟百分位从对应原始事件计算，不能平均各用户百分位。每用户同时报告实际活跃时间窗口；每实例和整轮吞吐使用对应共同测量窗口，不能直接累加各用户 QPS。

OpenClaw 的 Agent 端到端延迟与 backend_direct 的记忆调用延迟使用不同指标标签。setup、健康检查和 drain 以独立阶段展示。报告保留缺失打点和不可验证的用户隔离状态。

## 失败处理与兼容

配置预检覆盖未知字段、重复用户、缺失 workload/instance、负载文件格式、非有限速率/超时、模式冲突及插件能力。全部预检完成后再启动和施加负载。

用户失败、实例失败、trace 失败分开记录。可选 trace/资源采集失败不销毁客户端统计。中断时停止发起新请求、有界 drain、保存现有事件和部分汇总，最后清理本轮管理的进程。

旧 --data-path 单用户模式保持兼容。新多用户报告使用显式 schema 版本，导入器验证所有用户/实例/指标及关联关系，沿用内容白名单。

## 实现落点与验收

配置解析和用户/实例协调器放在独立模块，避免把现有 run_replay.py 继续扩成所有模式的入口。复用 JSONL 解析、请求事件、超时和汇总基础。为 Agent/Memory 调用增加显式实例环境参数；OpenClaw lifecycle 状态按实例分区。

验收覆盖：

1. 三用户共享一个实例，用户身份和会话互不覆盖，得到三份用户指标。
2. 三用户分配到两个实例，请求命中正确端点，两个实例同时执行。
3. 相同 JSONL 复用于两用户、不同 run，请求/幂等/trace 不冲突。
4. 共享 OpenClaw 时生命周期只按实例调用，用户 session 与记忆作用域隔离；能力不足时明确失败。
5. managed 启动成功、健康检查失败、部分实例启动失败和中断清理。
6. 单用户慢请求、超时或坏响应不阻塞其他用户；有界并发和阶段屏障保持正确。
7. 百分位/QPS、分用户失败计数、空检索、trace 缺失和嵌套隐私字段校验。
8. 使用可控假 HTTP 服务/子进程完成端到端测试，再对用户提供的真实实例运行小规模 probe；模拟结果不作为真实性能结论。
