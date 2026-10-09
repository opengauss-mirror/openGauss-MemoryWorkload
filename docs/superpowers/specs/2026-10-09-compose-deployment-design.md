# Compose 部署与原生 workflow 运行绑定设计

状态：部署与运行行为已在对话中确认；待审阅本文，再制定实施计划。

## 目标

使用一份 YAML 声明多个 Agent、记忆系统、可选向量数据库和可选图数据库模板，以及多组测试组合。`memory-bench deploy --config deployment.yaml` 为每组组合创建独立的容器和记忆数据卷。`memory-bench run --deployment <name>` 在同一次调用中依次运行所有组合，每组沿用现有平台原生 workflow、Skill 和报告结构。镜像可指定现有镜像名，也可同时给出本地 Docker 镜像 tar 包路径。未使用部署参数的现有 `run` 行为保持原样。

本功能管理本机 Docker Compose 上的测试环境。它不代替 Agent Skill、Memory Skill 或 Memory Plugin Skill；镜像和端口不能推断被测系统的请求协议。第一版不在一次组合中混用多个 Agent 或多个记忆系统，不自动扩缩容，不运行外部 benchmark runner，也不从任意镜像自动生成系统配置。

## 方案

平台从严格校验的 YAML 生成 Compose 文件，不要求各被测系统提供 Compose 文件。模板描述镜像、启动命令、端口、环境变量、配置挂载、状态挂载和健康检查；组合选择模板及 benchmark，并声明 `backend_direct` 或 `agent_plugin`。每个组合生成独立 Compose project；镜像层可以共用，容器、网络、状态目录和数据卷不能共用。可选向量数据库和图数据库属于该组合的记忆系统依赖。

相比直接拼接外部 Compose 文件，生成方式能保证实例命名、端口发现、数据隔离和运行绑定遵守同一契约。Compose 只负责容器生命周期，现有 Skill 继续负责调用和结果解析。

## YAML 契约

顶层字段为 `schema: deployment/1`、`name`、`agents`、`memories`、`vectors`（可省略）、`graphs`（可省略）和非空 `combinations`。标识符使用安全的短名称；未知字段、重复组合 ID、缺失引用和不匹配的 Skill 在启动 Docker 前报错。相对路径基于 YAML 所在目录解析。

每个服务模板声明：

- `skill`：Agent 或 Memory Skill ID；向量库和图数据库无 Skill 要求。
- `image.name`：Compose 使用的镜像名或 digest；`image.archive` 可选，为本地 tar 包。若给出 archive，先 `docker load`，随后验证所需镜像确实存在；若只给镜像名，交由 Docker 拉取或使用本地缓存。归档记录实际 image ID。
- `container_port`、`endpoint_scheme`（默认 `http`，图数据库可指定 `bolt` 等）和必需的容器健康检查；宿主端口仅绑定回环地址，交给 Docker 分配空闲端口。部署后从 Docker 查询实际端口，不预测端口号。
- 可选 `command`、非敏感的 `environment`、从宿主环境读取的 `environment_from`、只读 `config_mounts`、持久化的 `state_mounts`。引用的宿主环境变量在部署前检查；实际值不写进生成的 Compose 文件或平台部署记录。`state_mounts` 默认生成组合专属 Docker volume；需要从宿主侧读取 OpenClaw state 的挂载可显式标记 `host_visible: true`，其目录放在该组合部署目录内。
- Agent 和记忆系统模板的 `client.endpoint_env`：运行 Skill 所需的宿主侧 URL 环境变量名，例如 `OPENCLAW_GATEWAY_URL`、`MEM0_API_URL` 或 `OPENVIKING_API_URL`。`client.environment_from` 可传递运行 Skill 所需的凭据变量，但部署记录只保存变量名；向量库和图数据库模板不需要 `client`。

`service_endpoints` 将容器环境变量映射到同组合的 `agent`、`memory`、`vector` 或 `graph` 容器内地址。引用目标必须存在，使用目标模板的 `endpoint_scheme` 和 `container_port`；不能跨组合引用。平台自动生成 `memory -> vector`、`memory -> graph`、`agent -> memory` 的 Compose 健康依赖，其中不存在的服务不生成依赖。插件配置文件仍由使用者提供并通过只读挂载传入，平台不会猜测插件内部配置字段。

组合包含 `id`、`benchmark`、`agent`、`memory`、可选 `vector`、可选 `graph`、`memory_integration`、`data_path`、可选 `entrypoint` 和可选非敏感运行参数。benchmark 数据路径及 Skill 契约在部署前校验。组合的 `entrypoint` 必须是平台原生 scenario/case 入口；外部 runner 入口报错。`agent_plugin` 组合还必须能由现有 Skill 解析出对应的 Memory Plugin Skill。

示意结构（字段值需匹配实际镜像和 Skill）：

```yaml
schema: deployment/1
name: locomo-matrix
agents:
  claw:
    skill: openclaw
    image: {name: example/openclaw:tag, archive: ./images/openclaw.tar}
    container_port: 18789
    healthcheck: [CMD, node, /app/healthcheck.js]
    client: {endpoint_env: OPENCLAW_GATEWAY_URL}
memories:
  mem0:
    skill: mem0
    image: {name: example/mem0:tag, archive: ./images/mem0.tar}
    container_port: 8000
    healthcheck: [CMD, python, /app/healthcheck.py]
    state_mounts: [{target: /app/data}]
    client: {endpoint_env: MEM0_API_URL}
  graphmem:
    skill: example-graph-memory
    image: {name: example/graphmem:tag}
    container_port: 8080
    healthcheck: [CMD, /app/healthcheck]
    state_mounts: [{target: /app/data}]
    service_endpoints: {GRAPH_URI: graph}
    client: {endpoint_env: GRAPHMEM_API_URL}
vectors:
  milvus:
    image: {name: example/milvus:tag}
    container_port: 19530
    healthcheck: [CMD, /app/healthcheck]
    state_mounts: [{target: /var/lib/milvus}]
graphs:
  neo4j:
    image: {name: example/neo4j:tag}
    container_port: 7687
    endpoint_scheme: bolt
    healthcheck: [CMD, /app/healthcheck]
    state_mounts: [{target: /data}]
combinations:
  - id: claw-mem0
    benchmark: locomo
    agent: claw
    memory: mem0
    vector: milvus
    memory_integration: backend_direct
    data_path: ./data/locomo_small.json
  - id: claw-graphmem
    benchmark: locomo
    agent: claw
    memory: graphmem
    graph: neo4j
    memory_integration: backend_direct
    data_path: ./data/locomo_small.json
```

此示例只说明结构，不保证这些占位镜像包含所列命令。正式配置须明确模型服务地址、凭据引用及适配器配置。

## 部署状态与生命周期

`deploy` 先解析全部组合、验证文件和 Skill、检查镜像 archive 与环境引用，然后开始 Docker 操作。生成内容位于当前工作目录的 `.memory-bench/deployments/<name>/`，包括不含凭据的配置摘要、每组 Compose 文件和状态记录。此目录应由 Git 忽略。部署名在该工作目录内唯一；同名已存在时不重建或覆盖，要求先显式清理。

启动每组 project 后，等待其全部服务健康，记录 project 名、组合 ID、容器 ID、宿主回环端口、镜像 ID、配置哈希和创建时间。若任一组失败，停止并移除本次新建的所有 project 和数据卷，保留脱敏失败诊断。只有全部健康后才将部署状态标记为 ready。部署时不调用 benchmark，也不写入记忆。

提供 `memory-bench undeploy --deployment <name>`，仅停止状态记录中属于该部署的 Compose project，并移除这些 project 的数据卷及宿主可见状态目录。显式 `undeploy` 才删除成功部署的数据；`run` 不删除或重建容器。清理失败时保留状态记录和错误，便于重新执行清理。平台不操作记录之外的容器或卷。

## `run` 绑定和结果

`run --deployment <name>` 从当前工作目录读取 ready 状态，验证配置哈希、容器归属和健康状态，然后按 YAML 顺序运行组合。可用 `--combination <id>` 只运行一组。组合的 Agent/Memory Skill、集成模式、数据路径和原生 entrypoint 来自部署配置；同一调用不再要求 `--agent`、`--memory-backend` 等重复选择参数。部署模式与这些手动选择参数同时给出时直接报错，避免不一致。

每组以独立子进程调用现有 `memory-bench run` 原生路径。子进程只接收该组合的 Agent 与 Memory URL、明确声明的运行环境引用及原有用户环境；不修改父进程全局环境，也不把一组的端点带入另一组。每组使用唯一 run ID 和现有 `runs/<run-id>/` 归档。顶层 suite 摘要记录每组 run 目录、状态和部署标识；不合并不同组合的评分或耗时。单组失败仍尝试后续组合，最后返回非零；用户中断则停止后续组合，并保留已完成的 run 及部分 suite 摘要。

运行前健康检查证明服务可访问；实际用户/会话隔离和记忆写入正确性仍由 Skill 与 workflow gate 验证。相同镜像用于多个组合时，各组拥有独立记忆容器及向量库、图数据库容器和数据卷，因此测试数据不会因跨组复用而混杂。

## 验证

测试覆盖：严格 YAML 校验和路径解析；archive 加载后镜像名校验；多组合生成的 Compose project、卷、端口和依赖互不串用，包括可选图数据库的地址注入与健康依赖；启动失败时只清理本次资源；ready 状态与容器健康检查；suite 的环境路由、独立 run 目录、失败继续和中断；`undeploy` 的资源归属。命令集成测试使用可记录调用的 Docker CLI 替身，不要求开发机运行真实 Docker。具备实际镜像和数据集的环境再做真实 Compose 冒烟与端到端验证；没有这些资源时不宣称真实端到端已通过。
