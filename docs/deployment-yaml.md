# Compose 部署配置

`memory-bench deploy` 从一份 YAML 创建本机 Docker Compose 测试环境。需要 Docker Compose v2、可用的镜像、已存在的 Agent/Memory/Benchmark Skill 和实际测试数据。服务镜像需提供健康检查所需的程序，以及返回结构化 JSON 的只读 `verify.command`。部署命令本身不构造被测系统的配置文件；它从提供的 JSON/YAML 配置源文件生成各实例的副本。

```bash
memory-bench deploy --config deployment.yaml \
  --agent-config 'claw-mem0:log_level="debug"' \
  --memory-config 'claw-mem0:workers=4'
memory-bench deploy status --deployment locomo-matrix
memory-bench run --deployment locomo-matrix
memory-bench run --deployment locomo-matrix --combination claw-mem0
memory-bench undeploy --deployment locomo-matrix
```

`--agent-config` 与 `--memory-config` 可重复。格式是 `<实例 ID>:<参数名>=<JSON 值>`。Agent 实例 ID 是组合 ID；独立记忆实例 ID 也是组合 ID；共享记忆实例 ID 是 `shared_memory_instances` 的键。值按 JSON 解析，因此字符串要包含双引号，数字和布尔值保持原类型。配置中未列出的字段不能从 CLI 修改；重复参数、未知实例或类型不匹配会在启动容器前报错。CLI 参数只用于非敏感设置，凭据应经 `environment_from` 传入。

最小结构示例（镜像、配置路径和 verify 命令是示意，须替换为实际可用值）：

```yaml
schema: deployment/1
name: locomo-matrix
agents:
  claw:
    skill: openclaw
    image: {name: example/openclaw:tag, archive: ./images/openclaw.tar}
    container_port: 18789
    healthcheck: [CMD, node, /app/healthcheck.js]
    service_endpoints: {MEMORY_URL: memory}
    config_files:
      runtime:
        source: ./configs/openclaw.json
        target: /runtime/openclaw.json
        format: json
        parameters:
          log_level: {pointer: /logging/level, type: string}
    verify: {command: [node, /app/verify-runtime.js], expected_version: v1}
    client: {endpoint_env: OPENCLAW_GATEWAY_URL}
memories:
  mem0:
    skill: mem0
    image: {name: example/mem0:tag}
    container_port: 8000
    healthcheck: [CMD, python, /app/healthcheck.py]
    config_files:
      runtime:
        source: ./configs/mem0.yaml
        target: /app/config.yaml
        format: yaml
        parameters:
          workers: {pointer: /server/workers, type: integer}
    state_mounts: [{target: /app/data}]
    verify: {command: [python, /app/verify_runtime.py], expected_version: v1}
    client: {endpoint_env: MEM0_API_URL}
combinations:
  - id: claw-mem0
    benchmark: locomo
    agent: claw
    memory: mem0
    memory_integration: agent_plugin
    data_path: ./data/locomo_small.json
```

### OpenClaw Gateway Pool

一个组合需要多个 OpenClaw Gateway 时，可在同一部署中启用 Gateway Pool。以下字段是在上方 YAML 示例中的增量配置；Pool 镜像必须包含与参考项目 `gateway_pool.py` 的 `--host`、`--port`、`--upstream`、`--expected` 参数兼容的程序，健康检查需验证 `/pool/status`。上方 `agents.claw.client.endpoint_env` 应设为 `OPENCLAW_GATEWAY_URL`。

```yaml
gateway_pools:
  claw-pool:
    kind: openclaw_http_pool_v1
    image: {name: example/runner-with-gateway-pool:tag, archive: ./images/runner.tar}
    command: [python, /workspace/scripts/gateway_pool.py]
    container_port: 29789
    healthcheck: [CMD, python, /workspace/scripts/check_pool.py]
    resources: {cpus: 1, memory_mb: 256}
combinations:
  - id: claw-mem0
    benchmark: locomo
    agent: claw
    agent_replicas: 3
    gateway_pool: claw-pool
    memory: mem0
    memory_integration: agent_plugin
    data_path: ./data/locomo_small.json
```

`deploy` 给三个 Gateway 分别生成配置副本和 state 卷，并在其健康后启动 Pool；`run --deployment ... --combination claw-mem0` 访问 Pool 的宿主回环端口。`--agent-config 'claw-mem0:log_level="debug"'` 对三个副本应用相同的允许字段覆盖。每个组合拥有独立 Pool；即使多个组合共用记忆实例，也不会共用 Pool 的用户分配表。部署和运行会核对 Pool 上游、容器重启及配置漂移；报告只保留脱敏的用户分配和各上游请求量。未配置 `gateway_pool` 的组合仍使用一个 Agent 容器。启用 Pool 的组合不能使用指向 Agent 容器的 `preparation.seed` 或 `preparation.warmup` 命令，因为容器内命令不会经过 Pool。当前 `pressure_tests` 仍由部署命令拒绝，多用户压测需要单独实现。

### OpenClaw IPv4 提供方域名

若 OpenClaw 容器所在网络只能访问 IPv4，而模型提供方域名同时返回 A 和 AAAA 记录，可在对应 Agent 模板中显式设置域名列表：

```yaml
agents:
  claw:
    environment:
      MEMORY_BENCH_IPV4_ONLY_HOSTS: codex.example.com,embedding.example.com
```

设置后，`deploy` 将仓库中的 `skills/agents/openclaw/scripts/ipv4_provider_dns.cjs` 只读挂载到每个 OpenClaw 容器的 `/opt/memory-bench/ipv4_provider_dns.cjs`，并在已有 `NODE_OPTIONS` 后追加 `--require=/opt/memory-bench/ipv4_provider_dns.cjs`。Node 启动时会预加载脚本，仅对列表中的域名强制 IPv4 查询；未配置时不会挂载或预加载。部署后可检查生成的 Compose 配置中该容器的 `environment.NODE_OPTIONS` 和 `volumes`，并在容器内运行 `node -e 'require("node:dns").lookup("codex.example.com", {all:true}, (error, addresses) => { if (error) throw error; console.log(addresses) })'` 验证返回的地址族。域名应替换为实际提供方域名，且该域名需要有可用的 A 记录。

`image.archive` 可省略；指定时先执行 `docker load`，再检查镜像名。当前不校验 archive SHA256 或架构。相对路径以部署 YAML 所在目录为基准。`config_files` 只支持 JSON/YAML、已有字段的 RFC 6901 路径，以及 `string`、`integer`、`number`、`boolean` 四种标量类型。每个物理实例的配置副本只读挂载到容器，原文件不变。生成目录默认位于当前工作目录的 `.memory-bench/deployments/<name>/`，清理部署时删除。

默认配置副本权限为 `0600`。以非 root 用户运行的容器可在对应 `config_files` 项中设置 `mode: '0644'`；生成目录仍为 `0700`，副本只读挂载，但容器内其他用户也能读取该文件。仅在文件不含明文凭据时使用。

Docker 默认地址池耗尽时，可用 `network_subnets` 给每个组合组的网络指定未占用的 IPv4 网段。例如 `network_subnets: {claw-mem0: {storage_net: 10.253.246.0/28, agent_net_claw_mem0: 10.253.247.0/28}}`。组键为组合 ID（共享记忆时为共享实例 ID），Agent 网络名由组合 ID 中的 `-` 转为 `_`。未指定的网络仍由 Docker 自动分配；配置校验会拒绝未知网络、无效网段、配置内重叠网段，以及扣除网络地址、广播地址和网关地址后不足以容纳实际服务数的网段。

可选的 `vectors`、`graphs`、`mocks` 与组合中的 `vector`、`graph`、`mock` 引用按需要创建。数据库模板使用与服务相同的 `image`、`container_port`、`healthcheck`、`state_mounts` 和 `resources` 字段。Mock 模板使用 `endpoints` 声明命名端口，例如 `chat`、`embedding`；被组合或共享记忆实例选中的 Mock 会启动，顶层未被选中的 Mock 模板不会启动。服务通过 `service_endpoints` 的 `mock.chat` 等名字接入其端点。记忆系统引用向量库或图数据库时，必须声明对应 `service_endpoints`。Agent 插件模式须声明 `memory` 引用。宿主端口只绑定 127.0.0.1，部署后查询实际端口。

默认每个组合有独立 Agent、记忆实例和数据卷。若几个组合引用同一个 `shared_memory_instances` 键，它们共用一套记忆实例、数据库和数据卷；Agent 仍各自独立。共享实例的 CLI 配置覆盖只接受该共享实例 ID。`run --deployment` 默认依 YAML 顺序执行全部组合；`--combination` 只执行指定组合。共享实例的后续组合可看到前一组写入的数据，报告保留运行顺序和物理实例 ID。

`preparation.seed.enabled` 和 `preparation.warmup.enabled` 默认为 `false`。启用基线 seed 时必须同时启用 `preparation.readiness`；基线 seed 与索引就绪在 deploy 执行，warmup 在每次 run 的正式 workflow 前执行。共享记忆实例的基线准备配置放在该实例下，只执行一次。压测用户身份相关的 seed 仍属于 run 准备阶段。当前 CLI 会显式拒绝 `pressure_tests`；多 Agent、多用户并发压测按单独的实施计划接入。

### 复用 Milvus Golden Volume

对于使用**单个 Docker 命名卷**保存完整 Milvus 状态的部署，可在物理记忆实例的 `preparation` 中配置 `golden_volume`。首次 `deploy` 执行 seed、索引就绪检查和 flush/compaction，停止 Memory 与 Vector 容器后复制卷并写入基线清单；随后重新启动服务并复查就绪。以后使用相同 Golden 名称部署时，平台先校验基线内容，再把 Golden 只读复制到新的部署专属 Runtime 卷，跳过 seed，并在运行前检查索引就绪。`undeploy` 只删除 Runtime 卷，保留 Golden 卷。

```yaml
vectors:
  milvus:
    image: {name: example/milvus:tag}
    container_port: 19530
    healthcheck: [CMD, /app/check-health]
    state_mounts: [{target: /var/lib/milvus}]
shared_memory_instances:
  mem0-common:
    memory: mem0
    vector: milvus
    preparation:
      seed: {enabled: true, target: memory, command: [python, /app/seed.py], timeout_seconds: 3600}
      readiness: {enabled: true, target: memory, command: [python, /app/index_ready.py]}
      golden_volume:
        name: milvus_locomo_golden
        helper_image: python:3.11-alpine
        collection: memory_001
        users: [user-001, user-002]
        count_per_user: 10000
        dimensions: 1024
        seed_version: seed-v1
        index: {type: HNSW, metric: L2, M: 16, efConstruction: 200}
        finalize: {enabled: true, target: memory, command: [python, /app/compact.py], timeout_seconds: 3600}
```

上例的命令和镜像仅示意：`seed` 必须返回 `inserted`、`deleted_dynamic: 0`、`deleted_seed: 0`，以及每个用户的 `user_id`、`validated_count` 和 `reused: false`；`finalize` 必须在 flush 和 compaction 完成后返回 `{"status":"complete","collections":[{"collection":"memory_001","state":"Completed"}]}`。`readiness` 必须验证当前镜像下 HNSW 索引已加载且稳定，并返回 `{"ready":true}`。辅助镜像需要 `python3`、`sh`、`cp -a`、`du`、`df` 和 `sync`。确保 Milvus 的全部持久化状态确实位于该单一命名卷；分散在多个卷或宿主目录的部署不能使用此配置。

匹配时比较 collection、用户列表、每用户行数、维度、seed 版本和索引参数；服务名称及 Milvus 镜像 ID 只记录在清单中，不阻止复用。封存时记录卷内全部目录、文件内容和符号链接的 SHA-256 摘要；复用前及复制后再次校验。旧版清单没有摘要，不能复用，需要使用新的 Golden 卷名重新构建。部署开始前也会拒绝已存在的同名 Runtime 卷，避免使用或清理遗留数据。换镜像后的 Runtime 卷仍须通过当前部署的健康检查和索引就绪检查。Golden 卷不完整或基线不匹配时，部署在写入前失败；需要重建基线时请使用新的 Golden 卷名。`state.json` 的 `preparation.<实例>.golden_volume` 记录本次是 `built` 还是 `reused`、Runtime 卷名及复制耗时。对于已部署的 Mem0，可从 `.memory-bench/deployments/<部署名>/state.json` 中读取 `ports.<实例>.memory["8000/tcp"]`，设为 `MEM0_API_URL=http://127.0.0.1:<端口>`，再运行 `production-mem0-sample` 的 trace replay。回放产生的写入只进入 Runtime 卷；若要再次从同一基线测试，先 `undeploy` 并重新 `deploy`，不要复用已写入的 Runtime 卷。

Agent/Memory 的 `verify.command` 返回 JSON，至少包含 `version`、`endpoints`、`reachable` 和 `effective_parameters`。平台用它核对实际版本、依赖地址、连通性及允许覆盖的配置项生效值。敏感配置内容和 CLI 值不会写入状态或日志。`deploy status` 与 `run --deployment` 都复核容器、镜像、配置文件哈希和实际生效配置；发现漂移时拒绝运行。`undeploy` 仅清理部署状态记录归属的 Compose project、数据卷和生成文件。
