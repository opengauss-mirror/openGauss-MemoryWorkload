请基于 TARGET_REPO 中的新记忆系统，按照随附的 memory-system-auto-instrumentation Skill，先分析 ADD_ENTRYPOINT 与 SEARCH_ENTRYPOINT 的真实调用结构，再设计并加入性能打点。

输入：

- 目标源码：`TARGET_REPO`
- add/ingest 公开入口或线索：`ADD_ENTRYPOINT`
- search/recall 公开入口或线索：`SEARCH_ENTRYPOINT`
- 请求标识传递约定：`REQUEST_ID_CONTRACT`
- 打点启用方式：`TRACE_ENABLEMENT`
- JSONL 输出位置：`TRACE_OUTPUT`
- 目标仓库测试命令：`TEST_COMMANDS`

严格分成两个阶段执行：先交付 call graph 和 stage map，再 edit source。

第一阶段只做分析。Before you edit source, return a call graph and stage map for review. 从公开 API、SDK、CLI 或 queue consumer 向内追踪真实依赖调用、异步边界、后台任务、锁和上下文传播；先查明并复用仓库现有 logger、telemetry 和配置机制。Stage map 必须列出源码位置、真实调用边界、canonical stage、leaf/wrapper 角色、同步/异步属性，以及 request ID 的载体。没有 keyword search、fusion 或 rerank 时标为 `absent`，不得虚构 span。完成第一阶段后停止，等待批准。

批准后进入第二阶段。After approval, edit source with instrumentation default off. Propagate request IDs according to REQUEST_ID_CONTRACT. Write JSONL to TRACE_OUTPUT when TRACE_ENABLEMENT is enabled. 使用 monotonic clock 计算 duration，使用带时区 wall clock 写 timestamp；成功和异常路径都输出事件，异常事件清理敏感信息后必须重新抛出原异常。

只在已确认的真实边界做最小修改。异步 wrapper 若 offload 阻塞调用，可添加内部 `*.backend_call` leaf；wrapper 和 child leaf 不得同时进入类别占比。不得记录 prompt、message、query、raw request、memory、检索内容、凭据、token、私有 endpoint 或原始身份。不得静默转换异常、虚构阶段、顺带重构业务代码，或改变返回值、重试、超时、顺序和并发语义。

Run TEST_COMMANDS and a short add/search probe. 比较启用和关闭打点时的业务返回与异常，解析 JSONL 并扫描敏感 sentinel。

最终返回 changed files、测试和 probe 结果，以及逐阶段 coverage：`covered`、`absent`、`missing`、`unverified`。将存在事件但无法关联的路径单列为 `missing_request_id`，不要把它混同于零耗时或缺失 stage。
