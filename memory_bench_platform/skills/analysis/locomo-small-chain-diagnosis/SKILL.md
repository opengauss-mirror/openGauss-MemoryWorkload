# LoCoMo Small Chain Diagnosis

用于排查 `official_small` / `direct-ov` 这条 LoCoMo `small` 对接链路异常。

## 何时使用

- `small` 准确率明显偏低
- `memories=0`、`retrieval_miss`、`session_file_not_found` 等症状出现
- 需要判断问题在 `session 构造 / namespace 隔离 / capture / recall / answer` 哪一层

## 核心输入

- `external_artifacts/official_small/phaseA*_meta.json`
- `external_artifacts/official_small/remote_logs/*.master.log`
- `reports/analysis.json`

## 节点定义

1. `session_construction`
   - 看 `ingest_sessions`
   - 看 `ov_session_id`
   - 看 `commit_completed`
2. `namespace_isolation`
   - 看 `plugin_namespace_config.final`
   - 看 `accountId / userId / agent_prefix`
   - 看 `isolateUserScopeByAgent / isolateAgentScopeByUser`
3. `memory_capture`
   - 看 `master.log` 中 `memories=N`
   - 看 `zero_memory_sessions`
4. `recall_query`
   - 看 `ov_log_tail` 里的 `POST /api/v1/search/find`
   - 看 `GET /api/v1/content/read`
   - 看 `ledger_missing_rows`
5. `answer_generation`
   - 看 `qa_rows.response`
   - 看 `retrieval_miss_like_rows`
   - 看 `qa_total_tokens`

## 时间分布

诊断脚本会输出：

- `timing.ingest`
  - `min / max / mean / p50 / p90`
- `timing.qa`
  - `min / max / mean / p50 / p90`
- `timing.qa_tokens`

## 使用方式

```bash
python3 -m skills.benchmarks.locomo.tooling.test_entrypoints.diagnose_official_small \
  /path/to/memory_bench_platform/runs/<run_id>
```

或者直接看平台已经自动写入的：

- `reports/analysis.json -> chain_diagnostics`

## 判断原则

- `session_total` 正常但 `zero_memory_sessions` 很高
  - 优先怀疑 capture / commit / namespace 问题
- `search_find_calls > 0` 且 `content_read_calls > 0`
  - 说明 recall 不是完全没发生
- `ledger_missing_rows` 很高
  - 说明 OpenClaw session 侧可观测性缺失，不能只信回答文本
- `retrieval_miss_like_rows` 很高
  - 说明 answer 层拿到的是“无信息”上下文，不应先怪 judge

## 模块归类

遇到 `official_small` 异常时，先按下面分类，不要把所有低分都归因到同一层。

1. `OpenViking / memory-capture` 模块问题
   - 典型症状：
     - `memories=0/0/7/0`
     - `zero_memory_sessions` 很高
     - `commit_completed` 正常，但大多数 session 没抽出 memory
   - 归因：
     - 这是 `OpenViking 版本 / 配置 / capture-extraction` 链路问题
     - 不属于平台结果提取问题
   - 当前已知案例：
     - 切到正式 tag `v0.3.24` 后，这类问题从 `0/0/7/0` 改善到 `8/10/5/7`

2. `external runner / judge-export` 模块问题
   - 典型症状：
     - `qa_total=35`
     - `qa_results.csv` 里也有第 35 行
     - 但最后一行 `result` / `reasoning` 为空
   - 归因：
     - 这是外部 benchmark runner 或 judge 落盘不完整
     - 根因不在 OpenViking 版本
   - 典型例子：
     - `conv-26-q102` 行存在，回答存在，但未写出 judge 结果

3. `platform external-result-import` 模块问题
   - 典型症状：
     - 原始 `qa_results.csv` 有 35 行
     - 平台 `case_results/summary` 只有 34 条
   - 归因：
     - 这是平台对接模块问题
     - 旧逻辑会把空 `result` 行静默 `continue` 掉
   - 修复原则：
     - 不要丢题
     - 应保留为 `label=ungraded`
     - `summary.case_total` 必须与原始 `total_questions` 对齐

## 快速分流

- `memories=0` 大量出现
  - 先查 `OpenViking / memory-capture`
- `qa_results.csv` 最后一行存在，但 `result` 为空
  - 先查 `external runner / judge-export`
- `qa_results.csv` 有 35 行，但平台只导入 34 行
  - 先查 `platform external-result-import`
