# Compose 部署与原生 workflow 运行绑定设计

状态：部署与运行行为已在对话中确认；待审阅本文，再制定实施计划。

## 目标

使用一份 YAML 声明多个 Agent、记忆系统、可选向量数据库、可选图数据库和可选 Mock Server 模板，以及多组 Agent–记忆系统组合。`memory-bench deploy --config deployment.yaml` 为每组组合创建独立的容器和记忆数据卷。`memory-bench run --deployment <name>` 在同一次调用中依次运行所有组合，每组沿用现有平台原生 workflow、Skill 和报告结构；可选的 `pressure_tests` 将多个组合纳入同一场多用户并发压测。镜像可指定现有镜像名，也可同时给出本地 Docker 镜像 tar 包路径。未使用部署参数的现有 `run` 行为保持原样。

本功能管理本机 Docker Compose 上的测试环境。它不代替 Agent Skill、Memory Skill 或 Memory Plugin Skill；镜像和端口不能推断被测系统的请求协议。每个组合绑定一个 Agent 模板和一个记忆系统模板；一场压测可同时使用多个组合，因此可覆盖多个 Agent 和多个记忆系统。第一版不在单个组合内部混用多个 Agent 或多个记忆系统，不自动扩缩容，不运行外部 benchmark runner，也不从任意镜像自动生成系统配置。

## 方案

平台从严格校验的 YAML 生成 Compose 文件，不要求各被测系统提供 Compose 文件。模板描述镜像、启动命令、端口、环境变量、配置挂载、状态挂载和健康检查；组合选择模板及 benchmark，并声明 `backend_direct` 或 `agent_plugin`。每个组合生成独立 Compose project；镜像层可以共用，容器、网络、状态目录和数据卷不能共用。可选向量数据库、图数据库和 Mock Server 只在组合引用时创建。

相比直接拼接外部 Compose 文件，生成方式能保证实例命名、端口发现、数据隔离和运行绑定遵守同一契约。Compose 只负责容器生命周期，现有 Skill 继续负责调用和结果解析。

## YAML 契约

顶层字段为 `schema: deployment/1`、`name`、`agents`、`memories`、`vectors`（可省略）、`graphs`（可省略）、`mocks`（可省略）、非空 `combinations` 和可选 `pressure_tests`。标识符使用安全的短名称；未知字段、重复 ID、缺失引用和不匹配的 Skill 在启动 Docker 前报错。相对路径基于 YAML 所在目录解析。

每个服务模板声明：

- `skill`：Agent 或 Memory Skill ID；向量库、图数据库和 Mock Server 无 Skill 要求。
- `image.name`：Compose 使用的镜像名或 digest；`image.archive` 可选，为本地 tar 包。若给出 archive，先 `docker load`，随后验证所需镜像确实存在；若只给镜像名，交由 Docker 拉取或使用本地缓存。归档记录实际 image ID。
- `container_port`、`endpoint_scheme`（默认 `http`，图数据库可指定 `bolt` 等）和必需的容器健康检查；宿主端口仅绑定回环地址，交给 Docker 分配空闲端口。部署后从 Docker 查询实际端口，不预测端口号。
- 可选 `command`、非敏感的 `environment`、从宿主环境读取的 `environment_from`、只读 `config_mounts`、持久化的 `state_mounts`。引用的宿主环境变量在部署前检查；实际值不写进生成的 Compose 文件或平台部署记录。`state_mounts` 默认生成组合专属 Docker volume；需要从宿主侧读取 OpenClaw state 的挂载可显式标记 `host_visible: true`，其目录放在该组合部署目录内。
- Agent 和记忆系统模板的 `client.endpoint_env`：运行 Skill 所需的宿主侧 URL 环境变量名，例如 `OPENCLAW_GATEWAY_URL`、`MEM0_API_URL` 或 `OPENVIKING_API_URL`。`client.environment_from` 可传递运行 Skill 所需的凭据变量，但部署记录只保存变量名；向量库、图数据库和 Mock Server 模板不要求 `client`。
- Agent 和记忆系统模板的 `verify.command` 与 `verify.expected_version`：健康检查通过后，在容器内执行只读命令。命令输出 JSON，包含 `version`、实际配置的 `endpoints` 和逐依赖的 `reachable` 布尔值；平台精确比较版本和本组合生成的地址，并要求每个声明的依赖可达。缺少命令或检查不符时，该组合不能进入 ready。向量库、图数据库和 Mock Server 以容器健康检查为最低要求，也可配置同样的版本检查。
- 可选 `resources.cpus`、`resources.memory_mb` 和 `resources.numa_node`：前两项既作为容器限额，也参与启动前的 Docker 可用容量检查。NUMA 只在 Linux 主机显式启用；无法识别 NUMA 拓扑或资源不足时，在创建容器前失败。未指定资源时沿用 Docker 默认调度，不声称资源隔离。

Mock Server 模板沿用镜像、环境变量、只读配置挂载、状态挂载、健康检查和资源字段，并以非空 `endpoints` 映射声明一个或多个命名监听端口、协议及 `model_mode`（`replay-zero-delay`、`replay-with-delay` 或 `mock-fixed`），例如 `chat` 和 `embedding`。每个端口仅在宿主回环地址发布。它不需要 Agent/Memory Skill，也不要求 `verify.command`；被测 Agent 或记忆系统通过 `service_endpoints` 引用 `mock.<端点名>`。平台为引用方生成 Mock 健康依赖，并在引用方的 `verify.command` 中检查实际指向及可达性。Mock 模板可挂载只读 trace 或模型响应文件；平台不解释其内容。

`service_endpoints` 将容器环境变量映射到同组合的 `agent`、`memory`、`vector` 或 `graph` 容器内地址。引用目标必须存在，使用目标模板的 `endpoint_scheme` 和 `container_port`；不能跨组合引用。组合指定向量库或图数据库时，记忆系统模板必须声明相应地址映射；`agent_plugin` 组合还要求 Agent 模板声明记忆系统地址映射。平台自动生成 `memory -> vector`、`memory -> graph` 的 Compose 健康依赖，并在 `agent_plugin` 模式生成 `agent -> memory` 依赖。插件配置文件仍由使用者提供并通过只读挂载传入，平台不会猜测插件内部配置字段。

`service_endpoints` 引用 Mock 时使用 `mock.<端点名>`，由平台替换为该组合 Mock 容器的协议、服务名和命名端口。若模板引用 Mock 而组合未指定 `mock`，或引用不存在的端点，预检立即失败；若组合未指定 `mock`，该组合不创建 Mock 容器、卷或依赖。顶层定义但没有被任何组合引用的 Mock 模板也不会启动。

每组生成 `agent_net` 与 `storage_net`：Agent 默认只在前者，向量库和图数据库只在后者，记忆系统同时连接两者。需要 Agent 直连数据库的模板必须显式设置 `allow_agent_storage_access: true`；平台记录这一配置。部署核验在 `agent_plugin` 模式检查 Agent→记忆系统连通性，并始终检查记忆系统→已声明数据库连通性；不把容器进程健康当作依赖可达。镜像包 SHA256 与架构匹配校验不在本阶段范围内；仍校验 tar 可加载和目标镜像名存在。

Mock Server 仅接入引用方所在的网络：Agent 引用时接入 `agent_net`，记忆系统引用时接入 `storage_net`，两者都引用时接入两个网络。不同组合使用独立 Compose 网络，Mock 不加入无关网络。Mock 的镜像、健康状态、端口和资源限额与其他服务一起纳入部署状态、`deploy status` 和失败清理。

组合包含 `id`、`benchmark`、`agent`、`memory`、可选 `vector`、可选 `graph`、可选 `mock`、`memory_integration`、`data_path`、可选 `entrypoint` 和可选非敏感运行参数。benchmark 数据路径及 Skill 契约在部署前校验。组合的 `entrypoint` 必须是平台原生 scenario/case 入口；外部 runner 入口报错。`agent_plugin` 组合还必须能由现有 Skill 解析出对应的 Memory Plugin Skill。

组合可选 `preparation`。`seed` 和 `warmup` 各有 `enabled` 开关，默认 `false`；启用时必须提供目标服务角色和容器内 argv 命令。`seed_resource_boost` 可为该组向量库设置 seed 期间的临时 CPU/内存限额。`readiness` 有独立的 `enabled` 开关，默认 `false`；启用 seed 时必须同时启用 readiness，避免未完成索引就把部署标记为 ready。readiness 命令必须只读，输出 `{"ready": true, "details": {...}}`；平台在期限内轮询并要求配置的连续成功次数。各命令不经 shell；何为基线索引完成、如何构造基线数据由对应数据库/benchmark 脚本定义。此处的 readiness 检查部署基线；workflow 在每次写入后的 checkpoint readiness 仍属于 run，不能提前到 deploy。

`pressure_tests` 中每场压测声明 `id`、`max_workers`、`workloads`、`targets` 和非空 `users`。`targets` 通过唯一的 `combination` ID 引用已部署组合；组合中的 `agent`、`memory`、`memory_integration` 和可选数据库/Mock 引用就是 Agent–记忆系统及其依赖的权威映射，不在压测配置中重复填写另一套端点。多场压测可以引用同一组合，但同一部署同一时刻只允许运行一场测试，避免相互污染。一个组合只能在同一场压测的 `targets` 中出现一次。

每个 target 声明 `max_inflight`、`isolation_probe` 和该 Agent 回答模型的 `model_mode`（操作者声明）；每个用户声明唯一 `id`、`workload`、`target`、该 Agent 实例内唯一的 `agent_id`，以及可选速率/超时覆盖。workload 使用现有 `openmem-agent/1` 的 add/search JSONL 契约，路径相对部署 YAML；每用户请求串行，用户间及 target 间并发，受全局 `max_workers` 和目标 `max_inflight` 约束。一个 target 可以承载多个用户；同一个 Agent 或记忆系统模板被多个组合引用时仍实例化为不同容器与数据卷。压测必须引用 `agent_plugin` 组合，实际请求经该组合 Agent 再到其绑定的记忆系统；`backend_direct` 仍可用于普通 workflow，不能伪装成 Agent 压测。

部署前验证所有 target、workload 和用户引用，并检查 Agent Skill、Memory Plugin Skill 是否支持多用户请求、独立 `agent_id` 作用域、精确完成/就绪信号及所选协议。现有多用户回放只提供 OpenClaw 的 Agent 路径；支持其他 Agent Skill 是本设计的实施工作，需由该 Skill 提供明确的多用户请求适配器，不从镜像名推断能力。缺少能力的组合在启动 Docker 前报错，不静默改走记忆系统直连。

示意结构（字段值需匹配实际镜像和 Skill）：

```yaml
schema: deployment/1
name: locomo-matrix
agents:
  claw:
    skill: openclaw
    image: {name: "example/openclaw:tag", archive: ./images/openclaw.tar}
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
    workloads:
      support: {format: openmem-agent/1, add_file: ./data/support/add.jsonl, search_file: ./data/support/search.jsonl}
      developer: {format: openmem-agent/1, add_file: ./data/developer/add.jsonl, search_file: ./data/developer/search.jsonl}
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

此示例只说明结构，不保证这些占位镜像、Skill 或插件包含所列命令和多用户能力。正式配置须明确模型服务地址、凭据引用、插件路由及适配器配置。示例中 `claw-mem0` 和 `worker-graphmem` 分别把两个 Agent 模板绑定到两个记忆系统模板；两个 `claw-mem0` 用户共享该组合的容器，但有不同的逻辑 Agent 身份。

## 部署状态与生命周期

`deploy` 先解析全部组合和压测映射、验证数据文件及 Skill 能力、检查镜像 archive 与环境引用，然后开始 Docker 操作。生成内容位于当前工作目录的 `.memory-bench/deployments/<name>/`，包括不含凭据的配置摘要、每组 Compose 文件和状态记录。此目录应由 Git 忽略。部署名在该工作目录内唯一；同名已存在时不重建或覆盖，要求先显式清理。

在 Docker 操作前，汇总每组声明的 CPU/内存限额，与 Docker 运行环境可用资源比较；明确声明的 NUMA 位置也在此时规划并检查。容量不足直接失败，不先启动部分组合。未声明的资源不计入可保证容量。镜像 archive 仅验证存在、可加载并含所需镜像名；本阶段不计算 SHA256，也不校验镜像架构。

启动每组 project 后，等待其全部服务健康，执行版本、实际配置和依赖连通性核验。若 `seed.enabled: true`，在部署阶段按“临时提升向量库资源（若配置）→ seed → 索引/后端就绪轮询 → 恢复原始资源并核验 → 再次确认就绪”的顺序准备基线。若只启用 readiness，也在部署阶段确认现有数据已就绪。任何失败或中断都尝试恢复资源；恢复失败时不得标记 ready。记录 project 名、组合 ID、容器 ID、宿主回环端口、镜像 ID、资源限额、配置哈希、seed/就绪结果和创建时间。若任一组失败，停止并移除本次新建的所有 project 和数据卷，保留脱敏失败诊断。只有全部检查通过后才将部署状态标记为 ready。部署不运行 benchmark；只有明确启用 seed 时才写入基线数据，且每个新部署只执行一次。

`memory-bench deploy status --deployment <name>` 重新读取 Docker 实际状态，显示每组服务健康、端口、镜像 ID、资源限额、配置指纹和 seed/就绪状态；启用了 readiness 的组合重新执行只读就绪检查。容器缺失、被替换、镜像改变、配置核验或就绪检查失败时报告 drift，并给出非零退出码。状态输出不包含凭据。`run --deployment` 使用同一状态核验，不信任旧的 ready 标记，也不重新 seed。

提供 `memory-bench undeploy --deployment <name>`，仅停止状态记录中属于该部署的 Compose project，并移除这些 project 的数据卷及宿主可见状态目录。显式 `undeploy` 才删除成功部署的数据；`run` 不删除或重建容器。清理失败时保留状态记录和错误，便于重新执行清理。平台不操作记录之外的容器或卷。

## `run` 绑定和结果

`run --deployment <name>` 从当前工作目录读取 ready 状态，验证配置哈希、容器归属和健康状态，然后按 YAML 顺序运行组合。可用 `--combination <id>` 只运行一组。组合的 Agent/Memory Skill、集成模式、数据路径和原生 entrypoint 来自部署配置；同一调用不再要求 `--agent`、`--memory-backend` 等重复选择参数。部署模式与这些手动选择参数同时给出时直接报错，避免不一致。`--pressure-test <id>` 选择一场多用户压测，不能与 `--combination` 同用；未指定时仍按顺序运行普通 workflow，不隐式启动压测。

每组以独立子进程调用现有 `memory-bench run` 原生路径。子进程只接收该组合的 Agent 与 Memory URL、明确声明的运行环境引用及原有用户环境；不修改父进程全局环境，也不把一组的端点带入另一组。每组使用唯一 run ID 和现有 `runs/<run-id>/` 归档。顶层 suite 摘要记录每组 run 目录、状态和部署标识；不合并不同组合的评分或耗时。单组失败仍尝试后续组合，最后返回非零；用户中断则停止后续组合，并保留已完成的 run 及部分 suite 摘要。

若组合使用 Mock，run 记录被注入的 Mock 端点、对应消费方和各端点 `model_mode`，以区分真实模型与回放或固定响应；未配置 Mock 的组合不设置 Mock 端点，也不启动 Mock。Mock 的健康检查只证明服务可用，不代表回放内容匹配或计数正确；这些仍由相应测试的运行期校验判断。

若 `warmup.enabled: true`，每次 run 在状态/基线就绪复核通过后、正式 workflow 前执行该组合的 warmup；否则直接开始 workflow。warmup 的耗时与结构化结果归档在本次 run 中，不计入正式负载指标。warmup 必须使用与正式测试隔离的身份或数据范围，具体约束由组合的准备脚本实现并在报告中声明；平台不把一次 warmup 成功解释为记忆结果正确。seed 和基线索引构建属于部署阶段，run 不重复执行；正式 workflow 的逐次写入、commit 和 checkpoint readiness 继续在 run 中执行。重复运行同一部署若可能留下被测数据，比较实验应创建新部署以恢复相同基线。

运行前健康检查证明服务可访问；实际用户/会话隔离和记忆写入正确性仍由 Skill 与 workflow gate 验证。相同镜像用于多个组合时，各组拥有独立记忆容器及所选向量库、图数据库、Mock Server 容器和数据卷，因此测试数据不会因跨组复用而混杂。

## 多用户、多 Agent 与多记忆系统压测

`memory-bench run --deployment <name> --pressure-test <id>` 根据 `targets[].combination` 获取每组真实容器端点、Agent Skill、Memory Plugin Skill 和部署状态。平台扩展现有多用户回放协调器，使一场运行能同时调度不同 Agent Skill 的 target；每个请求携带所属用户的 `agent_id` 和由 run ID、用户 ID 派生的独立用户/会话身份。每个 Agent Skill 的多用户适配器必须处理 `openmem-agent/1` 的 add/search 请求，并返回可关联的请求结果；对应插件提供 prepare、阶段切换、精确写入完成/就绪和 finalize。平台对每个 target 分别执行这些生命周期动作，绝不把一个 target 的端点、环境或插件状态传给另一个 target。

启动负载前，依次复核部署健康与基线 readiness、执行各 target 插件 prepare、运行使用独立哨兵作用域的隔离探针，并按各组合的 `warmup.enabled` 执行隔离的 warmup。探针接收本场 run ID、target/组合 ID、用户与 Agent ID 列表及其他 target 的标识，必须实际验证同 target 用户之间以及不同 target 之间的写入/检索隔离，并输出与现有多用户隔离探针相同的结构化证据；只有对应 Skill/插件能提供该证据时才允许压测。探针和 warmup 完成后使用同一负载起点，跨所有 target 并发执行 add → 精确 drain/就绪 → search；任一 target 的准备失败时不向该 target 发负载，其他 target 可继续，整场退出非零。单用户请求按原有配置顺序串行，不把并发 QA/Judge 参数当作压测并发度。

一次压测独占其引用的部署组合，并在本地部署状态中持有运行锁；第二次 `run` 或 `undeploy` 在锁占用期间报错，异常退出时根据进程存活和部署状态恢复陈旧锁。源 JSONL 在写入前复制到权限受控的临时目录并校验，报告只保存输入指纹和数值摘要。重复使用同一部署会保留上次测试写入；需要相同初始状态作比较时创建新部署。`seed` 仍只在 deploy 执行一次，不随着压测用户数重复执行。

压测输出仍位于唯一的 `runs/<run-id>/`，包含脱敏的用户→target→组合→Agent/记忆系统映射、每用户和每 target 的请求数、成功率、QPS、p50/p95/p99、超时与失败、整体共同阶段窗口指标，以及各 target 声明的 Agent `model_mode`、实际 Mock 端点及其模式、探针、warmup、drain/就绪证据。Agent 模型模式是操作者声明，不因记忆系统使用 Mock 就自动变成 Mock 模式；不能用一个全局值掩盖不同 target 的模型依赖。Agent 端到端延迟与记忆 API 延迟不得合并比较；本压测所有 target 均走 Agent 路径。单 target 失败仍保留其他 target 的事件和部分报告，最终返回非零；中断返回 130 并保留已完成事件。每用户和每 target 的分位数由原始事件计算，不平均分位数。

## 验证

测试覆盖：严格 YAML 校验和路径解析；archive 加载后镜像名校验；多组合生成的 Compose project、卷、端口和依赖互不串用，包括可选图数据库与 Mock Server 的地址注入、命名端口、健康依赖及 Agent/存储网络隔离；未配置 Mock 时不创建相关容器；资源预检在容器启动前失败；启动失败时只清理本次资源；ready 状态、版本/配置/连通性核验与 `deploy status` 的 drift 检测；suite 的环境路由、独立 run 目录、失败继续和中断；seed/readiness/warmup 开关默认关闭，启用 seed 必须启用 readiness，deploy 仅 seed 一次并等待基线索引、失败时恢复资源，run 每次按需 warmup 且保留 workflow 的 checkpoint readiness；压测映射的引用和 Skill 能力校验、跨 target 并发与各自限流、用户身份及端点隔离、探针失败、运行锁、部分失败和分层报告；`undeploy` 的资源归属。命令集成测试使用可记录调用的 Docker CLI 替身，不要求开发机运行真实 Docker。具备实际镜像和数据集的环境再做真实 Compose 冒烟与端到端验证；没有这些资源时不宣称真实端到端已通过。
