# Benchmark Skill 接入模板

这个模板把原始数据整理成统一的 `BenchmarkScenario`。一个 `ScenarioSample` 表示一个完整的 Memory Episode；同一 Sample 内的多段 Session 和多个 Checkpoint 共享记忆，不同 Sample 由 Runtime 自动隔离。

接入时只需要替换 `scripts/build_scenario.py` 中的数据解析逻辑，并配置 `manifest.yaml` 的评测 Profile 和 Prompt。Builder 不应调用 Agent、Memory、Commit 或 Wait；这些运行步骤由 Composer 和 Runtime Adapter 生成、执行。

先运行 Builder 生成 Scenario，再用 Golden Test 核对 Episode 分组、时间顺序、Checkpoint、问题、答案和类别是否保持正确。
