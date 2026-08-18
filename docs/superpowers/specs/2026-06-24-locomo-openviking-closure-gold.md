# LoCoMo + OpenClaw + OpenViking 闭环 Gold

## 1. 目标

为当前仓库沉淀一份统一的闭环验收口径，用于区分：

- 测试平台是否已经把 `messages -> commit -> token extraction -> result extraction -> diagnostics` 打通。
- 被测系统 `OpenViking` 是否已经把 `memory write -> index build -> recall/search/find` 真正打通。

这份 gold 只服务于当前任务定位，不替代长期架构文档。

## 2. Gold 验收分层

### 2.1 平台闭环通过

满足以下条件时，判定测试平台侧闭环通过：

1. `locomo_test` 能以统一配置启动 `OpenClaw + OpenViking` 远端 isolated runtime。
2. `OpenViking` commit 后的 task/session token 用量能够被平台提取，并写回 `qa_results.csv`。
3. `qa_results.csv` 至少写出以下 OpenViking 闭环字段：
   - `ov_memory_written`
   - `ov_token_emitted`
   - `ov_index_available`
   - `ov_closure_state`
4. 即便 `stats=false`，QA 阶段结束后也会自动生成 `qa_diagnostics.json`。
5. 当 `memory` 已写入但索引不可检索时，平台能够明确给出：
   - `ov_closure_state=memory_written_but_index_unavailable`
   - `issues.openviking_memory_written_but_index_unavailable > 0`
   - `issues.openviking_index_missing_records_max > 0`

### 2.2 被测链路闭环通过

满足以下条件时，才判定 `OpenClaw + OpenViking` 被测链路真正闭环通过：

1. `commit` 后 memory 确实写入目标 user/account namespace。
2. `search/find` 或 `recall` 能返回非空命中，而不是只有 observer 的向量统计。
3. `system/consistency.missing_record_count = 0` 或稳定收敛到可检索状态。
4. `qa_results.csv` 中对应问题的 `ov_closure_state=memory_closed_loop_ready`。
5. 最小 LoCoMo 样例上，回答不是“无 recall / 无上下文”类型的假阴性。

只有同时满足 2.1 和 2.2，才算“完整 benchmark 平台 + 被测 memory 链路”都打通。

## 3. 当前代码基线已经完成的事项

### 3.1 统一 LLM 配置

- `env.toml / env.toml.example` 已统一为：
  - `[llm.chat]`
  - `[llm.embedding]`
- `judge` 默认继承 `llm.chat`，避免 judge 与主链路配置漂移。

### 3.2 OpenViking 对接闭环

- `locomo_test` 已改为以官方闭环为主：
  - `messages -> commit -> recall`
- 平台不再把旧 `compact/task` 兼容路径当作唯一真值来源。
- `keep_recent_count` 已提升为配置项：
  - 允许 `None`
  - 允许显式 `0`
  - 平台不再硬编码 `10`

### 3.3 结果提取与诊断

- `ov task/session usage` 已使用带 scope 的请求头提取。
- `qa_results.csv` 会落盘 OpenViking token 与闭环状态字段。
- `qa_diagnostics.json` 会自动聚合 run 级问题。
- `meta.json` / `stats` 会额外汇总 `ov_closure_counts` 与 `ov_closure_summary`。

### 3.4 远端 isolated runtime bootstrap

- `locomo_test/bootstrap_remote_runtime.py` 会：
  - 复制 OpenClaw runtime agent 所需 auth/profile/model 文件
  - 写入 OpenViking account/user/agent identity
  - 设置 scope 隔离参数
- 该修复已覆盖“runtime agent 缺少鉴权材料导致 commit/usage 查询失真”的平台问题。

## 4. 当前任务定位结论

### 4.1 已确认不是平台主因的问题

以下问题已经收敛并修复或可被平台稳定识别：

- OV token 一直为 0
  - 原因：平台查询 usage 时 headers 不完整。
  - 现状：已修复。
- isolated runtime 下插件鉴权失败
  - 原因：runtime agent 目录缺少 auth/profile/model 文件。
  - 现状：已修复。
- `keep_recent_count` 固化在测试平台
  - 原因：平台曾硬编码。
  - 现状：已改为配置项。

### 4.2 当前剩余主问题

当前剩余主问题不是“平台没发出去”或“平台没记下来”，而是：

- OpenViking local vectordb 出现运行态与持久态分叉；
- observer 还能看到向量统计；
- 但 fresh backend 看不到合法 collection；
- `search/find` 与 `search/search` 返回空；
- `system/consistency` 持续有 missing records；
- 磁盘 candidate store 为空，说明问题不只是 metadata 缺失。

因此当前更精确的归类是：

- 平台闭环：基本已通。
- 被测链路闭环：仍未通。
- 阻塞点：OpenViking local vectordb persistence/index consistency。

## 5. 当前推荐的最小诊断闭环

### 5.1 平台侧

先看：

- `qa_results.csv`
- `qa_diagnostics.json`
- `pipeline.log`

重点判断：

- 是否 `ov_memory_written=true`
- 是否 `ov_token_emitted=true`
- 是否 `ov_index_available=false`
- 是否 `ov_closure_state=memory_written_but_index_unavailable`

### 5.2 SUT 侧

再运行：

```bash
python3 -m skills.benchmarks.locomo.tooling.test_entrypoints.diagnose_openviking_split \
  --api-key <root-api-key> \
  --account-id <acct-run-id> \
  --agent-id <acct-run-id>_main \
  --target-uri viking://user/eval-1/memories
```

重点看：

- `observer_vikingdb`
- `search_find`
- `search_search`
- `consistency`
- `fresh_backend.collection_exists`
- `fresh_backend.collection_meta`
- `copied_store.candidate_count`
- `root_cause_hint`

## 6. 当前 gold 结论

截至 2026-06-24，当前 gold 应表述为：

- `locomo_test` 已具备判定 OpenViking memory 闭环状态的测试平台能力。
- 平台已能把“写入成功但索引不可检索”从“平台提取失败”中分离出来。
- 在共享 `1933` 服务上，LoCoMo 异常结果不应再笼统归因到测试平台，而应先检查：
  - 是否命中了已损坏的共享 vectordb
  - 是否把 recall 扩到了 agent-scoped namespace
- 在 isolated OpenViking 实例上，已经确认：
  - memory 真实落在 `viking://user/<user>/memories`
  - direct `search/find` 对 user-root URI 可命中
  - `locomo_test` mini-test 已能返回正确回答

- 因此当前更精确的 gold 是：
  - 平台侧：已打通
  - mini-test 实际记忆闭环：已打通
  - 共享 1933 服务上的 `memory_written_but_index_unavailable`：属于历史污染环境，不再代表当前 isolated 入口的真实状态

## 7. 下一步完成条件

要把本任务从“定位完成”推进到“真正打通”，下一步必须满足：

1. 最小样例上 `search/find` 返回非空命中。
2. `consistency` 不再持续出现 missing records。
3. `ov_closure_state` 从 `memory_written_but_index_unavailable` 进入 `memory_closed_loop_ready`。
4. 在此基础上再跑一轮 LoCoMo small，验证 recall 质量与答案质量。
