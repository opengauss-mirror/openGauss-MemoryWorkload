# 生产负载回放与自动打点 Skill 设计

## 1. 目标

本改动交付两个能力：

1. 增加一个指导型自动打点 Skill。Meta agent 使用该 Skill 分析新记忆系统的 add/search 调用链，在源码中加入可关闭、可关联的性能打点，并验证改动没有改变业务语义。
2. 增加一个 Production Replay Benchmark Skill。该 Skill 从目录中读取 add/search JSONL，先完成 add 阶段和异步 drain，再执行 search 阶段。平台把原始请求交给支持该协议的 Memory Skill。

两项能力使用同一个 `request_id` 关联客户端请求、Memory Skill 调用、目标系统内部 span 和运行报告。

## 2. 范围

### 2.1 包含

- OpenMem 风格 add/search JSONL 的目录发现、校验和逐行读取。
- 原始请求 payload 的透传。
- add、异步状态等待、阶段 drain 和 search 的两阶段执行。
- 请求级成功率、延迟、结果数量及内部 span 关联覆盖率。
- Meta agent 使用的打点规则和提示词。
- 假 Memory Skill 端到端测试及文档契约测试。

### 2.2 不包含

- 从分离的 add/search 文件重建生产读写交错时序。
- 将 OpenMem 请求预先简化为普通 `content/query`。
- 在 Benchmark Skill 中实现某个目标记忆系统的字段映射。
- 在运行归档中保存消息正文、query、memory 内容或凭据。
- 第一版复刻生产请求之间的时间间隔。

## 3. 方案选择

### 3.1 采用方案

Production Replay Benchmark Skill 使用平台的 Memory Skill 边界。Benchmark 将完整请求放入 `inputs.raw_request`，同时沿用标准 `ingest/recall/status` 生命周期。Memory Skill 声明并实现 `openmem-v1` 原始请求协议。

该方案保留生产请求的字段、尺寸和消息结构，也复用平台现有的编排、监控、归档和结果分析能力。

### 3.2 未采用方案

Benchmark 直接调用 add/search HTTP API 会绕过 Memory Skill，平台无法复用目标系统适配器和统一生命周期。

Benchmark 将请求转换成 `content/query` 会丢失消息结构、过滤条件、租户字段及请求尺寸分布。

## 4. 目录布局

```text
memory_bench_platform/skills/
├── instrumentation/
│   └── memory-system-auto-instrumentation/
│       ├── SKILL.md
│       └── prompts/meta-agent.md
└── benchmarks/
    └── production-http-replay/
        ├── SKILL.md
        ├── manifest.yaml
        └── scripts/
            ├── validate.py
            └── run_replay.py
```

`instrumentation/` 中的文件供 meta agent 阅读。现有 Integration Skill loader 不加载该目录，也不要求它提供 manifest。README 将说明指导型 Skill 与运行时 Integration Skill 的区别。

`production-http-replay` 是可执行 Benchmark Skill。Manifest 声明 external runner 入口，要求调用方传入数据目录，并声明 Memory Skill 必须支持 `openmem-v1`、`ingest`、`status` 和 `recall`。

Runner 通过平台公开的 Memory Skill 执行接口调用选中的适配器，不直接调用目标系统 HTTP API。CLI 向 runner 注入选中的 Memory Skill ID、run ID、数据目录和输出目录。Runner 输出平台可导入的标准请求事件与汇总文件。该边界让 runner 逐行读取数据，也避免平台把 `raw_request` 写入 benchmark scenario artifact。

## 5. 输入数据契约

`--data-path` 指向一个目录。校验器必须在该目录中识别到一个 add JSONL 和一个 search JSONL。缺失文件或同类文件超过一个时，校验失败并列出候选文件名。

每行使用以下 envelope：

```json
{
  "request": {},
  "response": {}
}
```

`request` 必须是 JSON object。`response` 可以缺省。Runner 不发送 `response`；它只从原响应中提取生产成功状态、结果数量和异步状态等非敏感基线元数据。

校验器和 runner 逐行解析文件，避免一次加载大请求集合。错误信息包含文件名、行号和字段路径，不包含请求正文。

## 6. 请求标识与身份策略

Benchmark 为每行生成稳定请求标识：

```text
<operation>-<six-digit-line-number>-<canonical-request-sha256-prefix>
```

Canonical hash 使用排序后的 JSON key 和固定 UTF-8 编码。系统使用完整 hash 检测重复请求，使用短前缀构造可读 ID。

Benchmark 将 `request_id` 传入 Memory Skill。支持 HTTP 的 Memory Skill 再把它放入目标系统接受的 header、body 或上下文载体。目标系统内部打点沿用同一个值。

脱敏数据中的 `user_id` 可能为空。默认策略使用本次 run 的确定性 benchmark identity 填充空值，保留非空值。运行配置记录 identity 策略和生成规则。平台不在报告中记录原 identity。

## 7. Memory Skill 请求契约

add 请求使用：

```json
{
  "action": "ingest",
  "inputs": {
    "source_protocol": "openmem-v1",
    "source_operation": "add",
    "request_id": "add-000001-…",
    "raw_request": {}
  }
}
```

search 请求使用 `action=recall` 和 `source_operation=search`，其余字段相同。

Memory Skill 必须在 manifest capability 中声明 `openmem-v1` 支持。平台在运行前检查该 capability。声明支持的适配器必须处理完整 payload；适配器无法表达某个字段时应返回结构化的 unsupported-field 错误，不得静默删除字段。

Memory Skill 将 add 响应转换成标准 operation，其中包含后端 task ID 和状态。返回 running 时，Benchmark 使用标准 `status` action 轮询。Memory Skill 将 search 响应转换成标准 `count`、`memories` 和 `evidence_text`，同时把允许归档的非敏感统计放入 metrics。

## 8. 执行流程

```text
validate dataset directory
  -> stream add JSONL
  -> call selected Memory Skill with ingest requests
  -> poll accepted async operations
  -> drain all add operations
  -> mark dataset complete or partially written
  -> stream search JSONL
  -> call selected Memory Skill with recall requests
  -> join request events and internal spans
  -> write correctness, latency and coverage summaries
```

单个请求失败不会终止本轮回放。Runner 记录错误类型和经过清理的错误信息，然后继续执行。add drain 完成后仍有失败或超时任务时，Runner 将数据集状态标记为 `partially_written`，search 结果保留，但报告必须携带该状态。

第一版支持 add/search 独立的并发、速率和超时设置。Runner 将这些值写入 `run_config.json`。分离文件不包含统一时间线，因此第一版只执行 add 后 search 的两阶段顺序。

## 9. 归档与隐私

Runner 在执行期间持有 `raw_request`，但运行归档不得保存它。请求事件只记录：

- request ID、operation、文件标识和行号
- canonical payload hash、payload byte size 和顶层字段集合
- 开始时间、耗时、状态和清理后的错误
- task ID 的不可逆摘要、result count 和响应 byte size

Run artifact 不记录消息正文、query、返回 memory、API key、token、私有 endpoint 或原始 identity。测试扫描运行目录，发现样例中的 sentinel 内容时失败。

## 10. 自动打点 Skill

### 10.1 输入

Meta agent 接收目标源码路径、add/search 入口线索、打点启用方式和 `request_id` 传递约定。入口线索缺失时，agent 从 HTTP route、SDK method、CLI command 或 queue consumer 开始追踪。

### 10.2 工作流程

Meta agent 执行以下步骤：

1. 追踪 add/search 入口到业务服务、模型调用、embedding、存储、索引、锁、队列和响应格式化的调用链。
2. 找出已有 logger、telemetry、配置和 context propagation 机制。
3. 输出打点清单，标明 stage、`leaf/wrapper`、同步或异步边界及请求关联方式。
4. 在真实调用边界加入最小打点代码。
5. 为成功和异常路径输出结构化 JSONL，随后重新抛出原异常。
6. 运行目标仓库测试和小规模 add/search probe。
7. 输出覆盖报告，列出已覆盖阶段、缺失阶段、无法确认的阶段和验证命令。

Skill 禁止 agent 根据函数名猜测阶段、记录业务正文、改变业务返回值或异常类型。Agent 不能把 wrapper 和子 leaf 同时计入阶段占比。

### 10.3 Canonical stage

写入阶段：

```text
extract.preprocess
extract.llm
extract.vectorize
extract.write
```

检索阶段：

```text
retrieve.query_vectorize
retrieve.vector_search
retrieve.keyword_search
retrieve.fusion
retrieve.rerank
retrieve.format
```

系统可以保留自己的 stage 名称，但覆盖报告必须将其映射到上述分类。Wrapper span 只用于诊断，不进入类别占比的分母。

### 10.4 事件格式

每个事件至少包含：

```text
kind, ts, operation, stage, span_role, duration_ms, status, request_id
```

可选字段包括 trace ID、task ID、tenant/user/session/resource ID、count、provider、model、backend、retry count 和 result count。错误事件增加清理后的 `error_type` 和 `error_message`。

目标系统默认关闭打点，benchmark 运行时显式启用。Agent 使用 monotonic clock 测量 duration，使用带时区 wall clock 写 timestamp。

## 11. Meta agent 提示词

`prompts/meta-agent.md` 提供可直接使用的提示词，并为以下值保留填写位置：

- 目标源码路径
- add/search 公开入口或接口文档
- `request_id` header/body 约定
- 打点开关与日志输出位置
- 目标测试命令

提示词要求 agent 先提交调用链和打点计划，再编辑源码。Agent 应限制修改范围，复用目标仓库风格，并附上测试结果、未覆盖阶段和已知限制。

## 12. 失败语义

| 情况 | 处理 |
|---|---|
| JSONL 语法或必要字段错误 | 校验失败；报告文件、行号和字段 |
| add/search 单请求失败 | 记录失败并继续 |
| add 异步任务失败或超时 | 计入 add 失败；数据集标记为部分写入 |
| Memory Skill 不支持 `openmem-v1` | 运行前失败 |
| 适配器静默丢弃未声明字段 | 契约测试失败 |
| 内部 span 缺少 request ID | 回放继续；结果标记为 exploratory |
| collector 不可用 | 写入 skipped reason；回放继续 |

## 13. 输出

正式 run 至少生成：

- `run_config.json`
- `request_events.jsonl`
- `perf_trace_events.jsonl` 或内部 span 导入结果
- add/search 请求汇总，包含成功率和 p50/p95/p99/max
- add 异步完成率和 drain 状态
- search 非空率及结果数量分布
- request ID join coverage，包含 missing、duplicate 和 unmatched 统计
- collector 状态和失败原因
- 数据集完整或部分写入标记

平台只有在 request ID 关联链完整时才生成阶段归因结论。关联链缺失时，报告保留客户端结果并标记为 exploratory。

## 14. 测试

实施采用测试先行。测试覆盖：

- 目录发现、同类多文件、缺失文件和错误行定位
- JSONL 流式读取及 canonical request ID
- 原始 add/search payload 无损传入假 Memory Skill
- 空 identity 的确定性填充及非空 identity 保留
- add、drain、search 阶段屏障
- 异步状态轮询、失败、超时和部分写入
- 请求重复检测和 join coverage
- 运行归档的正文与敏感字段扫描
- Benchmark manifest、external runner 解析和 CLI validate
- external runner 通过假 Memory Skill 完成端到端回放
- 自动打点 Skill 必需章节和 meta-agent 提示词契约
- `memory_bench_platform` 与 `locomo_test` 回归

macOS 基线中，`memory_bench_platform` 有 9 个依赖 Linux `/proc` 的既有失败；其余 320 个测试通过。`locomo_test` 的 84 个测试通过。实施验证要区分这些环境失败与新增失败。

## 15. 验收标准

1. CLI 能校验包含 add/search JSONL 的目录，并报告请求数量和 schema 概况。
2. 假 Memory Skill 能收到与输入文件一致的完整请求对象。
3. Runner 完成所有 add 的 drain 后才发送第一条 search。
4. 单请求失败不会终止负载，报告会反映失败率和部分写入状态。
5. Run artifact 不包含测试 sentinel 正文或凭据。
6. request ID 能关联客户端事件与导入的内部 span；缺失关联时报告标记 exploratory。
7. Meta agent Skill 和提示词能指导 agent 生成 stage map、最小补丁、验证结果及覆盖报告。
8. 新增定向测试通过，回归结果不新增失败。
