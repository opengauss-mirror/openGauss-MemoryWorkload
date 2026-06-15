# OpenViking Memory Write Diagnosis

用于排查 `OpenClaw/OpenViking` 评测里“分数异常低、回答频繁说无信息、怀疑 memory 没写进去”的问题。

## 适用场景

- LoCoMo / LongMemEval 跑分明显异常
- `analysis.json` 中 `retrieval_miss` 很高
- 需要判断问题在平台、agent、还是 OpenViking 写入链路

## 核心证据路径

1. 先看 `reports/analysis.json`
   - `failure_summary`
   - `failure_buckets`
   - `ingest_summary`
2. 再看 `reports/case_results.json`
   - 确认错误回答是否大量落在“无信息/未提到/没记忆”
3. 若是 external runner，继续看：
   - `external_artifacts/*/remote_logs/*.master.log`
   - 重点找 `memories=N`
4. 若 `session_total > 0` 且大量 `memories=0`
   - 优先判断为 memory 写入/抽取异常
   - 不要先归咎 judge

## 关键判断规则

- `accuracy` 低，但 `qa_results/case_results` 中大量回答是“没有信息”
  - 优先判定为 `retrieval_miss`
- `master.log` 显示多个 session `memories=0`
  - 优先判定为写入链路异常
- 平台 `summary/case_results/analysis` 与原始 `qa_results/meta` 一致
  - 不应继续怀疑平台算分

## 当前已知经验

- LoCoMo `official_small` 异常案例中，`memories=0/0/7/0` 这种分布说明大多数 session 根本没写入 memory。
- 如果 agent 回答中出现 “recalled memories don't mention ...” 这类模式，高概率不是 judge 过严，而是 recall 没拿到目标事实。
