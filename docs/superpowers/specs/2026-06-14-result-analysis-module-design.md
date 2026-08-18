# 结果分析模块设计说明

## 1. 背景

当前 `memory_bench_platform` 已具备统一 run 目录、结构化报告落盘、外部 benchmark 结果导入、CPU 资源采样等能力，但仍缺少统一的“结果分析层”。

现状问题有两类：

1. 已有 run 只能查看 `reports/summary.json`、`reports/case_results.json`、`reports/external_result_summary.json` 等原始结果，缺少统一的失败归因、资源摘要整理和结论输出。
2. benchmark 运行完成后不会自动生成“为什么成功率高/低”的分析产物，用户需要人工翻日志、翻 CSV、翻 JSON 才能得到结论。

本设计目标是在不破坏现有 benchmark skill / agent skill 双侧边界的前提下，补上一层可复用的分析模块，并支持：

- 对既有 run 目录做离线分析
- 在平台 `run` 主流程结束后自动生成分析结果
- 对 LoCoMo 这类外部 runner 导入结果提供第一版规则化失败归因
- 为未来 LongMemEval、更多 benchmark、更多 agent 保留扩展点

## 2. 设计目标

### 2.1 功能目标

1. 新增统一结果分析模块，输入 `run_dir`，输出结构化分析结果。
2. 新增 CLI 离线入口，可对既有 run 执行分析。
3. 在平台 `run` 主流程中自动调用分析模块，将分析结果落入 `reports/`。
4. 第一版覆盖 LoCoMo / 外部导入结果的失败归因、分类统计、CPU 运行信息摘要。
5. 提供人可读说明和机器可读配置，保存为 analysis skill，用于约束分析输入输出和后续扩展。

### 2.2 非目标

1. 第一版不把 analysis skill 纳入与 benchmark / agent 完全同级的执行协议，不新增第三套 orchestrator 运行模型。
2. 第一版不尝试做通用 LLM 评审或复杂语义聚类，先以规则化分析为主。
3. 第一版不重写已有评分逻辑，不改变 benchmark 的 judge 输出。
4. 第一版不引入新的远端执行依赖，不要求重新跑 benchmark 才能分析。

## 3. 总体方案

### 3.1 推荐方案

采用“平台核心分析模块 + analysis skill 描述层”的双层方案：

1. 平台核心新增 `result_analysis.py`
   - 负责读取 run 目录
   - 汇总 summary / case results / external results / monitor artifacts
   - 生成统一 `analysis.json` 和 `analysis.md`
2. CLI 增加 `analyze-run`
   - 对既有 run 目录做离线分析
3. `run` 主流程自动挂载分析
   - 在 `summary.json` 和 `case_results.json` 写出后自动调用分析模块
4. 新增 `skills/analysis/result-analyzer/`
   - `SKILL.md`：人可读说明
   - `manifest.yaml`：机器可读元信息

### 3.2 不采用的方案

不采用“把结果分析完全做成第三类执行 skill”的方案。原因是当前平台 skill 体系的核心是 benchmark 输入适配和 agent 执行适配，而结果分析属于 run 后处理。如果第一版就把 analysis 强行纳入独立执行协议，会增加平台复杂度，但不会显著提升实际交付价值。

## 4. 模块边界

### 4.1 Platform Core 负责

1. 发现并读取 run 目录中的结构化结果。
2. 按统一规则构造分析对象。
3. 汇总 CPU 资源摘要。
4. 生成并写出标准分析产物。
5. 在 `run` 主流程中自动触发分析。
6. 通过 CLI 提供离线分析入口。

### 4.2 Analysis Skill 负责

1. 描述分析模块支持哪些输入产物。
2. 描述分析模块输出哪些报告字段。
3. 描述第一版支持哪些 benchmark 特化归因器。
4. 作为人机共读的扩展说明，不直接承担运行控制。

### 4.3 Benchmark Skill 不负责

1. 不负责结果归因逻辑实现。
2. 不负责 CPU 摘要统计。
3. 不负责分析报告格式定义。

### 4.4 Agent Skill 不负责

1. 不负责失败归因。
2. 不负责 run 级结果汇总。
3. 不负责分析报告生成。

## 5. 输入与输出

### 5.1 输入

分析模块以 `run_dir` 为唯一入口，优先读取：

1. `run.json`
2. `reports/summary.json`
3. `reports/case_results.json`
4. `reports/external_result_summary.json`
5. `artifacts/monitor/cpu_status.csv`
6. 其他可选辅助文件
   - `logs/*.log`
   - `records/external_entrypoint.json`

### 5.2 输出

分析模块必须输出：

1. `reports/analysis.json`
2. `reports/analysis.md`

其中：

- `analysis.json` 面向程序消费
- `analysis.md` 面向人工查看和评审

## 6. 统一分析对象模型

第一版新增统一分析对象 `RunAnalysisSummary`，必须包含以下字段：

- `run_id`
- `benchmark_id`
- `agent_id`
- `entrypoint_kind`
- `status`
- `overall_accuracy`
- `case_total`
- `case_passed`
- `case_failed`
- `category_summary`
- `failure_summary`
- `failure_buckets`
- `resource_summary`
- `source_artifacts`
- `analysis_notes`

### 6.1 `failure_summary`

用于给出总量级统计，例如：

- `wrong_count`
- `correct_count`
- `retrieval_miss_count`
- `unsupported_no_info_count`
- `format_or_empty_count`
- `judge_mismatch_candidate_count`

### 6.2 `failure_buckets`

用于展示每类归因下的代表性失败样本。每个 bucket 下保存若干样本：

- `case_id`
- `question`
- `expected_answer`
- `response`
- `category`
- `reason`
- `bucket`

### 6.3 `resource_summary`

第一版至少输出 CPU 信息：

- `cpu_sample_count`
- `cpu_user_avg`
- `cpu_sys_avg`
- `cpu_idle_avg`
- `cpu_user_peak`
- `cpu_sys_peak`
- `cpu_idle_min`

若没有 `cpu_status.csv`，则保留空结构并记录缺失说明。

## 7. 第一版归因规则

### 7.1 通用规则

对失败样本按顺序匹配归因：

1. `format_or_empty`
   - 响应为空
   - 缺少判分所需关键字段
   - case 结果结构不完整
2. `retrieval_miss`
   - 回答高频出现“no information / no mention / don't have memory / not specified / 没有相关信息”等拒答或无记忆表达
3. `unsupported_no_info`
   - 回答非空，但明显未回答问题，只复述问题或给出极泛化陈述
4. `judge_mismatch_candidate`
   - 回答看起来包含事实，但仍被判错；第一版只做候选标记，不自动改分
5. `other`
   - 其他未匹配情况

### 7.2 LoCoMo 特化

LoCoMo 第一版分析重点识别两类现象：

1. 明显存在标准答案，但回答走“没有信息”分支
2. 外部 runner、`locomo_test`、平台导入结果之间的精度差异

因此在 `analysis_notes` 中补充：

- 当前结果是否更像召回缺失，而非 judge 过严
- 当前入口与导入结果是否一致
- 当前入口与其他入口是否存在可见差异

## 8. CLI 设计

新增子命令：

```bash
python3 -m memory_bench_platform.cli analyze-run --run-dir /path/to/run
```

行为要求：

1. 成功时输出 `run_dir` 或分析文件路径。
2. 默认覆盖生成 `reports/analysis.json` 和 `reports/analysis.md`。
3. 缺少非关键输入时尽量降级分析，不轻易整体失败。
4. 缺少 `summary.json` 或 `case_results.json` 这类关键文件时才报错退出。

## 9. 自动挂载点

分析模块挂载在 `run` 主流程的报告写出之后：

1. `write_summary(...)`
2. `write_case_results(...)`
3. 若有则 `write_external_result_summary(...)`
4. `analyze_run(run_dir)`

这样离线分析和自动分析共用同一实现，不分叉逻辑。

## 10. Skill 目录设计

新增目录：

```text
memory_bench_platform/skills/analysis/result-analyzer/
  SKILL.md
  manifest.yaml
```

`manifest.yaml` 必须包含以下字段：

- `kind: analysis`
- `id: result-analyzer`
- `version`
- `entry.module`
- `supports.benchmarks`
- `supports.sources`
- `outputs`

这类 skill 当前只作为描述层和扩展注册点，不进入 benchmark/agent 执行选择流程。

## 11. 代码落点

本设计第一版必须新增或修改以下文件：

### 新增

- `memory_bench_platform/memory_bench_platform/result_analysis.py`
- `memory_bench_platform/tests/test_result_analysis.py`
- `memory_bench_platform/skills/analysis/result-analyzer/SKILL.md`
- `memory_bench_platform/skills/analysis/result-analyzer/manifest.yaml`

### 修改

- `memory_bench_platform/memory_bench_platform/cli.py`
- `memory_bench_platform/memory_bench_platform/reporter.py`
- `memory_bench_platform/README.md`

## 12. 测试策略

### 12.1 单元测试

覆盖以下场景：

1. 能从最小 run 目录读取 `summary.json` 和 `case_results.json`
2. 能识别 `retrieval_miss`
3. 能识别 `format_or_empty`
4. 能汇总 `cpu_status.csv` 的均值和峰值
5. CLI `analyze-run` 子命令可见
6. CLI `analyze-run` 能成功生成 `analysis.json`

### 12.2 集成测试

覆盖以下场景：

1. `run` 外部导入路径结束后自动生成 `analysis.json`
2. `run` case-source 原生路径结束后自动生成 `analysis.json`

### 12.3 实际结果验证

使用当前已有 LoCoMo run 做离线分析，验证能产出：

1. 统一 accuracy 摘要
2. CPU 摘要
3. 失败归因 bucket
4. Markdown 版分析报告

## 13. 风险与约束

1. 第一版规则归因不保证语义完美，只保证“可解释、可复核、可扩展”。
2. 现有 `case_results.json` 来源存在两种：
   - 原生 workflow judge 输出
   - 外部 runner 导入输出
   因此分析器必须兼容字段差异。
3. 某些 run 没有 CPU 采样文件，分析器必须降级。
4. `analysis skill` 第一版只是描述层，不应反向侵入 orchestrator 主协议。

## 14. 结论

第一版结果分析模块应作为平台核心后处理能力实现，再辅以轻量 analysis skill 作为说明和扩展注册点。这样既能立刻分析现有 LoCoMo 结果，又能自动挂入 benchmark 平台闭环，满足“先离线分析，再平台自动产出”的目标，并且不会破坏当前 benchmark skill / agent skill 的职责边界。
