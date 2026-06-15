# LoCoMo Benchmark Skill

负责将 LoCoMo 数据集整理为平台统一的多轮记忆任务。

## 外部 runner 约束

- `official_small` 这类 external runner 依赖真实 `OpenClaw/OpenViking` 环境。
- 默认要求被测软件使用“当前最新正式 release tag”。
- 如果使用非正式版本，必须在 run 记录或分析报告中显式写明原因。

## 版本优先级

1. 用户明确指定的正式版本
2. 上游仓库当前最新正式 release tag
3. 经验证的回退正式 tag

不应默认使用：

- `dirty` 工作树
- `dev` 版本号
- 只在本地提交未发布的 commit

## 对结果解释的影响

- 若 `LoCoMo small` 出现大面积 `memories=0`：
  - 优先检查 `OpenViking` 版本/配置/写入链路
- 若 `qa_results.csv` 与平台 `summary/case_results` 数量不一致：
  - 归类为 external runner 导出或平台 importer 问题
  - 不应再归因到 `OpenViking tag` 本身
