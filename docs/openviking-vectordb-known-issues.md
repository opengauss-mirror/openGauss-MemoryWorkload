# OpenViking Vectordb 已知问题

## 1. collection metadata 残缺

- 问题：OpenViking 本地 vectordb 在极端重启/异常退出后，可能出现 `index/default/versions/*/manager_meta.json` 与 `.write_done` 仍在，但顶层 `collection_meta.json` 或 `index/default/index_meta.json` 缺失。
- 现象：
  - 运行中的旧进程仍可继续报告已有向量数量。
  - fresh manager / 新进程冷启动时会认为 collection 不存在，导致检索链路失真。
- 归类：被测系统已知缺陷，不作为当前 LoCoMo 对接阻塞项。

### 2026-06-23 当前复现实证

- 运行中 OpenViking `observer/vikingdb` 报告：
  - `Collection=context`
  - `Vector Count=39`
- 同一运行实例的 `memory` 文件系统可见：
  - `viking://user/eval-1/memories/...`
- 但同一实例：
  - `/api/v1/search/find` 返回 `total=0`
  - `/api/v1/search/search` 返回 `total=0`
  - `/api/v1/system/consistency` 持续报告 `missing_records > 0`
- 更关键的是，fresh backend / 新进程复查时：
  - `collection_exists=False`
  - `collection_info=None`
  - `collection_meta=None`
  - 按 `account_id=acct-locomo_min_verify2_20260623_205824` 直接查 vector records 返回 `0`
- 同时，磁盘上只看到：
  - `/root/.openviking/data/vectordb/context/index/default/versions`
  - 未看到顶层 `collection_meta.json`

### 2026-06-24 远端 split 诊断复核

- 使用：
  - `memory_bench_platform/skills/benchmarks/locomo/tooling/test_entrypoints/diagnose_openviking_split.py`
- 诊断对象：
  - `account_id=acct-locomo_min_authfix3_20260623_213058`
  - `target_uri=viking://user/eval-1/memories`
- 当前实测结果：
  - `observer/vikingdb`:
    - `Collection=context`
    - `Vector Count=38`
  - `search/find.total=0`
  - `search/search.total=0`
  - `system/consistency.expected_count=52`
  - `system/consistency.missing_record_count=52`
  - `live_files.collection_meta_exists=false`
  - `live_files.index_meta_exists=false`
  - `fresh_backend.collection_exists=false`
  - `fresh_backend.collection_meta=null`
  - `fresh_backend.account_rows=[]`
  - `copied_store.candidate_count=0`
- 诊断脚本给出的 `root_cause_hint`：
  - `missing_collection_meta`
  - `fresh_backend_cannot_see_collection`
  - `candidate_store_empty`
  - `consistency_missing_records`

- 这轮复核与 2026-06-23 的判断一致，而且更明确：
  - 不是只有 metadata 丢失；
  - 连 candidate store 也为空；
  - 因此不是“补一个 metadata 文件”就能恢复检索。

### 当前结论

- 这不是 `locomo_test` 结果提取链或 `OpenClaw` 发送链的问题。
- `OV token=0` 曾是平台查询 headers 不完整导致，现已修复。
- 当前剩余 `recall/search/find=0` 的根因，已收敛为：
  - OpenViking local vectordb 运行态与持久态分叉
  - 或等价的 collection metadata 残缺 / 不可重建
- 因此会出现：
  - 运行态 observer 看到有向量
  - fresh backend 看不到 collection
  - benchmark 侧 memory 文件已写入，但检索仍为空

### metadata 恢复试验（副本）

- 在不触碰线上目录的前提下，对 `/root/.openviking/data/vectordb/context` 做了副本恢复试验：
  - 复制到 `/tmp/ov-vdb-recover-test/context`
  - 用 OpenViking 本地 collection 路径补建 `collection_meta.json`
  - 再手工补建 `index/default/index_meta.json`
- 结果：
  - fresh collection 的 `collection_exists / list_indexes` 可以恢复
  - 但恢复后的副本 `count` 异常（观测到 `2047`），且出现大量
    `Candidate data is None for label index ...`
  - 说明“仅补 metadata 文件”不足以证明索引健康恢复
- 当前判断：
  - 纯运维层的 metadata 重建，可能把“完全不可见”恢复成“勉强可加载”
  - 但不能保证向量索引与底层 store 一致，更不能直接作为 benchmark 正式闭环的可靠修复

### 进一步收敛：candidate store 已空

- 在同一份副本 `/tmp/ov-vdb-recover-test/context` 上继续做了拆分验证：
  - 直接读取 `store/` 下的 candidate data，结果 `CAND_COUNT = 0`
  - 删除旧 `index/default` 后，仅按当前 store 重建新 index，结果 `COUNT = 0`
- 这说明：
  - 问题不只是 `collection_meta.json` / `index_meta.json` 缺失
  - 而是磁盘上的 candidate store 本身已经没有可恢复记录
  - 运行态 observer 看到的 `Vector Count > 0`，本质上更像“旧 index 里残留 label / 向量结构”，而不是一套可被 fresh backend 正常重建的完整数据

### 更精确的当前结论

- 当前 `search/find=0` 的 SUT 根因可进一步表述为：
  - OpenViking local vectordb 运行态 index 仍持有旧 label / 向量统计
  - 但持久态 metadata 缺失，且 candidate store 为空
  - 因此 fresh backend 既看不到合法 collection，也拿不到可检索 candidate data
- 换句话说：
  - 不是“只要补 metadata 就能恢复检索”
  - 而是“metadata 与 store/candidate 数据同时断裂”

## 2. 当前平台绕过策略

- 对 `locomo_test` 的 OpenViking 模式，在 `health_check` 阶段默认发送一次独立 bootstrap warmup 请求。
- 目标：
  - 在 fresh data dir 或 reset 后，尽早触发 OpenViking context collection / index 初始化。
  - 避免把“schema 尚未初始化”与“LoCoMo 记忆能力”混为一谈。
- 该 bootstrap 仅用于测试前预热，不用于掩盖真实 recall/answer 质量问题。

## 3. 当前建议

- 做 LoCoMo 最小用例或 small 跑数前，优先走带 health check 的 `locomo_test` 入口。
- 如需关闭该预热，可设置环境变量：

```bash
export LOCOMO_OPENVIKING_BOOTSTRAP=false
```

- 若后续仍观察到“运行态有向量、冷启动后 collection 不存在”，应继续按 SUT 缺陷跟踪，而不是直接判定 benchmark 接口失败。

## 4. 2026-06-24 闭环修正结论

经过对 `locomo_test` 最新远端入口的复核，当前 `search/find=0` 并不只有一类原因，而是至少有两类：

### 4.1 共享 1933 服务污染

- 旧 `run_locomo_test_remote.sh` 虽然隔离了 OpenClaw gateway/state，但默认仍把 plugin 指向共享 `http://127.0.0.1:1933`。
- 该共享服务长期复用 `/root/.openviking/data/vectordb/context`，并且已有：
  - `collection_meta.json` 缺失
  - `index_meta.json` 缺失
  - candidate store 为空
- 因此会出现：
  - observer 里 `Vector Count > 0`
  - 真实 `search/find = 0`
  - fresh backend 看不到 collection

### 4.2 namespace 查询落点错位

- 在 isolated OpenViking 数据目录上复核时，fresh backend 可直接看到：
  - `account_id=acct-locomo_test_iso_20260624b`
  - `uri=viking://user/eval-1/memories/...`
- 直接对 isolated OV 做查询对比：
  - `target_uri=viking://user/eval-1/memories` -> `total=5`
  - `target_uri=viking://user/eval-1/agent/<agent>/memories` -> `total=0`
  - `target_uri=viking://agent/<agent>/user/eval-1/memories` -> `total=0`
- 说明：
  - memory 实际写入 user-root namespace
  - 但 recall 一旦被 `isolateUserScopeByAgent / isolateAgentScopeByUser` 扩到 agent scope，就会命中 0

### 4.3 当前平台侧修正

- `run_locomo_test_remote.sh` 已改为：
  - 启动独立的 isolated OpenViking 实例
  - 使用独立 workspace/data 目录
  - 不再默认回落到共享 `1933`
- `locomo_test.bootstrap_remote_runtime` 已改为：
  - 保留 per-run `account_id`
  - 不再默认注入 `isolateUserScopeByAgent / isolateAgentScopeByUser`

### 4.4 当前实测结果

- 新 run：`locomo_test_iso_20260624c`
- 结果：
  - ingest 正常 commit，`ov_task` 非 0
  - QA 三题回答恢复为正确事实回答
  - gateway 日志明确显示：
    - recall 查询 `target_uri="viking://user/eval-1/memories"`
    - 后续成功 `content/read` 多条 memory
    - 实际注入了 `6 memories`

- 因此当前结论更新为：
  - `OV token 不回写`：平台问题，已修复
  - `search/find=0`：主要由共享损坏的 1933 服务和 agent-scope namespace 错位共同导致
  - `LoCoMo + OpenClaw + OpenViking` 的 mini-test memory 闭环：已用 isolated OV + user-root recall 跑通
