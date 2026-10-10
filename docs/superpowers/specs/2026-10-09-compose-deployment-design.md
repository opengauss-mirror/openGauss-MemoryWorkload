# Compose 部署与原生 workflow 运行绑定设计

状态：部署与运行行为已在对话中确认；待审阅本文，再制定实施计划。

## 目标

使用一份 YAML 声明多个 Agent、记忆系统、可选向量数据库、可选图数据库和可选 Mock Server 模板，以及多组 Agent–记忆系统组合。`memory-bench deploy --config deployment.yaml` 默认给每组组合创建独立的容器和记忆数据卷；可显式声明多个 Agent 组合共用一个记忆实例，以测试共享后端争用。`memory-bench run --deployment <name>` 在同一次调用中依次运行所有组合，每组沿用现有平台原生 workflow、Skill 和报告结构；可选的 `pressure_tests` 将多个组合纳入同一场多用户并发压测。镜像可指定现有镜像名，也可同时给出本地 Docker 镜像 tar 包路径。未使用部署参数的现有 `run` 行为保持原样。

本功能管理本机 Docker Compose 上的测试环境。它不代替 Agent Skill、Memory Skill 或 Memory Plugin Skill；镜像和端口不能推断被测系统的请求协议。每个组合绑定一个 Agent 模板和一个记忆系统模板；一场压测可同时使用多个组合，因此可覆盖多个 Agent 和多个记忆系统。第一版不在单个组合内部混用多个 Agent 或多个记忆系统，不自动扩缩容，不运行外部 benchmark runner，也不从任意镜像自动生成系统配置。

## 方案

平台从严格校验的 YAML 生成 Compose 文件，不要求各被测系统提供 Compose 文件。模板描述镜像、启动命令、端口、环境变量、配置挂载、状态挂载和健康检查；组合选择模板及 benchmark，并声明 `backend_direct` 或 `agent_plugin`。独立组合各生成一个 Compose project；显式共享同一记忆实例的组合合并到该实例的一个 project，共用记忆容器、数据库、存储网络和数据卷，但保有独立的 Agent 容器与 Agent 网络。不同 project 的容器、网络、状态目录和数据卷不共用；镜像层可以共用。可选向量数据库、图数据库和 Mock Server 只在引用时创建。

相比直接拼接外部 Compose 文件，生成方式能保证实例命名、端口发现、数据隔离和运行绑定遵守同一契约。Compose 只负责容器生命周期，现有 Skill 继续负责调用和结果解析。

## YAML 契约

顶层字段为 `schema: deployment/1`、`name`、`agents`、`memories`、`vectors`（可省略）、`graphs`（可省略）、`mocks`（可省略）、`shared_memory_instances`（可省略）、非空 `combinations` 和可选 `pressure_tests`。标识符使用安全的短名称；未知字段、重复 ID、缺失引用和不匹配的 Skill 在启动 Docker 前报错。相对路径基于 YAML 所在目录解析。

每个服务模板声明：

- `skill`：Agent 或 Memory Skill ID；向量库、图数据库和 Mock Server 无 Skill 要求。
- `image.name`：Compose 使用的镜像名或 digest；`image.archive` 可选，为本地 tar 包。若给出 archive，先 `docker load`，随后验证所需镜像确实存在；若只给镜像名，交由 Docker 拉取或使用本地缓存。归档记录实际 image ID。
- `container_port`、`endpoint_scheme`（默认 `http`，图数据库可指定 `bolt` 等）和必需的容器健康检查；宿主端口仅绑定回环地址，交给 Docker 分配空闲端口。部署后从 Docker 查询实际端口，不预测端口号。
- 可选 `command`（容器启动程序及固定参数的 argv 数组）、非敏感的 `environment`、从宿主环境读取的 `environment_from`、只读 `config_mounts`、可覆盖的 `config_files` 和持久化的 `state_mounts`。`command` 保持模板原值，不受 deploy 参数影响；未设置时保留镜像默认启动行为。引用的宿主环境变量在部署前检查；实际值不写进生成的 Compose 文件或平台部署记录。`state_mounts` 默认生成所属组合或共享记忆实例专属的 Docker volume；需要从宿主侧读取 OpenClaw state 的挂载可显式标记 `host_visible: true`，其目录放在所属部署目录内。
- Agent 和记忆系统的 `config_files` 以唯一文件键声明 `source`、容器内绝对路径 `target`、`format`（首版 `json` 或 `yaml`）和非空 `parameters`。每个参数以唯一短名称声明指向现有字段的 RFC 6901 JSON Pointer `pointer` 和严格值类型 `type`（`string`、`integer`、`number`、`boolean`）。参数名在同一服务模板内唯一；同一文件中两个参数不得指向同一字段。`source` 是 YAML 目录下的文件，`target` 不得与其他配置或状态挂载重叠。平台只修改显式列出的参数，不创建缺失路径或改变字段类型；不提供通用文本替换、任意路径修改或启动参数修改。普通 `config_mounts` 保持原样、只读挂载，不接受字段覆盖。
- 部署时先读取源配置，再为每个物理 Agent/记忆实例生成独立副本，应用该实例的 CLI 覆盖，最后只读挂载到 `target`。原始文件不修改；未覆盖的字段保持原值。共享记忆实例只有一份副本。生成目录权限为 0700，文件权限为 0600；部署前验证容器运行用户可读取挂载文件。状态和日志只记录参数名、来源及配置文件哈希，不回显配置内容或 CLI 值。覆盖参数只用于非敏感字段；凭据继续通过 `environment_from`，避免在命令行历史中留下凭据。源文件若含凭据，副本按敏感文件处理并在 `undeploy` 时清理。配置文件解析失败、字段不存在、类型不符或写入/挂载失败均在容器启动前报错。
- Agent 和记忆系统模板的 `client.endpoint_env`：运行 Skill 所需的宿主侧 URL 环境变量名，例如 `OPENCLAW_GATEWAY_URL`、`MEM0_API_URL` 或 `OPENVIKING_API_URL`。`client.environment_from` 可传递运行 Skill 所需的凭据变量，但部署记录只保存变量名；向量库、图数据库和 Mock Server 模板不要求 `client`。
- Agent 和记忆系统模板的 `verify.command` 与 `verify.expected_version`：健康检查通过后，在容器内执行只读命令。命令输出 JSON，包含 `version`、实际配置的 `endpoints`、逐依赖的 `reachable` 布尔值，以及按模板参数名返回的 `effective_parameters`；平台精确比较版本、地址、可覆盖字段的生效值，并要求每个声明的依赖可达。核验输出仅用于进程内比较，不写入状态或日志。缺少命令或检查不符时，该组合不能进入 ready。向量库、图数据库和 Mock Server 以容器健康检查为最低要求，也可配置同样的版本检查。
- 可选 `resources.cpus`、`resources.memory_mb` 和 `resources.numa_node`：前两项既作为容器限额，也参与启动前的 Docker 可用容量检查。NUMA 只在 Linux 主机显式启用；无法识别 NUMA 拓扑或资源不足时，在创建容器前失败。未指定资源时沿用 Docker 默认调度，不声称资源隔离。

Mock Server 模板沿用镜像、环境变量、只读配置挂载、状态挂载、健康检查和资源字段，并以非空 `endpoints` 映射声明一个或多个命名监听端口、协议及 `model_mode`（`replay-zero-delay`、`replay-with-delay` 或 `mock-fixed`），例如 `chat` 和 `embedding`。每个端口仅在宿主回环地址发布。它不需要 Agent/Memory Skill，也不要求 `verify.command`；被测 Agent 或记忆系统通过 `service_endpoints` 引用 `mock.<端点名>`。平台为引用方生成 Mock 健康依赖，并在引用方的 `verify.command` 中检查实际指向及可达性。Mock 模板可挂载只读 trace 或模型响应文件；平台不解释其内容。

`service_endpoints` 将容器环境变量映射到本组合或显式共享记忆实例的 `agent`、`memory`、`vector`、`graph` 容器内地址。引用目标必须存在，使用目标模板的 `endpoint_scheme` 和 `container_port`；不能引用其他独立组合或共享组。组合指定向量库或图数据库时，记忆系统模板必须声明相应地址映射；`agent_plugin` 组合还要求 Agent 模板声明记忆系统地址映射。平台自动生成 `memory -> vector`、`memory -> graph` 的 Compose 健康依赖，并在 `agent_plugin` 模式生成 `agent -> memory` 依赖。插件配置文件仍由使用者提供并通过只读挂载传入，平台不会猜测插件内部配置字段；部署核验必须读取插件生效配置，而不能只核对环境变量。

`service_endpoints` 引用 Mock 时使用 `mock.<端点名>`，由平台替换为所属组合或共享实例 Mock 容器的协议、服务名和命名端口。独立组合的引用方使用组合的 `mock`；共享组中 Agent 使用本组合的 `mock`，记忆系统使用共享实例的 `mock`。引用方未配置对应 Mock 或引用不存在的端点时，预检立即失败；未被引用的 Mock 模板不创建容器、卷或依赖。

独立组合生成 `agent_net` 与 `storage_net`：Agent 默认只在前者，向量库和图数据库只在后者，记忆系统同时连接两者。共享记忆实例为每个 Agent 组合生成独立 `agent_net_<组合 ID>`，记忆容器连接这些 Agent 网络及一条共享 `storage_net`，数据库只连接存储网络；各 Agent 不能直接访问别的 Agent。需要 Agent 直连数据库的模板必须显式设置 `allow_agent_storage_access: true`；平台记录这一配置。部署核验在 `agent_plugin` 模式检查 Agent→记忆系统连通性，并始终检查记忆系统→已声明数据库连通性；不把容器进程健康当作依赖可达。镜像包 SHA256 与架构匹配校验不在本阶段范围内；仍校验 tar 可加载和目标镜像名存在。

Mock Server 仅接入引用方所在的网络：Agent 引用时接入其 Agent 网络，记忆系统引用时接入 `storage_net`，两者都引用时接入相应网络。共享组中的组合级 `mock` 只可供该 Agent 引用；共享记忆实例级 `mock` 只可供记忆系统引用，避免同一记忆容器从不同组合获取冲突配置。不同部署组使用独立 Compose 网络，Mock 不加入无关网络。Mock 的镜像、健康状态、端口和资源限额与其他服务一起纳入部署状态、`deploy status` 和失败清理。

组合包含 `id`、`benchmark`、`agent`、`memory` 或 `memory_instance` 二选一、可选 `vector`、可选 `graph`、可选 `mock`、`memory_integration`、`data_path`、可选 `entrypoint` 和可选非敏感运行参数。`memory_instance` 引用顶层 `shared_memory_instances`，后者声明 `memory`、可选 `vector`/`graph`/`mock` 和可选基线 `preparation`。使用 `memory_instance` 时，组合不得重复配置 `vector`/`graph`；组合的 `mock` 仅用于 Agent，记忆系统所需 Mock 必须配置在共享实例上。相同共享实例的记忆模板、数据库、记忆侧 Mock、数据卷和基线准备仅创建/执行一次；同组的 Agent 各有独立容器和状态。benchmark 数据路径及 Skill 契约在部署前校验。组合的 `entrypoint` 必须是平台原生 scenario/case 入口；外部 runner 入口报错。`agent_plugin` 组合还必须能由现有 Skill 解析出对应的 Memory Plugin Skill。

独立组合可选 `preparation`；共享组合的 `seed`、`readiness` 和 `seed_resource_boost` 必须配置在共享记忆实例，组合只可配置自己的 `warmup`。`seed` 和 `warmup` 各有 `enabled` 开关，默认 `false`；启用时必须提供目标服务角色和容器内 argv 命令。`seed_resource_boost` 可为所属向量库设置 seed 期间的临时 CPU/内存限额。`readiness` 有独立的 `enabled` 开关，默认 `false`；启用 seed 时必须同时启用 readiness，避免未完成索引就把部署标记为 ready。readiness 命令必须只读，输出 `{"ready": true, "details": {...}}`；平台在期限内轮询并要求配置的连续成功次数。各命令不经 shell；何为基线索引完成、如何构造基线数据由对应数据库/benchmark 脚本定义。此处 seed 只能写入与压测用户身份无关的共享/基础设施基线；按用户身份写入的数据属于 run 准备阶段，因为每次 run 都生成新的用户身份。此处的 readiness 检查部署基线；workflow 在每次写入后的 checkpoint readiness 仍属于 run，不能提前到 deploy。

`pressure_tests` 中每场压测声明 `id`、`max_workers`、`workloads`、`targets`、非空 `users`、可选 `user_seed`、可选 `load_profile` 和可选 `validation`。`targets` 通过唯一的 `combination` ID 引用已部署组合；组合中的 `agent`、`memory` 或 `memory_instance`、`memory_integration` 和可选数据库/Mock 引用就是 Agent–记忆系统及其依赖的权威映射，不在压测配置中重复填写另一套端点。多场压测可以引用同一组合，但同一部署同一时刻只允许运行一场测试，避免相互污染。一个组合只能在同一场压测的 `targets` 中出现一次。

每个 target 声明 `max_inflight`、`isolation_probe` 和该 Agent 回答模型的 `model_mode`（`real-model`、`replay-zero-delay`、`replay-with-delay` 或 `mock-fixed`，由操作者声明）；每个用户声明唯一 `id`、`workload`、`target`、该 Agent 实例内唯一的 `agent_id`，以及可选速率/超时覆盖。workload 使用现有 `openmem-agent/1` 的 add/search JSONL 契约，路径相对部署 YAML；每用户请求串行，用户间及 target 间并发，受全局 `max_workers` 和目标 `max_inflight` 约束。一个 target 可以承载多个用户；同一个 Agent 模板被多个组合引用时仍实例化为不同容器，记忆系统模板默认也如此，只有显式 `memory_instance` 才共享。压测必须引用 `agent_plugin` 组合，实际请求经该组合 Agent 再到其绑定的记忆系统；`backend_direct` 仍可用于普通 workflow，不能伪装成 Agent 压测。

部署前验证所有 target、workload 和用户引用，并检查 Agent Skill、Memory Plugin Skill 是否支持多用户请求、独立 `agent_id` 作用域、精确完成/就绪信号及所选协议。多用户适配器的输入为 `openmem-agent/1` 请求、`request_id`、逻辑 `agent_id`、用户/会话身份和目标端点；输出至少包含请求状态、可关联的 `request_id`、回答或检索结果及写入所需的会话句柄。插件提供 `prepare`、`set_phase`、`flush`、`wait_settle`、`finalize`，并返回精确就绪凭证；平台传入每个组合独立的运行上下文，不把 Agent Skill ID 写死为 OpenClaw。同一记忆实例被多个组合引用时，插件还必须声明并证明 prepare、阶段切换和 finalize 以 Agent/用户作用域隔离，不会重置另一 target 的状态；否则预检拒绝共享拓扑。现有多用户回放只提供 OpenClaw 的 Agent 路径；支持其他 Agent Skill 是本设计的实施工作，需由该 Skill 实现此接口并在 manifest 声明能力，不从镜像名推断。缺少能力的组合在启动 Docker 前报错，不静默改走记忆系统直连。

`user_seed.enabled` 默认 `false`。启用时，每个被引用 workload 必须提供非空 `seed_file`，沿用 `openmem-agent/1` add 格式。run 已确定本次用户/会话身份后，经对应 Agent 适配器为各用户写入该文件，并等待插件精确就绪；失败的用户不进入测量。此阶段的请求、耗时和就绪证据单独归档，不计入压测吞吐或延迟。deploy 的 `seed` 仍只负责不依赖压测用户身份的共享基线，不能替代此步骤。

默认 `load_profile.mode: finite_replay`，按源 JSONL 各执行一次，适用于回放但不宣称找到容量上限。可选 `steady_search` 在有限 add 和精确 drain 后循环读取 search JSONL，要求正数 `ramp_seconds`、`steady_seconds` 和全场 `target_rps`；调度器按用户轮转计划请求，受全局与各 target 在途数约束，不无限积压。报告区分目标与实际 RPS、队列等待、超时及因容量限制未发出的计划请求。升压段不计入稳态延迟分位数；每次循环派生新 `request_id`，保持用户身份与查询语义。持续写入会改变记忆基数和去重语义，本版仅支持有限 add 回放，不把循环写入包装成稳态写入压测。

`validation` 可声明 0 到 1 之间的 `sample_rate`、`min_request_success_rate`、`min_correct_rate`，非负整数 `max_isolation_violations`，以及 `checker.command`。checker 是以部署 YAML 所在目录为 cwd 的宿主机 argv 命令，不经 shell；收到抽样的原始请求、Agent 回答与 JSONL 中可选的预期结果，在权限受控的临时目录运行，返回结构化的正确/非空/串读判定；原文和答案不进入报告。现有回放解析器会忽略 JSONL 的 `response`，实施时须把预期结果传给 checker，不能只读取请求。没有预期结果或 checker 时，只报告请求完成、非空率和隔离探针结果，正确率标记 `unavailable`，不能据此宣称记忆质量达标；配置了 `min_correct_rate` 却无法评分时预检失败。压测前后及稳态期间使用独立哨兵身份做跨用户/跨组合隔离探针；探针请求单独标记，不混入业务延迟和 QPS，但其资源开销在报告中披露。正确性或隔离门槛未达标时整场失败。

示意结构（字段值需匹配实际镜像和 Skill）：

```yaml
schema: deployment/1
name: locomo-matrix
agents:
  claw:
    skill: openclaw
    image: {name: "example/openclaw:tag", archive: ./images/openclaw.tar}
    command: [node, /app/server.js]
    config_files:
      runtime:
        source: ./configs/openclaw.json
        target: /runtime/openclaw.json
        format: json
        parameters:
          log_level: {pointer: /logging/level, type: string}
    container_port: 18789
    healthcheck: [CMD, node, /app/healthcheck.js]
    service_endpoints: {MEMORY_URL: memory}
    verify: {command: [node, /app/verify-runtime.js], expected_version: example-version}
    client: {endpoint_env: OPENCLAW_GATEWAY_URL}
  worker:
    skill: example-agent
    image: {name: "example/agent:tag"}
    container_port: 8081
    healthcheck: [CMD, /app/healthcheck]
    service_endpoints: {MEMORY_URL: memory}
    verify: {command: [/app/verify-runtime], expected_version: example-version}
    client: {endpoint_env: EXAMPLE_AGENT_URL}
memories:
  mem0:
    skill: mem0
    image: {name: "example/mem0:tag", archive: ./images/mem0.tar}
    command: [python, /app/server.py]
    config_files:
      runtime:
        source: ./configs/mem0.yaml
        target: /app/config.yaml
        format: yaml
        parameters:
          workers: {pointer: /server/workers, type: integer}
    container_port: 8000
    healthcheck: [CMD, python, /app/healthcheck.py]
    state_mounts: [{target: /app/data}]
    service_endpoints:
      VECTOR_URI: vector
      LLM_URL: mock.chat
      EMBEDDING_URL: mock.embedding
    verify: {command: [python, /app/verify_runtime.py], expected_version: example-version}
    client: {endpoint_env: MEM0_API_URL}
  graphmem:
    skill: example-graph-memory
    image: {name: "example/graphmem:tag"}
    container_port: 8080
    healthcheck: [CMD, /app/healthcheck]
    state_mounts: [{target: /app/data}]
    service_endpoints: {GRAPH_URI: graph}
    verify: {command: [/app/verify-runtime], expected_version: example-version}
    client: {endpoint_env: GRAPHMEM_API_URL}
vectors:
  milvus:
    image: {name: "example/milvus:tag"}
    container_port: 19530
    healthcheck: [CMD, /app/healthcheck]
    state_mounts: [{target: /var/lib/milvus}]
graphs:
  neo4j:
    image: {name: "example/neo4j:tag"}
    container_port: 7687
    endpoint_scheme: bolt
    healthcheck: [CMD, /app/healthcheck]
    state_mounts: [{target: /data}]
mocks:
  model-replay:
    image: {name: "example/model-replay:tag", archive: ./images/model-replay.tar}
    endpoints:
      chat: {container_port: 18094, endpoint_scheme: http, model_mode: replay-zero-delay}
      embedding: {container_port: 18095, endpoint_scheme: http, model_mode: replay-zero-delay}
    healthcheck: [CMD, /app/healthcheck]
    config_mounts: [{source: ./traces, target: /traces}]
combinations:
  - id: claw-mem0
    benchmark: locomo
    agent: claw
    memory: mem0
    vector: milvus
    mock: model-replay
    memory_integration: agent_plugin
    data_path: ./data/locomo_small.json
    preparation:
      seed: {enabled: true, target: memory, command: [python, /app/seed_baseline.py]}
      readiness: {enabled: true, target: memory, command: [python, /app/check_baseline_index.py], timeout_seconds: 300, consecutive_successes: 2}
      warmup: {enabled: true, target: memory, command: [python, /app/warmup_isolated.py]}
  - id: worker-graphmem
    benchmark: locomo
    agent: worker
    memory: graphmem
    graph: neo4j
    memory_integration: agent_plugin
    data_path: ./data/locomo_small.json
pressure_tests:
  - id: agent-memory-load
    max_workers: 32
    user_seed: {enabled: true}
    load_profile: {mode: steady_search, ramp_seconds: 30, steady_seconds: 300, target_rps: 20}
    validation:
      sample_rate: 0.1
      checker: {command: [python, ./scripts/check_answer.py]}
      min_request_success_rate: 0.99
      min_correct_rate: 0.9
      max_isolation_violations: 0
    workloads:
      support: {format: openmem-agent/1, seed_file: ./data/support/seed.jsonl, add_file: ./data/support/add.jsonl, search_file: ./data/support/search.jsonl}
      developer: {format: openmem-agent/1, seed_file: ./data/developer/seed.jsonl, add_file: ./data/developer/add.jsonl, search_file: ./data/developer/search.jsonl}
    targets:
      - combination: claw-mem0
        max_inflight: 8
        model_mode: real-model
        isolation_probe: {target: agent, command: [node, /app/check-isolation.js]}
      - combination: worker-graphmem
        max_inflight: 8
        model_mode: real-model
        isolation_probe: {target: agent, command: [/app/check-isolation]}
    users:
      - {id: user-a, target: claw-mem0, workload: support, agent_id: bench-a}
      - {id: user-b, target: claw-mem0, workload: developer, agent_id: bench-b}
      - {id: user-c, target: worker-graphmem, workload: support, agent_id: bench-c}
```

此示例只说明结构，不保证这些占位镜像、Skill 或插件包含所列命令和多用户能力；示例中的 `seed_file`、add/search JSONL 及 checker 也需提供真实文件，且启用正确率门槛时 JSONL 必须含可评分的预期结果。正式配置须明确模型服务地址、凭据引用、插件路由及适配器配置。示例中 `claw-mem0` 和 `worker-graphmem` 分别把两个 Agent 模板绑定到两个记忆系统模板；两个 `claw-mem0` 用户共享该组合的容器，但有不同的逻辑 Agent 身份。

若要让两个 Agent 组合真正争用同一记忆实例，可在同一 YAML 中另加如下映射；这两个组合不得再写 `memory`、`vector` 或 `graph`，并由压测 `targets` 同时引用：

```yaml
shared_memory_instances:
  mem0-common:
    memory: mem0
    vector: milvus
    mock: model-replay
    preparation:
      seed: {enabled: true, target: memory, command: [python, /app/seed_baseline.py]}
      readiness: {enabled: true, target: memory, command: [python, /app/check_baseline_index.py], timeout_seconds: 300, consecutive_successes: 2}
combinations:
  - {id: claw-common, benchmark: locomo, agent: claw, memory_instance: mem0-common, memory_integration: agent_plugin, data_path: ./data/locomo_small.json}
  - {id: worker-common, benchmark: locomo, agent: worker, memory_instance: mem0-common, memory_integration: agent_plugin, data_path: ./data/locomo_small.json}
```

此片段展示替代映射，不是附加到上方示例的完整配置。共享实例测试与独立实例测试必须在报告中区分，不能把相同记忆镜像误当作相同物理实例。

部署时可临时覆盖指定 Agent 或记忆实例的配置文件字段，无需修改源文件：

```bash
memory-bench deploy --config deployment.yaml \
  --agent-config 'claw-mem0:log_level="debug"' \
  --memory-config 'claw-mem0:workers=4'
```

每个选项可重复，格式为 `<实例 ID>:<参数名>=<JSON 值>`，按第一个 `=` 分割，值使用 JSON 解析，以保留数字、布尔和字符串类型。Agent 实例 ID 始终是组合 ID；独立记忆实例 ID 也是组合 ID，共享记忆实例 ID 使用 `shared_memory_instances` 的键。同一实例的不同参数可以分别覆盖；同一参数重复、未知实例/参数、角色不符、JSON 无效、值类型不符均在任何容器启动前报错。共享记忆实例只接受一份物理实例配置，不允许从不同 Agent 组合分别修改。未指定覆盖的字段保留源文件值；覆盖只对本次新部署生效，同名已存在的部署仍须先 `undeploy`。

## 部署状态与生命周期

`deploy` 先解析全部组合和压测映射、校验配置字段覆盖、验证数据文件及 Skill 能力、检查镜像 archive 与环境引用，然后开始 Docker 操作。生成内容位于当前工作目录的 `.memory-bench/deployments/<name>/`，包括不含凭据的配置摘要、每个 project 的 Compose 文件、实例配置副本和状态记录。配置指纹包含源文件与生成副本的哈希、参数名和来源（源文件或 CLI），不保存 CLI 值；`deploy status` 复核副本哈希、容器挂载源路径和 `verify.command` 输出中的实际生效配置字段、版本、地址与依赖，以发现漂移。服务的 `verify.command` 必须输出每个可覆盖参数的实际生效值供平台比较；比较结果仅报告字段名和是否匹配，不回显值。无法核实实际生效值时不得标记 ready。此目录应由 Git 忽略。部署名在该工作目录内唯一；同名已存在时不重建或覆盖，要求先显式清理。

在 Docker 操作前，按实际要创建的容器汇总 CPU/内存限额；共享实例的记忆容器和数据库只计一次，与 Docker 运行环境可用资源比较。明确声明的 NUMA 位置也在此时规划并检查。容量不足直接失败，不先启动部分组合。未声明的资源不计入可保证容量。镜像 archive 仅验证存在、可加载并含所需镜像名；本阶段不计算 SHA256，也不校验镜像架构。

启动每个 project 后，等待其全部服务健康，执行版本、实际配置和依赖连通性核验。若 `seed.enabled: true`，在部署阶段按“临时提升向量库资源（若配置）→ seed → 索引/后端就绪轮询 → 恢复原始资源并核验 → 再次确认就绪”的顺序准备基线；共享记忆实例只做一次。若只启用 readiness，也在部署阶段确认现有数据已就绪。任何失败或中断都尝试恢复资源；恢复失败时不得标记 ready。记录 project 名、组合 ID、共享实例 ID（若有）、容器 ID、宿主回环端口、镜像 ID、资源限额、配置哈希、seed/就绪结果和创建时间。若任一组失败，停止并移除本次新建的所有 project、数据卷和生成的配置副本，保留脱敏失败诊断。只有全部检查通过后才将部署状态标记为 ready。部署不运行 benchmark；只有明确启用 seed 时才写入共享基线数据，且每个新记忆实例只执行一次。

`memory-bench deploy status --deployment <name>` 重新读取 Docker 实际状态，显示每组及共享实例的服务健康、端口、镜像 ID、资源限额、配置指纹和 seed/就绪状态；启用了 readiness 的实例重新执行只读就绪检查。容器缺失、被替换、镜像改变、配置核验或就绪检查失败时报告 drift，并给出非零退出码。状态输出不包含凭据。`run --deployment` 使用同一状态核验，不信任旧的 ready 标记，也不重新执行部署基线 seed。

提供 `memory-bench undeploy --deployment <name>`，仅停止状态记录中属于该部署的 Compose project，并移除这些 project 的数据卷及宿主可见状态目录；共享实例及其 Agent 组合以 project 为单位一起清理，不因某个组合运行失败就删除共享后端。显式 `undeploy` 才删除成功部署的数据；`run` 不删除或重建容器。清理失败时保留状态记录和错误，便于重新执行清理。平台不操作记录之外的容器或卷。

## `run` 绑定和结果

`run --deployment <name>` 从当前工作目录读取 ready 状态，验证配置哈希、容器归属和健康状态，然后按 YAML 顺序运行组合。可用 `--combination <id>` 只运行一组。组合的 Agent/Memory Skill、集成模式、数据路径和原生 entrypoint 来自部署配置；同一调用不再要求 `--agent`、`--memory-backend` 等重复选择参数。部署模式与这些手动选择参数同时给出时直接报错，避免不一致。`--pressure-test <id>` 选择一场多用户压测，不能与 `--combination` 同用；未指定时仍按顺序运行普通 workflow，不隐式启动压测。

每组以独立子进程调用现有 `memory-bench run` 原生路径。子进程只接收该组合的 Agent 与 Memory URL、明确声明的运行环境引用及原有用户环境；不修改父进程全局环境，也不把一组的端点带入另一组。每组使用唯一 run ID 和现有 `runs/<run-id>/` 归档。顶层 suite 摘要记录每组 run 目录、状态、部署标识和物理记忆实例 ID；不合并不同组合的评分或耗时。默认顺序运行若包含共享实例，后运行组合会看到前一组合写入的状态，报告标记这一顺序和共享关系，不把这些结果当作独立基线比较。单组失败仍尝试后续组合，最后返回非零；用户中断则停止后续组合，并保留已完成的 run 及部分 suite 摘要。

若组合或其共享记忆实例使用 Mock，run 记录被注入的 Mock 端点、对应消费方和各端点 `model_mode`，以区分真实模型与回放或固定响应；两处均未配置 Mock 时不设置 Mock 端点，也不启动 Mock。Mock 的健康检查只证明服务可用，不代表回放内容匹配或计数正确；这些仍由相应测试的运行期校验判断。

若 `warmup.enabled: true`，每次 run 在状态/基线就绪复核通过后、正式 workflow 前执行该组合的 warmup；否则直接开始 workflow。warmup 的耗时与结构化结果归档在本次 run 中，不计入正式负载指标。warmup 必须使用与正式测试隔离的身份或数据范围，具体约束由组合的准备脚本实现并在报告中声明；平台不把一次 warmup 成功解释为记忆结果正确。部署基线 seed 和基线索引构建属于 deploy；压测用户 seed 属于 run 的测量前准备；正式 workflow 的逐次写入、commit 和 checkpoint readiness 继续在 run 中执行。重复运行同一部署若可能留下被测数据，比较实验应创建新部署以恢复相同基线。

运行前健康检查证明服务可访问；实际用户/会话隔离和记忆写入正确性仍由 Skill 与 workflow gate 验证。独立组合即使使用相同镜像，也有各自的记忆容器、数据库、Mock Server 和数据卷；共享记忆实例的组合则有意共用这些资源，报告必须标记共享拓扑。

## OpenClaw Gateway Pool 扩展

OpenClaw 可为一个组合部署多个 Gateway 实例，并在它们前面部署一个独立的 Gateway Pool。此扩展沿用该组合所属的 Compose project、记忆实例和网络；每个 Gateway 有独立的 OpenClaw 配置副本、state 目录和容器身份，均连接同一个组合指定的记忆实例。Pool 连接该组合的 Agent 网络，只代理指向本组合 Gateway 的请求，不跨组合或共享记忆实例的其他 Agent 路由。未启用 Pool 的组合保持当前单 Agent 拓扑。此扩展不改变 `backend_direct` 的记忆系统直连路径。

在 `deployment/1` 中增加可选的顶层 `gateway_pools` 模板映射。模板声明 `image`（镜像名及可选 tar）、`container_port`、`healthcheck`、可选 `environment`/`environment_from`、`config_mounts`、`state_mounts`、`resources`，以及运行参考项目 `gateway_pool.py` 协议的 `command` 前缀。首版只支持 `kind: openclaw_http_pool_v1`：平台在命令后追加 `--host 0.0.0.0 --port <container_port>`、每个 Gateway 的 `--upstream http://<service>:<agent.container_port>` 和 `--expected <副本数>`；不依赖 Compose DNS 对缩放副本的返回顺序。组合增加可选 `gateway_pool` 模板引用和 `agent_replicas` 正整数；未指定 Pool 时副本数必须为 1，指定 Pool 时 Agent Skill 必须为 `openclaw`，且使用 HTTP transport。每个组合独立实例化 Pool；模板可以复用，Pool 容器、用户分配表和端口不能跨组合共用。示意增量：

```yaml
gateway_pools:
  openclaw-pool:
    kind: openclaw_http_pool_v1
    image: {name: "example/runner-with-gateway-pool:tag"}
    command: [python, /workspace/scripts/gateway_pool.py]
    container_port: 29789
    healthcheck: [CMD, python, /workspace/scripts/check_pool.py]
combinations:
  - id: claw-mem0
    benchmark: locomo
    agent: claw
    agent_replicas: 3
    gateway_pool: openclaw-pool
    memory: mem0
    memory_integration: agent_plugin
    data_path: ./data/locomo_small.json
```

Compose 为该组合生成 `agent_<id>_1` 至 `agent_<id>_N` 和 `gateway_pool_<id>`。各 Agent 的内网端口可以相同；仅 Pool 的宿主端口供 `run` 调用，Agent 宿主端口只在需要单实例核验时发布到回环地址。Pool 以 `depends_on: service_healthy` 等待全部上游，部署就绪检查还需请求 `/pool/status`，核对上游数量、地址集合与声明的副本数，并对每个 Agent 单独执行版本、配置生效、插件连接和依赖可达核验。任一副本或 Pool 不健康、上游集合变化、容器或镜像漂移时，`deploy status` 和 `run --deployment` 均拒绝 ready。容量预检按副本数累加 Agent 资源，再加 Pool 资源；部署状态记录 Pool 和每个副本的容器、镜像、端口及配置指纹。失败回滚和 `undeploy` 按原有 project 归属清理这些资源。

启用 Pool 的组合，其 Agent `client.endpoint_env` 指向 Pool 的宿主回环 URL。OpenClaw HTTP 适配器必须在每个 `/v1/responses` 请求体中传稳定的 `user`，并在使用 `X-OpenClaw-Memory-User` 时传相同值；参考实现只按请求体的 `user` 维护粘性分配，缺失该字段会逐请求轮转。普通 workflow 由运行上下文生成稳定且隔离的用户身份，多用户压测使用本次 run 与目标实例、用户 ID 派生的身份；同一用户的 add、search、用户 seed、用户 warmup 和隔离探针必须使用同一个路由身份。没有可验证的路由身份时，不向 Pool 发送测量请求。现有 `preparation` 的容器内命令不经 Pool，启用 Pool 的组合不得配置以 Agent 为目标的 seed 或 warmup；用户级准备由后续压测实现经 Pool 调用。Pool 返回的 `X-OpenClaw-Upstream` 与 `/pool/status` 分配表用于核对实际路由；报告记录脱敏的用户到上游实例映射、每实例请求量和上游故障，不把仅有 Pool HTTP 200 视为 Agent 或记忆写入成功。Pool 实例重启导致分配表丢失时，运行中视为拓扑变化并停止该组合，避免同一用户迁移到另一 Gateway 的会话状态。

本节是对已实现 Compose 部署功能的后续设计，当前 `deployment_config.py`、`deployment_compose.py` 和 `deployment_run.py` 尚不支持 `gateway_pools`、`agent_replicas` 或 Pool 端点绑定；实现前需单独制定并审阅增量实施计划。参考项目的 Pool 实现位于 `AgentMemoryWorkload/scripts/gateway_pool.py`，部署方式见其 `docker-compose.runner.yml` 和 `scripts/generate_scaleout_compose.py`；实际镜像必须包含与此协议兼容的程序。

## 多用户、多 Agent 与多记忆系统压测

`memory-bench run --deployment <name> --pressure-test <id>` 根据 `targets[].combination` 获取真实容器端点、Agent Skill、Memory Plugin Skill、物理记忆实例 ID 和部署状态。平台扩展现有多用户回放协调器，使一场运行能同时调度不同 Agent Skill 的 target；每个请求携带所属用户的 `agent_id` 和由 run ID、用户 ID 派生的独立用户/会话身份。Agent 适配器处理 `openmem-agent/1` 的 add/search 请求，并返回可关联的结果；插件负责 prepare、阶段切换、精确写入完成/就绪和 finalize。每个 target 使用独立的 Agent 环境及插件运行上下文；共享记忆实例的 target 仅共用后端容器，不共用 Agent 会话。压测前通过 Agent 写入随机哨兵，再从指定记忆实例的对应作用域读取，核对实例 ID、请求关联和实际命中，并验证其他非共享实例未命中；无法证明实际路由时停止该 target，不以环境变量相等代替验证。

启动负载前，依次复核部署健康与基线 readiness、执行各 target 插件 prepare、运行使用独立哨兵作用域的路由/隔离探针、按需完成每用户 seed 与精确就绪，并按各组合的 `warmup.enabled` 执行隔离的 warmup。容器内探针通过 stdin 接收 `multi-user-isolation-probe/2` JSON，包含本场 run ID、target/组合 ID、物理记忆实例 ID、用户与 Agent ID 列表及其他 target 的标识；输出须包含目标实例 ID、实际请求 ID、Agent ID 集合，以及路由、写作用域、读作用域和跨 target 隔离的布尔证据。平台比对这些字段与目标实例和插件回执，要求实际验证同 target 用户之间、共享记忆实例中的不同 Agent 之间，以及不同记忆实例之间的写入/检索隔离；缺少证据时不启动该 target。探针、用户 seed 和 warmup 完成后使用同一负载起点，跨所有 target 并发执行 add → 精确 drain/就绪 → search；任一 target 的准备失败时不向该 target 发负载，其他 target 可继续，整场退出非零。单用户请求按原有配置顺序串行，不把并发 QA/Judge 参数当作压测并发度。

每次 `run` 独占其引用的部署组合及任何共享记忆实例，并在本地部署状态中持有运行锁；另一次使用相同资源的 `run` 或 `undeploy` 在锁占用期间报错，异常退出时根据进程存活和部署状态恢复陈旧锁。源 JSONL 在写入前复制到权限受控的临时目录并校验，报告只保存输入指纹和数值摘要。重复使用同一部署会保留上次测试写入；需要相同初始状态作比较时创建新部署。部署基线 seed 每个物理记忆实例只执行一次，用户 seed 则按本次 run 的身份逐用户执行。

压测输出仍位于唯一的 `runs/<run-id>/`，包含脱敏的用户→target→组合→Agent/记忆系统/物理记忆实例映射、每用户和每 target 的请求数、成功率、QPS、p50/p95/p99、超时与失败、升压/稳态分段及共同阶段窗口指标，以及各 target 声明的 Agent `model_mode`、实际 Mock 端点及其模式、路由/隔离探针、用户 seed、warmup、drain/就绪和正确性证据。共享实例的指标另按物理记忆实例汇总，不能把两个共享 target 当作两台独立记忆服务。Agent 模型模式是操作者声明，不因记忆系统使用 Mock 就自动变成 Mock 模式；不能用一个全局值掩盖不同 target 的模型依赖。Agent 端到端延迟与记忆 API 延迟不得合并比较；本压测所有 target 均走 Agent 路径。单 target 失败仍保留其他 target 的事件和部分报告，最终返回非零；中断返回 130 并保留已完成事件。每用户和每 target 的分位数由原始事件计算，不平均分位数。

只有 workload 数据、用户数、速率、模型模式和初始数据状态一致的 target 才列入横向性能比较；其他 target 保留分项和共享实例总量报告，不计算“谁更快”的排名。请求事件保留 target、物理记忆实例和 `request_id`，可与可用的后端 trace 关联；缺少内部阶段或硬件采集证据时标记 `unavailable`，不从客户端延迟推断数据库或模型阶段耗时。

## 验证

Gateway Pool 增量验证覆盖：未配置 Pool 的原有单 Agent 行为；多个 Gateway 的独立配置和 state、相同记忆实例绑定、静态上游列表和网络归属；Pool 镜像与副本资源预检、启动依赖、`/pool/status` 上游集合、每副本生效配置和故障回滚；`run` 使用 Pool 端口且不继承冲突的父进程端点；同一用户跨 add/search/seed/warmup 的粘性路由、不同用户分配、请求体 `user` 与身份头不一致时拒绝、缺少身份时拒绝；Pool 重启、上游替换或缺失时状态漂移和运行中止；报告中保留脱敏分配与每实例请求量。使用参考 `gateway_pool.py` 的真实容器冒烟验证路由契约，不能只凭 Compose 渲染测试宣称端到端通过。

测试覆盖：严格 YAML 校验和路径解析；JSON/YAML 配置源文件、每实例副本、允许字段的类型化 CLI 覆盖、重复或未知字段拒绝、共享记忆唯一副本、配置挂载和实际生效值漂移；archive 加载后镜像名校验；独立组合与共享记忆实例的 Compose project、卷、端口、容量只计一次和网络隔离，包括可选图数据库与 Mock Server 的地址注入、命名端口及健康依赖；未配置 Mock 时不创建相关容器；资源预检在容器启动前失败；启动失败时只清理本次资源；ready 状态、版本/配置/连通性核验与 `deploy status` 的 drift 检测；suite 的环境路由、独立 run 目录、失败继续和中断；seed/readiness/warmup 开关默认关闭，启用基线 seed 必须启用 readiness，共享实例 deploy 仅 seed 一次，run 按身份逐用户 seed 并按需 warmup，保留 workflow 的 checkpoint readiness；压测映射与适配器能力校验、实际 Agent→指定记忆实例路由探针、共享插件作用域、跨 target 并发与限流、有限回放和稳态检索、用户身份/正确性/隔离门槛、运行锁、部分失败和分层报告；`undeploy` 的资源归属。命令集成测试使用可记录调用的 Docker CLI 替身，不要求开发机运行真实 Docker。具备实际镜像和数据集的环境再做真实 Compose 冒烟与端到端验证；没有这些资源时不宣称真实端到端已通过。
