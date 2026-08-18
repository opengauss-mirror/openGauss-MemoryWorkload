# Result Analyzer Skill

负责对 `memory_bench_platform` 的 run 目录做结果分析，输出统一的结构化分析结果和人工可读结论。

## 输入

- `run.json`
- `reports/summary.json`
- `reports/case_results.json`
- `reports/external_result_summary.json`（可选）
- `artifacts/monitor/cpu_status.csv`（可选）

## 输出

- `reports/analysis.json`
- `reports/analysis.md`

## 当前边界

- 该 skill 只承担说明和扩展注册职责。
- 当前不进入 benchmark / agent skill 的统一加载与执行选择流程。
- 当前实现由 `memory_bench_platform.result_analysis` 提供。

## 第一版分析重点

- 汇总 run 级 accuracy、通过数、失败数
- 提取 category summary
- 对失败样本做规则化归因
- 提取 CPU 运行摘要
- 输出人工可读 Markdown 报告
