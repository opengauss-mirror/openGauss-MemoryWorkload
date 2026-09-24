# 多用户、多实例真实负载回放

通过 YAML 将测试用户固定绑定到 JSONL 负载和实例。多个用户可共用一个实例，也可分配到多个实例；每用户串行、用户间并发，实例有独立并发上限。旧 `--data-path` 用法不变。

## 运行

```bash
memory-bench validate --benchmark production-http-replay --agent none \
  --workload-config workloads.yaml
memory-bench run --benchmark production-http-replay --agent none \
  --workload-config workloads.yaml --run-id multi-example
memory-bench analyze-run --run-dir runs/multi-example
```

OpenClaw 使用 `--agent openclaw` 和 YAML `mode: openclaw`。无需 `--memory-backend`；Memory Skill 或插件按实例配置。若显式传入 `--memory-integration`，必须与 mode 一致（分别为 backend_direct、agent_plugin）。`--workload-config` 与 `--data-path` 互斥。

`plan-run`/`validate` 只检查文件、环境引用和能力，不启动进程、不连接服务。`run` 才执行健康检查与隔离探针。运行目录必须不存在，避免覆盖前一次结果。配置错误、实例/请求/恢复失败返回非零退出码；中断返回 130，并保存已完成事件及部分报告。

## backend_direct 配置

```yaml
schema: multi-user-replay/1
mode: backend_direct
model_mode: real-model
max_workers: 32
defaults:
  add_rate: 0
  search_rate: 2
  request_timeout_seconds: 120
  drain_timeout_seconds: 600
  poll_interval_seconds: 1
workloads:
  support:
    format: openmem-v1
    add_file: data/support/add.jsonl
    search_file: data/support/search.jsonl
  developer:
    format: openmem-v1
    add_file: data/developer/add.jsonl
    search_file: data/developer/search.jsonl
instances:
  memory-a:
    kind: memory
    memory_skill: your-openmem-compatible-skill
    deployment: external
    max_inflight: 8
    environment_from:
      YOUR_MEMORY_URL: BENCH_MEMORY_A_URL
      YOUR_MEMORY_API_KEY: BENCH_MEMORY_A_KEY
    healthcheck_command: [python3, scripts/check_memory.py]
  memory-b:
    kind: memory
    memory_skill: your-openmem-compatible-skill
    deployment: external
    max_inflight: 8
    environment_from:
      YOUR_MEMORY_URL: BENCH_MEMORY_B_URL
      YOUR_MEMORY_API_KEY: BENCH_MEMORY_B_KEY
    healthcheck_command: [python3, scripts/check_memory.py]
users:
  - {id: user-a, workload: support, instance: memory-a}
  - {id: user-b, workload: developer, instance: memory-a}
  - id: user-c
    workload: support
    instance: memory-b
    controls: {search_rate: 1}
```

这是接入模板，示例 Skill、环境变量和检查脚本需要替换为真实适配器。Memory Skill 必须真正实现 `openmem-v1` 原始请求协议，manifest 声明 `capabilities.raw_request_protocols: [openmem-v1]`，支持 ingest/status/recall。当前内置 OpenViking Skill 不支持该协议；不能只修改声明来绕过检查。每条 JSONL 为 `{"request": {...}, "response": {...}}`，response 可省略且不会发给目标服务。add/search 文件都必须非空。

配置用户是身份权威：替换源 `user_id`，匿名身份由 run_id 与用户别名派生；存在 conversation_id 时也派生独立会话。其他原始字段交给适配器，不丢弃过滤条件等业务语义。同文件分给多个用户、换 run_id 均得到不同身份和幂等键。后端必须实际执行用户隔离；报告不会把传递 scope_id 当作隔离已验证。

路径相对 YAML 所在目录；实例 cwd 也相对该目录。命令为 argv 数组，运行于实例 cwd，不经 shell。环境覆盖只进入该实例子进程；`environment_from` 的值是当前环境变量名称，不是凭据本身。所有环境引用在启动前解析。

## OpenClaw 配置差异

将 mode 改为 openclaw，workload format 改为 `openmem-agent/1`，每用户添加独立 agent_id。实例示例：

```yaml
instances:
  claw-a:
    kind: openclaw
    plugin_id: openclaw-openviking
    deployment: external
    max_inflight: 8
    environment:
      OPENCLAW_CONFIG_PATH: claw-a/config.json
      OPENCLAW_STATE_DIR: claw-a/state
    environment_from:
      OPENCLAW_GATEWAY_URL: BENCH_CLAW_A_URL
      OPENCLAW_GATEWAY_TOKEN: BENCH_CLAW_A_TOKEN
    healthcheck_command: [python3, scripts/check_claw.py]
    isolation_probe_command: [python3, scripts/check_memory_isolation.py]
users:
  - {id: user-a, workload: support, instance: claw-a, agent_id: bench-user-a}
  - {id: user-b, workload: developer, instance: claw-a, agent_id: bench-user-b}
```

历史消息保留 role/content，以明确标注的历史 JSON 内容作为 Agent 用户输入，由记忆插件捕获。search 的 query 作为问题发给 Agent，使用与历史会话不同的新 session，防止直接从聊天上下文作答。HTTP `/v1/responses` 链路不会降级为 backend_direct。

映射仅支持 user_id、conversation_id、add.messages（消息仅 role/content，role 为 user/assistant/system）及 search.query。filters、memory_limit_number 等未知字段会在预检时报错，不能静默丢弃。本模式不是 OpenMem HTTP 等价回放。

插件 prepare、ingest/qa 阶段切换、finalize 每实例各执行一次；命名空间包含 run_id 和实例 ID，同一实例的用户依靠不同 agent 的记忆作用域隔离。测试期间需独占实例配置，不能同时跑另一个 benchmark 或生产会话。配置必须明确 endpoint/config/state 路径；同实例请复用同一实例 ID，不要用别名重复配置。若提供 `OPENCLAW_GATEWAY_BASE_URL`，运行时也会将 `OPENCLAW_GATEWAY_URL` 设为同一地址，避免继承的父进程地址覆盖实例配置。

### 隔离探针契约

探针由接入者提供，收到 stdin JSON：

```json
{"schema":"multi-user-isolation-probe/1","run_id":"multi-example","instance_id":"claw-a","namespace":"replay-...","users":[{"user_id":"replay-...","agent_id":"bench-user-a"}]}
```

它必须实际验证对应 agent 的写入与检索作用域（例如写入用户专属哨兵并交叉检索），成功时退出 0，并输出：

```json
{"isolated":true,"identity_mode":"agent","run_id":"multi-example","agent_ids":["bench-user-a"],"write_scope_verified":true,"read_scope_verified":true}
```

agent_ids 必须恰好覆盖本实例配置用户。探针应使用输入中的实例 ID 和 namespace 验证作用域，也需针对可能连接同一租户的其他实例检查跨实例串读。退出 0、不同 session、不同请求头都不足以通过验证；不要用输出固定成功 JSON 的空脚本替代探针。探针在插件 prepare 后、实际负载前执行，使用同一实例环境，失败则该实例用户标记 setup_failed。其他实例继续运行。探针结果是接入者证据契约，不是平台自动实现的隔离验证；仅固定全局 userId 且无法证明 agent 作用域的插件不支持共享实例独立用户测试。

## 托管实例

将 deployment 改为 managed，增加：

```yaml
start_command: [python3, scripts/start_instance.py]
startup_timeout_seconds: 120
exclusive_resources:
  - tcp:127.0.0.1:18789
  - path:claw-a/config.json
  - path:claw-a/state
```

启动脚本必须前台运行，不得 daemonize、转交不受进程组控制的 Docker/远端服务。平台只停止自己创建的进程组，不停止 external 服务，不删除数据库。健康检查非零时重试，直到启动超时。启动失败和中断也执行清理。

必须准确声明监听地址与端口，以及配置、state、数据目录等独占资源；路径会正规化后检查重复，tcp 使用统一地址写法。平台无法从任意启动命令推断所有资源，也不能识别 DNS 别名指向同一外部服务。不同 OpenClaw 实例必须不同端口、config、state，事先创建测试 agents/插件路由。

插件恢复快照可能含凭据，只存放在权限 0700 的系统临时目录，不进入报告。finalize 成功后移除；失败时保留以便恢复，报告 `recovery_snapshot` 给出临时目录名，位于 Python `tempfile.gettempdir()` 下。恢复后由操作员清理，不要把快照提交或加入报告归档。

Memory 适配器的 runtime_context.run_dir 同样放在报告目录之外，并按实例/用户分区；正常退出后移除。适配器自行生成的原文调试文件不会并入报告，只有协调器的白名单事件与数值汇总会归档。

## 阶段和指标

全局 add → drain → search，所有用户完成写入后再进入检索。backend_direct 的 add 耗时包含异步状态轮询；OpenClaw 分开记录 Agent 接收耗时与原生 compact/精确任务 drain 耗时。sleep、全局队列清空等弱证据不视为精确就绪。单请求失败不停止其他用户；已有部分写入的用户标记 partially_written。

每用户最多一个在途请求，每实例 max_inflight 和全局 max_workers 都生效。速率是每用户请求开始间隔的下限（0 不限速），最后一条请求后不再额外等待；不会复刻源文件的生产时间间隔，也不重放原始 add/search 交错顺序。JSONL 按行读取并及时关闭文件，不会为每个用户长期占用文件描述符。

当 max_workers 不小于活跃实例数时，为每个实例保留至少一个执行槽，避免慢实例占满线程池；其他槽可供实例共享。worker 数少于活跃实例数时每实例最多占一个槽，实例之间仍需轮流等待，因此建议 max_workers 至少等于实例数。OpenClaw 的 HTTP 200 还会检查业务状态和 Responses 输出结构，failed/incomplete 或空对象不能计为成功。

输出位于 `runs/<run-id>/`：

- request_events.jsonl：增量写入，匿名身份、实例、请求 ID、操作、源行号、输入 hash/字节数、排队时间、调用时间、状态和数值结果数。
- user_summary.json/csv、instance_summary.json/csv：分用户/实例的请求数、成功率、超时、QPS、p50/p95/p99/max。
- summary.json、report.html：整体及分用户结果（整体 QPS 使用共同阶段窗口）；analyze-run 严格验证报告、事件关联和重算指标后重新渲染。
- run_config.json：脱敏绑定、各用户控制项、输入文件指纹、model_mode、适配器/插件标识与可用版本，不保存原始路径、请求、回答、凭据或私有端点。

用户 QPS 使用该用户的实际活跃窗口；实例与整体 QPS 使用共同阶段窗口。QPS 分子包含成功和失败请求；延迟分位数也包含失败，必须结合成功率解读。分位数从原始事件计算，绝不平均各用户分位数。OpenClaw Agent 端到端延迟不能与记忆 API 延迟混作同一指标。

可选实例 perf_trace_path 只用于请求 ID 关联覆盖率，trace 不可读或损坏不影响客户端指标。内部阶段、tokens、质量、硬件数据没有证据时明确 unavailable/skipped；Agent 有答案不等于检索命中。model_mode 是操作者对目标部署的声明，不会自动把外部模型切换成 mock/replay。此版本只证明客户端测试链路；真实隔离与真实性能还需使用目标服务的小规模 probe 验证。
