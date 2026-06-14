# 测试入口验证结果

## 1. 当前状态

当前三条入口都已经形成可复用路径，但稳定性等级不同：

- `official wrapper`
  - 已完成独占端到端验证。
- `locomo_test` remote wrapper
  - 已完成独占端到端验证。
- `memory_bench_platform` external runner
  - 已完成 external runner 调用、结果导入、恢复模式验证。
  - 已完成 from-scratch 完整 `35` 题收尾验证。

## 2. 验证结果

### 2.1 official wrapper

- 入口：`tools/test_entrypoints/run_official_locomo_small.sh`
- 产物目录：`/tmp/official_on_sample0_20260614_155220`
- 结果：`7/35 = 20.00%`
- 关键产物：
  - `/tmp/official_on_sample0_20260614_155220/meta.json`
  - `/tmp/official_on_sample0_20260614_155220/qa_results.csv`
  - `/tmp/official_on_sample0_20260614_155220/remote_logs/official_on_sample0_20260614_155220.master.log`

### 2.2 locomo_test remote wrapper

- 入口：`tools/test_entrypoints/run_locomo_test_remote.sh`
- 产物目录：`/tmp/locomo_test_output/locomo_test_remote_20260614_161053`
- 结果：`9/35 = 25.71%`
- 关键产物：
  - `/tmp/locomo_test_output/locomo_test_remote_20260614_161053/meta.json`
  - `/tmp/locomo_test_output/locomo_test_remote_20260614_161053/qa_results.csv`
  - `/tmp/locomo_test_output/locomo_test_remote_20260614_161053/pipeline.log`

### 2.3 memory_bench_platform external runner

- 恢复模式入口：
  - `python3 -m memory_bench_platform.cli run --benchmark locomo --agent openclaw --entrypoint official_small --run-id locomo-openclaw-importprobe`
- 产物目录：
  - `/mnt/d/code/Agent/test/memory_bench_platform/runs/locomo-openclaw-importprobe`
- 导入结果：`7/35 = 20.00%`
- 关键产物：
  - `runs/locomo-openclaw-importprobe/run.json`
  - `runs/locomo-openclaw-importprobe/records/external_entrypoint.json`
  - `runs/locomo-openclaw-importprobe/reports/summary.json`
  - `runs/locomo-openclaw-importprobe/reports/external_result_summary.json`
  - `runs/locomo-openclaw-importprobe/reports/case_results.json`

- from-scratch 收尾入口：
  - `python3 -m memory_bench_platform.cli run --benchmark locomo --agent openclaw --entrypoint official_small --run-id locomo-openclaw-fromscratch-full`
- 产物目录：
  - `/mnt/d/code/Agent/test/memory_bench_platform/runs/locomo-openclaw-fromscratch-full`
- 当前导入结果：`7/35 = 20.00%`
- 关键产物：
  - `runs/locomo-openclaw-fromscratch-full/run.json`
  - `runs/locomo-openclaw-fromscratch-full/records/external_entrypoint.json`
  - `runs/locomo-openclaw-fromscratch-full/reports/summary.json`
  - `runs/locomo-openclaw-fromscratch-full/reports/external_result_summary.json`
  - `runs/locomo-openclaw-fromscratch-full/reports/case_results.json`

## 3. 当前推荐

1. 正式 LoCoMo small 跑数：优先使用 `run_locomo_test_remote.sh`
2. 官方 baseline / direct-ov 对照：使用 `run_official_locomo_small.sh`
3. 平台归档与统一报告：使用 `memory_bench_platform` external runner

## 4. 仍待收敛

- `official wrapper` 与 `locomo_test` 当前结果仍不一致：`20.00%` vs `25.71%`
- `official wrapper` 与 `locomo_test` 当前结果仍不一致：`20.00%` vs `25.71%`
- `memory_bench_platform` from-scratch 与 `official wrapper` 当前对齐到 `20.00%`
- `locomo_test` 的 `memory_token_totals` / `ov_token_totals` 仍未恢复成可信统计
