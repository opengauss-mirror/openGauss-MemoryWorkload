# 跨样本并发

原生 `cli run` / `cli plan` 支持 `--sample-concurrency N`，默认 1。
支持 `backend_direct` 和配置了独立 OpenClaw 运行实例的 `agent_plugin`。
第一版使用上面的原生手动运行入口，`run --deployment` 不透传此选项，
显式设置大于 1 会报错，不会静默忽略。

## 运行

在已有服务、模型和 Token 采集配置的基础上：

```bash
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend openviking \
  --memory-integration backend_direct --data-path /path/to/locomo.json \
  --sample-concurrency 2 --qa-concurrency 2 --judge-concurrency 2
```

LongMemEval 使用同一命令，将 benchmark 改为 `longmemeval`，
data-path 改为对应的原生数据集文件。此参数不配置或部署模型、Agent、记忆服务。

- LoCoMo 的一个 conversation（全部历史及 QA）是一个样本。
- LongMemEval 的一个问题及其历史集合是一个样本。
- 只有一个样本的 LoCoMo-small 不会因提高样本并发而加速。
- 也可在已有 execution_spec 中设置 `sample_concurrency`；CLI 显式参数优先。

## 顺序与隔离

每个活跃样本沿用 composer 的依赖链：每段 ingest 后，按记忆适配器能力
执行 flush 和 readiness；完成后才开始下一段。同步写入的适配器以成功返回
作为完成屏障，不额外调用不支持的 flush/status。检查点 QA 和 Judge 完成后，
才推进同一样本的下一个检查点。失败与超时的依赖、跳过或降级规则不变。

不同样本可以处于不同阶段。QA 和 Judge 使用全局共享线程池，
其上限分别是 qa_concurrency 和 judge_concurrency，不乘以样本并发数；
历史准备不占 QA 线程池。sample_concurrency 控制同时活跃的完整样本流程数，
不表示后端内部 LLM 或 Embedding 的请求上限。

并发前要求每个 case 有 sample_id，记忆步骤有显式 scope_id，
不同样本不能共享 scope；Agent session_key 必须唯一，
不支持跨样本依赖。实际后端仍须正确遵守适配器的 scope 隔离配置。
结果按原计划顺序汇总，每个 case 的进度独立落盘。

## 统计边界

沿用现有评分、Token 采集与离线重建。整个 run 只启停一次记忆 Token 采集器：
原生全局计数器或独占代理窗口不按样本重复采集、求和。
请求级证据可保留已有 case/step 关联；只有整轮统计的来源不能据此拆出样本用量。
Cache Read 保留现有归一化口径，不在 total 上重复相加。

独占代理要求本轮开始前没有在途请求，并使用未被其他 run 占用的审计目录。
不同样本属于同一 run，可以共享该独占代理。

步骤时间、Trace 和整轮耗时仍保留。阶段独占窗口 profiling 与
sample_concurrency > 1 不兼容，执行器会拒绝该组合；需要阶段独占分析时使用 1。
并发下写入与 QA 会共享后端资源，耗时应解释为并发负载下的结果，
不能直接当作串行延迟。

本功能不更改历史正文、模型、检索参数、评分规则或记忆系统源码。

OpenViking 并发运行需要 `OPENVIKING_SCOPE_USERS_FILE`。使用原生账号管理接口为每个样本预先创建独立用户，并将凭据保存在权限为 0600 的 JSON 文件中。键是计划里的完整 `scope_id`，值是 `{"user_id": "...", "api_key": "..."}`。每个 scope 使用不同的用户和用户密钥，同一样本的写入、提交、就绪查询和召回复用同一绑定。文件属于运行环境，不应纳入版本控制。使用新 run ID 时同时准备新的用户和绑定。

## OpenClaw agent_plugin

复用同一调度器，每个样本绑定一个已部署的专用 Gateway。首次只支持 OpenClaw
组合；不会自动创建容器，也不会自动为任意记忆系统提供隔离。

准备私有 `sample-runtimes.json`（权限 0600，不提交到仓库）：

```json
{
  "conv-26": {
    "OPENCLAW_GATEWAY_URL": "http://127.0.0.1:19001",
    "OPENCLAW_GATEWAY_TOKEN": "当前实例密钥",
    "OPENCLAW_CONFIG_PATH": "/absolute/sample-a/openclaw.json",
    "OPENCLAW_STATE_DIR": "/absolute/sample-a/state",
    "OPENCLAW_BIN": "/absolute/sample-a/openclaw-host"
  },
  "conv-30": {
    "OPENCLAW_GATEWAY_URL": "http://127.0.0.1:19002",
    "OPENCLAW_GATEWAY_TOKEN": "当前实例密钥",
    "OPENCLAW_CONFIG_PATH": "/absolute/sample-b/openclaw.json",
    "OPENCLAW_STATE_DIR": "/absolute/sample-b/state",
    "OPENCLAW_BIN": "/absolute/sample-b/openclaw-host"
  }
}
```

键必须与数据集计划的 sample_id 完全对应。LongMemEval 使用 question_id。
每份环境还需包含所选组合原有的环境配置；oGMemory 的
`OGMEM_GATEWAY_CONTAINER`、`OGMEM_PLUGIN_STATE_FILE` 分别指向当前样本容器和独立阶段文件。
`OPENCLAW_BIN` 是指向当前实例的 CLI 包装脚本。服务共享时，必须配置不同的
原生账号/用户/记忆 scope；仅分开 Gateway 端口不能保证后端数据隔离。
OpenViking 在各实例原生配置中绑定独立账号/用户，oGMemory 沿用适配器的样本 scope 绑定。

```bash
python -m memory_bench_platform.cli run \
  --benchmark locomo --agent openclaw --memory-backend ogmemory \
  --memory-integration agent_plugin --data-path /absolute/two-samples.json \
  --sample-concurrency 2 --sample-runtime-config /absolute/sample-runtimes.json \
  --qa-concurrency 2 --judge-concurrency 4
```

OpenViking 更换 memory-backend 和实例配置，LongMemEval 更换 benchmark 与数据文件。
串行对照可保留独立实例绑定，将 sample-concurrency 和 qa-concurrency 均设为 1。
每段历史依次执行 Agent 接收、原生提交（若支持）、就绪检查（若支持），
然后才进入下一段。QA 在线程池中跨样本并发，全局上限为 qa-concurrency，默认每个样本的 Gateway 同时最多一题。Judge 使用已有共享线程池。
阶段切换、配置快照、pending、CLI 状态和 Token transcript 查询都使用当前样本绑定。
没有绑定且并发为 1 的原有入口保持可用。

运行时绑定与阶段独占 profiling 暂不能组合；步骤起止时间与整轮报告仍保留。
记忆 Token 仍只采集一次整轮独占窗口，Agent usage 从各自会话采集，
不会按样本重复累加整个后端计数器。绑定文件可能含凭据，应按私有运行配置管理。

所有实例在运行结束时分别执行原有 finalize，恢复各自配置；某个实例失败不会跳过
其他实例的收尾，汇总报告保留逐实例结果。oGMemory 的只读就绪查询若因另一样本
提取占用服务而触发单次读取超时，会在原有总 readiness 预算内重查，耗尽预算仍失败；
不重提 compact，不重复触发提取，也不重试配置错误或其它 HTTP 错误。

Adapters with externally assigned memory identities may provide
`scripts/sample_identity.py` in their skill. Before writing, Workload passes
each bound or parallel sample's scopes on stdin using that sample's adapter
environment. Return an `identities` array of non-secret native identity
strings. Shared identities or a failed preflight stop the multi-sample run.
The default unbound serial path does not invoke this check.

oGMemory 的 HTTP Gateway 可以显式开启同一样本内的只读 QA 并发。例如
`--sample-concurrency 1 --qa-concurrency 4 --qa-per-sample 4 --judge-concurrency 4`
允许一个 conversation 同时回答四题。多样本共用全局 QA 上限，每题使用独立会话并读取同一样本的记忆。
日志只用于确认对应会话的召回，无法归属的错误保留在 Gateway 日志中；有效答案缺少本题回执时按召回未验证记录。

### 插件历史不完整时的 QA

oGMemory 专用 HTTP Gateway 若确认当前 compact 的准备请求已终止失败，并确认该会话空闲、索引队列为空，
Workload 保留失败回执，跳过本检查点剩余历史，尝试切换至同一 scope 的只读 QA 阶段。
专用 Gateway 重启、加载原生插件并通过阶段校验后，各题沿用正常 Agent 和 Judge；评分记入
`degraded_qa`，检查点仍为未就绪。后续检查点继承历史不完整标记。身份或配置错误、后台状态未知、
只读阶段未确认时，QA 继续受依赖门禁保护。其他插件可通过相同的已终止失败码及只读回执接入。
