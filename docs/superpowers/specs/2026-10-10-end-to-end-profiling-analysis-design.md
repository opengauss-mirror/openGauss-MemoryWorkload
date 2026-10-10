# 端到端阶段化 Profiling 与性能分析闭环设计

状态：待评审。本文定义目标行为和接口，不代表现有代码已经实现。

## 1. 目标与边界

目标是在同一次受控运行中完成：`deploy → verify → prepare → warmup → 分阶段运行 → perf/Topdown 采集 → 内部打点导入 → 关联与归因 → 报告 → 安全清理`。使用者只指定部署组合或多用户负载、采集策略和打点来源；平台自动管理采集器生命周期，输出可复核的原始证据与基础瓶颈报告。

成功标准：

1. 每个被测阶段有明确开始/结束事件、目标物理实例、负载统计和采集状态；采集失败不会伪装成零值。
2. Linux 上具备权限和 PMU 能力时，可以对指定容器的整个进程组/cgroup 按阶段运行 `perf stat`，采集基础硬件计数器或 Topdown；可选对选定阶段运行 `perf record` 生成热点证据。
3. 导入记忆系统的结构化 span，以 `run_id + instance_id + request_id` 关联客户端事件，按非重叠 leaf 阶段计算写入/检索分解和残差。
4. 报告同时展示吞吐、延迟、成功率、内部阶段、硬件指标、关联覆盖率与采集限制，并能从归档数据离线重建。
5. 部署、运行、分析任何一步失败都留下明确状态；平台仅停止自己创建的采集进程，不清理外部部署或原始日志。

首版 Linux-only `perf`；macOS 等平台运行负载但将硬件采集标记为 `skipped: unsupported_platform`。不自动修改被测系统源码、不开启特权容器、不把外部模型等待时间解释为 CPU 瓶颈，也不自动给出无证据的优化建议。自动打点 Skill 是上游接入手段，本文只规定它输出的 trace 契约及覆盖验证。

## 2. 现状与方案选择

现有 `deployment_lifecycle.py`、`deployment_run.py` 支持 Compose 部署、就绪校验和普通组合运行；`multi_user_replay.py` 支持 add/drain/search，但与 deployment 尚未打通。`ResourceMonitor` 对宿主 `/proc` 做整轮 CPU/内存/磁盘/网络采样，不能按物理实例归因，亦无 `perf`/Topdown。`result_analysis.py` 的阶段资源标记来自部分 LoCoMo 诊断时长，不是实际时钟锚点。`official_small_timing.py` 能分析特定格式的时长；通用真实负载仅检查内部 request ID 覆盖率，报告将内部阶段和硬件标为 unavailable/skipped。`hardware_profile` 目前主要记录配置，不能作为采集已执行的证据。

比较三种做法：

| 做法 | 优点 | 代价/风险 |
|---|---|---|
| 在每个 benchmark runner 内直接调用 perf | 单路径最短 | 生命周期、权限与报告解析重复；外部 runner 和多用户实现会分叉 |
| 在部署外用 shell 包裹整个运行 | 对现有代码侵入少 | 无可靠阶段边界与容器身份；异步 drain 和多实例无法归因 |
| 统一阶段事件 + 独立采集模块 + 离线分析模块（选用） | runner 只声明阶段；采集器和报告可复用，支持离线复算 | 需明确跨模块契约并逐条接入运行路径 |

选用第三种。接口保持小：runner 发阶段事件；采集模块接受阶段与目标映射；分析模块只读取不可变归档。`ResourceMonitor` 保留为宿主粗粒度背景数据，不把其结果冒充容器 perf。

## 3. 运行范围与阶段模型

`run --deployment NAME [--combination ID] --profiling-config FILE` 使用已部署且通过 drift 检查的组合。`run --benchmark production-http-replay --workload-config FILE --profiling-config FILE` 支持现有外部/托管多用户实例。两种入口归档同一阶段/采集格式。部署配置中的多 Agent `pressure_tests` 以已有 [Compose 压测设计](2026-10-09-compose-deployment-design.md) 和 [实施计划](../plans/2026-10-09-multi-agent-pressure-plan.md) 为前置：其 `run --deployment NAME --pressure-test ID` 接入同一阶段与采集接口，按 `targets[].combination` 解析物理 Agent/Memory/数据库实例，不能复制一套 profiler 或改走 backend_direct。该前置尚未实现时，当前拒绝分支必须保持，不能把“部署后多用户 profiling”宣称为已闭环。已有 `deploy` 与 `run --deployment` 是两个显式命令；提供新 `experiment --config DEPLOYMENT.yaml --profiling-config FILE [--pressure-test ID]` 串行编排 `deploy → run → analyze → 可选 undeploy`，默认保留部署供检查，只有 `--cleanup-owned-deployment` 才删除本命令创建的部署。若部署原先已存在，该选项也不得删除它。

阶段使用统一 `phase_id`，不是靠时长累加推断：

| 阶段 | 开始/结束边界 | 默认测量？ | 说明 |
|---|---|---|---|
| `setup` | 部署就绪后至准备结束 | 否 | 包含 seed/readiness；可单独开启 profiling |
| `warmup` | 首个预热请求前至预热 drain 后 | 否 | 不进入正式延迟/吞吐统计 |
| `ingest` | 第一条正式 add 即将提交前至最后一条 add 客户端完成 | 是 | 不把异步后台完成混入 add 客户端延迟 |
| `drain` | ingest 结束至全部精确提交/索引就绪 | 是 | backend_direct 若逐请求已 drain，记录 `not_applicable`；不能用固定 sleep 伪造就绪 |
| `recall` | 第一条正式 search/QA 提交前至最后一条完成 | 是 | QA/answer/judge 若存在，另有 `qa`、`judge` 子阶段；单一请求可能跨内部阶段 |
| `cooldown` | 正式流量结束至采集器停止 | 否 | 用于观察尾部后台活动，独立展示 |

phase 事件至少含 `schema`, `run_id`, `phase_id`, `target_id`, `start_utc`, `end_utc`, `start_monotonic_ns`, `end_monotonic_ns`, `state`, `request_count`, `warmup`。每个目标的阶段窗口是半开区间 `[start,end)`；全局阶段窗口是各目标并集，不将不同实例时间相加。采集器先返回 `ready` 才可打开正式窗口；启动延迟进入 `collector_start_ms`，不计入负载阶段。停止时立即封闭窗口，再等待采集器退出。重叠阶段只能属于不同目标或显式的父/子关系；不能把父、子时长相加。多用户同一物理实例共享一个采集目标，不按用户重复运行 perf；用户级性能仍来自请求事件。

## 4. 配置与目标身份

新增独立、严格校验的 `profiling/1` YAML；部署 YAML 和 `multi-user-replay/1` 不保存特权命令或机器私有路径。配置示例：

```yaml
schema: profiling/1
mode: best_effort              # best_effort | required
phases: [ingest, drain, recall]
targets: [memory, vector]      # 组合逻辑角色；可用 instance ID 精确限定
hardware:
  stat: basic                  # off | basic | topdown_l1
  interval_ms: 1000
  record: off                  # off | selected_phases
  record_phases: []
  max_raw_bytes: 1073741824
internal_trace:
  source: adapter             # adapter | file | off
  max_raw_bytes: 1073741824
quality_gates:
  min_request_join_rate: 0.95
  min_counter_running_fraction: 0.90
```

`mode: required` 表示所选目标/阶段的硬件采集不可启动、严重复用或损坏时运行结论为 `invalid_profile`，但已经发送的请求仍据实归档；`best_effort` 继续执行并标明缺口。宿主能力在 deploy 前预检，容器目标在 deploy 后、首个正式请求前复检；现有 deploy 可能执行基线 seed，因此 `required` 不承诺在任何部署写入前失败，只保证不在不可采集状态下发送正式测量请求。`internal_trace` 独立于硬件 mode：未提供 trace 时基础负载报告仍有效，但不可声称内部阶段归因。显式 `min_request_join_rate` 仅是用户设定的归因门槛；未达到时归因结果 invalid，不把客户端性能数据改成成功的内部分析。

目标注册表由运行前实际部署状态生成，字段包括 `target_id`, `role`, `physical_instance_id`, `deployment_kind`, `container_id`, `cgroup_path`, `host_pid`, `image_digest`, `restart_count`, `owner`。配置中的 role 解析到唯一物理实例；共享 memory/vector 只登记一次。外部实例须显式配置宿主 PID 或 cgroup 路径，并验证归属；缺少目标身份只能运行客户端负载，profiling 标记 skipped。每一阶段开始与结束核对容器 ID、启动时间和 restart count；中途重启使该目标的计数器无效，不能把新旧进程拼成一条样本。

## 5. Perf/Topdown 采集模块

定义 `ProfilerSession.start_phase(phase, targets) -> collector_handles` 与 `stop_phase(handles) -> collector_status` 两个调用面；内部适配器可为 Linux cgroup perf 或 `unavailable`。默认选择宿主机 cgroup v2 作用域，以覆盖容器子进程；不能可靠解析时拒绝自动退化为单 PID。单 PID 附加只允许显式 `exploratory` 策略，报告必须标记 `partial_process_coverage`。

采集前 probe：Linux 与 `perf` 版本、cgroup v2、容器 cgroup 路径、`perf_event_paranoid`/能力、所需基础事件、TopdownL1 指标支持、目标 CPU 集、容器 drift。Topdown 不可用时不自动改名为 Topdown；`best_effort` 可保留 basic counters 并记录降级，`required` 则在首个正式测量请求前阻止运行。记录失败类型和工具版本，不输出原始凭据或完整环境变量。

`basic` 收集 cycles、instructions、task-clock、cache/branch 指标（实际支持集合由 probe 决定）；`topdown_l1` 收集 Frontend Bound、Bad Speculation、Backend Bound、Retiring 及所需原始事件。每个目标/阶段启动独立 `perf stat`，解析 `-x` 机器格式或固定 JSON 格式，保存原始 stderr 和规范化 CSV/JSON。必须保留 `time_enabled/time_running` 或 perf 提供的 running 百分比、not-supported/not-counted 标记；运行比例低于门槛时该指标 invalid，不用缩放值给出确定瓶颈判断。Topdown 四类占比只在同一可比窗口、同一 CPU 作用域和计数有效时展示，并检查总和容差（默认 95%–105%）；跨阶段只能比较相同 target/事件组配置。`perf record` 仅对选定阶段采样，限制文件大小，原始 `perf.data` 可能暴露符号/路径，保存在权限受控的本地归档，不默认上传或嵌入 HTML。

collector 状态机：`planned → probing → ready → running → stopping → completed`，以及 `skipped/failed/invalid`。异常、中断和超时都在 `finally` 停止平台启动的进程组，等待退出并持久化部分原始输出。不得 `sudo`、改变内核 sysctl 或自动提升 Docker 权限。阶段请求已开始但 perf 后发故障时，不重放写请求，只标记该窗口采集无效。

## 6. 记忆系统打点导入与时间关联

接受现有 `[MEM_PERF]`/纯 JSONL，每行必须有 `kind=memory_perf`, `ts`（带时区）, `operation`, `stage`, `span_role=leaf|wrapper`, `duration_ms`, `status`, `request_id`。建议补充 `start_utc/end_utc` 或 `start_monotonic_ns/end_monotonic_ns`、`run_id`, `instance_id`, `trace_id`, `task_id`, `parent_span_id`, `span_id`。跨机器不能比较 monotonic 时间；时钟偏差由部署前/后时钟 probe 估计并归档。没有可信时间锚点时可以按 request ID 做时长归因，但不得把 span 硬配到某个 perf 时间桶。异步任务由 `task_id` 连接原始请求，后台 span 可落在 drain，不因响应已返回就丢弃。

导入适配器只处理用户指定或 Skill 声明的日志来源；在运行开始记下文件偏移/轮转标识，结束后只导入本轮时间与 ID 范围。原始日志可能含业务内容，默认不复制到公开报告；先校验/脱敏后将数值、ID、stage、状态和时间写入 `perf_trace_events.jsonl`。字段过大、重复 span ID、负时长、未知 stage、缺失 ID 分别计数，不静默跳过。用户、租户与实例标识必须参与 join，防止跨用户同名 request ID 串接。

stage map 来自打点 Skill 的版本化 manifest：系统原名 → canonical category，注明 leaf/wrapper、父子关系、是否异步、预期操作。写入类别为 preprocess / extract LLM / vectorize / memory write；检索类别为 query vectorize / vector search / keyword search / fusion / rerank / format。未知 leaf 归 `other_leaf`，未知 wrapper 仅诊断。每请求优先计算非重叠 leaf 之和；父 wrapper 的子项缺口记录 `unattributed_ms`，服务总时长与客户端 wall time 的差额另记 `client_service_gap_ms`。禁止将父 wrapper 与子 leaf 同时加入占比分母。即使 trace 文件存在，join 覆盖率不足也只输出探索性结果。

硬件计数器归因单位是“目标物理实例 × 阶段窗口”，不是单条请求。对同一窗口汇总的 memory span 与 perf 指标只能并列比较；不能把共享 cgroup 的 cycles 精确摊给用户或 LLM/向量库调用。报告应明确这一归因粒度。

## 7. 分析与报告

分析器纯离线读取归档，不调用被测服务。首先校验 run/config/phase/request/collector/trace schema 与引用关系，然后生成：

```text
runs/<run-id>/
  run_config.json
  request_events.jsonl
  phase_events.jsonl
  target_registry.json
  profiling/collector_status.json
  profiling/raw/<target>/<phase>/perf-stat.txt
  profiling/raw/<target>/<phase>/perf.data           # 可选，本地受控
  profiling/perf_counters.csv
  profiling/topdown_summary.csv
  profiling/perf_trace_events.jsonl
  reports/join_coverage.json
  reports/stage_summary.csv
  reports/attribution_residuals.csv
  reports/profiling_analysis.json
  reports/profiling_analysis.md
  reports/profiling_analysis.html
```

`run_config.json` 记录代码/镜像版本、工作负载指纹、模型模式、并发/速率/超时、profiling 配置摘要、硬件和 perf 版本，不写密钥、原始请求或私有 endpoint。所有运行目录保持不可覆盖；报告公开目录只放脱敏数据，可能敏感的 raw 独立设为用户私有权限。`analyze-run` 对该 schema 重新计算报告，发现原始数据与已有汇总不符时报错，不把旧值当权威。

基础报告至少含：各阶段 QPS/成功率/p50/p95/p99、目标与用户维度；内部阶段分位数/占比与 residual；request join 的匹配/缺失/重复/多余数量；每目标每阶段 cycles、IPC、cache/branch miss、Topdown L1（若有效）；采集覆盖率、失败/降级原因、模型模式与局限性。简单规则可标注候选瓶颈，例如“recall p95 上升且同阶段 vector_search leaf 占比高”，但只有证据链完整时给 `confidence=supported`；否则显示 `hypothesis` 和下一次验证实验。质量/非空率不满足门槛时先报告正确性问题，不做“更快”结论。

分析结论不得把宿主资源采样当作进程级 perf，不得把 Topdown 占比与 wall-time 占比相加，也不得将缺失硬件/trace 数据记作 0。普通 benchmark 的 `official_small` timing 报告继续保留，并经适配器导入统一模型；缺失字段显示 unavailable，不从旧汇总反推不存在的 span。

## 8. 模块接入点与迁移

- `deployment_run.py` 负责部署状态核对、运行目标注册与组合选择；不包含 perf 命令解析。新的 experiment 编排模块负责可恢复的 deploy/run/analyze/cleanup 顺序。
- `multi_user_replay.py`、普通 workflow runner 和压测计划中的 `pressure_runtime.py` 都向同一 phase sink 发事件，并将 request events 交给分析器；不直接解析硬件计数器。压测前置未落地时保持当前拒绝行为。
- 新 profiling 模块隐藏 capability probe、cgroup 定位、采集器进程生命周期与 raw/status 归档；Linux perf 是首个适配器，unavailable 是另一实际适配器。
- 新 trace 导入/归因模块处理 stage map、去重、join 和残差；保留现有 `official_small_timing` 特定格式适配层。
- `result_analysis.py` 只协调离线分析结果并将统一 profiling 摘要嵌入现有报告，不把新解析逻辑继续堆进单个大文件。

向后兼容：不提供 profiling 配置时现有命令行为、报告不变。提供配置才生成 profiling/ 新工件。旧 run 的 `analyze-run` 继续可用，缺少新文件不视为损坏。`profiling/1` 与 stage-map 版本不兼容时明确失败，不静默降级。`deployment/1` 和 `multi-user-replay/1` 不改变现有字段含义。

## 9. 验证矩阵与实施顺序

1. **契约与离线分析**：用固定小样本验证 schema、重复/错配 request ID、leaf/wrapper 防重、缺失阶段 residual、无效计数器和旧 run 兼容；不依赖真实 perf。
2. **采集器**：用可注入命令执行器模拟 perf 支持、不支持、无权限、挂起、子进程残留、目标重启、计数器复用与原始文件上限；Linux CI 再做最小真实 perf smoke，权限不足须明确 skipped。
3. **阶段接入**：backend_direct 与 OpenClaw 各用 add→drain→search 小负载，验证先启动采集器、再打开阶段窗口，以及中断后的状态/清理。共享实例、多用户/多 Agent、不同速度用户只生成一次物理目标采集。
4. **部署闭环**：假 Docker 验证 deploy/verify/run/analyze/cleanup 的顺序、已存在部署不被清理、部署漂移阻止采集、运行失败仍产部分报告；真实 Linux 环境做一次端到端受控 probe。多 Agent 部署压测闭环须在既有压测计划完成后，针对共享记忆实例、多个 Agent target 和稳态 search 分别验收。
5. **报告验收**：人工核对一个高 LLM 等待和一个 CPU/向量检索主导的样本，确认报告不把 wall wait 判作 CPU 热点，Topdown/阶段耗时并列且有覆盖率/限制说明。

交付门槛：`best_effort` 在无 perf 权限的平台可完成客户端报告且状态明确；`required` 在正式测量请求前发现采集不可用；可用环境中每个目标/阶段都有原始计数器、规范化行和可复算报告；所有请求写入/检索的关联覆盖率和未关联原因可见；外部服务和用户原始数据不被自动删除或归档公开。
