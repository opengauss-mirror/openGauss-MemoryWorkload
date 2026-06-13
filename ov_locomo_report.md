# OpenClaw 上下文引擎评测报告：OpenViking(V2) vs MemoryCore(+Embedding)

## 一、测试配置

**公共配置：**


| 项目        | 配置                                                    |
| --------- | ----------------------------------------------------- |
| 推理模型      | doubao-seed-2-0-code-preview-260215 (volcengine-plan) |
| Judge 模型  | doubao-seed-2-0-code-preview-260215 (coding endpoint) |
| 数据集       | LoCoMo (locomo10.json)                                |
| 测试样本      | 6 个（conv-26/30/41/42/43/44），共 885 题                   |
| Embedding | doubao-embedding-vision-251215                        |


**差异配置：**


| 项目    | OpenViking(V2)                  | MemoryCore(+Embedding)                       |
| ----- | ------------------------------- | -------------------------------------------- |
| 上下文引擎 | OpenViking-plugin               | OpenClaw MemoryCore                          |
| 记忆管理  | openviking memory v2 compressor | hybrid search (vector 0.7 + text 0.3, top 6) |


> 注1：OpenClaw + MemoryCore 的记忆提取路径有 1） Agent 显式写 memory 文件的，2） 由 compaction 前的 memory flush 触发。LoCoMo 测试场景下，compact 未触发、未生成记忆文件，导致无法检索记忆（sample 0 验证准确率 0%），因此本测试为 MemoryCore 添加提示词，引导生成记忆文件、搜索记忆文件。

> 注2：OpenClaw + MemoryCore 配置embedding 模型，使能检索功能

---

## 二、测试结论

基于 6 个公共样本（885 题）的同口径对比，OpenViking相比MemoryCore准确率提升5.8%，Token节约59.7%


| 指标            | MC            | OV                | 差异          |
| ------------- | ------------- | ----------------- | ----------- |
| **准确率**       | 68.9%         | **74.7%**         | OV 提升 5.8%  |
| **Token 总消耗** | 4,269 万 0,117 | **1,718 万 3,492** | OV 节省 59.7% |
| **Cache 命中率** | **88.8%**     | 72.0%             | MC 高 16.8%  |


> 注：OV 的 Token 包含 Import 阶段（记忆导入/压缩）和 QA 阶段的消耗；MemoryCore 仅包含 QA 阶段消耗，不包含 Import 阶段（当前无法统计记忆提取Token 消耗），即MemoryCore实际Token开销会更大。

---

## 三、详细数据对比

### 3.1 各样本准确率


| Sample       | MC        | OV        | 差异             | 优势方    |
| ------------ | --------- | --------- | -------------- | ------ |
| conv-26      | 66.4%     | 87.5%     | OV 提升 21.1%    | OV     |
| conv-30      | 71.6%     | 77.8%     | OV 提升 6.2%     | OV     |
| conv-41      | 79.6%     | 75.7%     | MC 高 3.9%      | MC     |
| conv-42      | 65.8%     | 62.3%     | MC 高 3.5%      | MC     |
| conv-43      | 63.5%     | 72.5%     | OV 提升 9.0%     | OV     |
| conv-44      | 69.9%     | 78.9%     | OV 提升 9.0%     | OV     |
| **整体（6 样本）** | **68.9%** | **74.7%** | **OV 提升 5.8%** | **OV** |


OV 在4个样本上都优于MC。OV 在 conv-26 上优势最大（提升 21.1%），MC 在 conv-41/42 上略优（高 3~4%）。

### 3.2 各类别准确率（6 样本）


| Category    | MC                  | OV                  | 差异             |
| ----------- | ------------------- | ------------------- | -------------- |
| Single-hop  | 53.5% (92/172)      | 64.5% (111/172)     | OV 提升 11.0%    |
| Multi-hop   | 78.3% (141/180)     | 73.9% (133/180)     | MC 高 4.4%      |
| Temporal    | 54.7% (29/53)       | 58.5% (31/53)       | OV 提升 3.8%     |
| Open-domain | 72.5% (348/480)     | 80.4% (386/480)     | OV 提升 7.9%     |
| **Overall** | **68.9% (610/885)** | **74.7% (661/885)** | **OV 提升 5.8%** |


OV 在 4 个类别中 3 个领先。Single-hop 和 Open-domain 提升显著，MC 仅在 Multi-hop 上有优势（高 4.4%）。

### 3.3 Token 消耗（6 样本）


| 指标                            | MC             | OV             | 差异              |
| ----------------------------- | -------------- | -------------- | --------------- |
| QA Input (no-cache)           | 4,777,701      | 3,862,710      | OV 节省 19.1%     |
| QA Cache Read                 | 37,912,416     | 9,942,272      | OV 节省 73.8%     |
| QA Prompt Total (Input+Cache) | 42,690,117     | 13,804,982     | **OV 节省 67.7%** |
| OV Import (Embed+VLM)         | 未统计            | 3,378,510      | —               |
| **总计（不含 Output）**             | **42,690,117** | **17,183,492** | **OV 节省 59.7%** |
|                               |                |                |                 |
| QA Output                     | 588,690        | 35,235         | OV 节省 94.0%     |


**各样本 Token 消耗（不含 Output）：**


| Sample  | MC Prompt Total | OV QA Prompt   | OV Import     | OV Total       | 差异              |
| ------- | --------------- | -------------- | ------------- | -------------- | --------------- |
| conv-26 | 7,545,711       | 2,197,924      | 387,589       | 2,585,513      | OV 节省 65.7%     |
| conv-30 | 3,812,703       | 1,107,323      | 424,188       | 1,531,511      | OV 节省 59.8%     |
| conv-41 | 6,190,596       | 2,270,103      | 669,761       | 2,939,864      | OV 节省 52.5%     |
| conv-42 | 10,539,844      | 2,608,765      | 656,621       | 3,265,386      | OV 节省 69.0%     |
| conv-43 | 9,169,549       | 2,859,339      | 697,387       | 3,556,726      | OV 节省 61.2%     |
| conv-44 | 5,431,714       | 2,761,528      | 542,964       | 3,304,492      | OV 节省 39.2%     |
| **总计**  | **42,690,117**  | **13,804,982** | **3,378,510** | **17,183,492** | **OV 节省 59.7%** |


OV 在所有样本上 Token 消耗均低于 MC，节省幅度从 39.2%（conv-44）到 69.0%（conv-42），整体节省 59.7%。其中 OV Import 阶段额外消耗约 338 万 Token，但 QA Prompt 大幅缩减弥补了这一开销。

### 3.4 Cache 命中率


| 指标                        | MC    | OV    |
| ------------------------- | ----- | ----- |
| Cache Read / Prompt Total | 88.8% | 72.0% |


MC 的 Cache 命中率高于 OV 16.8%。MC 采用完整上下文窗口，历史对话复用率高；OV 经过记忆压缩后上下文更短，Cache 可复用部分相应减少，但总 Token 消耗仍大幅低于 MC。

### 3.5 各样本分类别详细对比

conv-26 — OV 87.5% vs MC 66.4%（OV 提升 21.1%）


| Category    | MC    | OV    | 差异          |
| ----------- | ----- | ----- | ----------- |
| Single-hop  | 46.9% | 84.4% | OV 提升 37.5% |
| Multi-hop   | 83.8% | 86.5% | OV 提升 2.7%  |
| Temporal    | 76.9% | 92.3% | OV 提升 15.4% |
| Open-domain | 64.3% | 88.6% | OV 提升 24.3% |


conv-30 — OV 77.8% vs MC 71.6%（OV 提升 6.2%）


| Category    | MC    | OV    | 差异          |
| ----------- | ----- | ----- | ----------- |
| Single-hop  | 72.7% | 54.5% | MC 高 18.2%  |
| Multi-hop   | 88.5% | 84.6% | MC 高 3.9%   |
| Open-domain | 61.4% | 79.5% | OV 提升 18.1% |


conv-41 — MC 79.6% vs OV 75.7%（MC 高 3.9%）


| Category    | MC    | OV    | 差异          |
| ----------- | ----- | ----- | ----------- |
| Single-hop  | 67.7% | 61.3% | MC 高 6.4%   |
| Multi-hop   | 81.5% | 70.4% | MC 高 11.1%  |
| Temporal    | 50.0% | 62.5% | OV 提升 12.5% |
| Open-domain | 86.0% | 83.7% | MC 高 2.3%   |


conv-42 — MC 65.8% vs OV 62.3%（MC 高 3.5%）


| Category    | MC    | OV    | 差异          |
| ----------- | ----- | ----- | ----------- |
| Single-hop  | 48.6% | 48.6% | 持平          |
| Multi-hop   | 77.5% | 57.5% | MC 高 20.0%  |
| Temporal    | 36.4% | 54.5% | OV 提升 18.1% |
| Open-domain | 70.3% | 69.4% | MC 高 0.9%   |


conv-43 — OV 72.5% vs MC 63.5%（OV 提升 9.0%）


| Category    | MC    | OV    | 差异          |
| ----------- | ----- | ----- | ----------- |
| Single-hop  | 38.7% | 54.8% | OV 提升 16.1% |
| Multi-hop   | 61.5% | 69.2% | OV 提升 7.7%  |
| Temporal    | 57.1% | 50.0% | MC 高 7.1%   |
| Open-domain | 72.0% | 81.3% | OV 提升 9.3%  |


conv-44 — OV 78.9% vs MC 69.9%（OV 提升 9.0%）


| Category    | MC    | OV    | 差异          |
| ----------- | ----- | ----- | ----------- |
| Single-hop  | 60.0% | 80.0% | OV 提升 20.0% |
| Multi-hop   | 75.0% | 79.2% | OV 提升 4.2%  |
| Temporal    | 42.9% | 14.3% | MC 高 28.6%  |
| Open-domain | 75.8% | 85.5% | OV 提升 9.7%  |


6 个样本中，OV 在 4 个样本整体领先，MC 在 2 个样本（conv-41/42）略优。OV 在 Single-hop 和 Open-domain 类别上普遍表现更好，MC 在 Multi-hop 推理上有一定优势。

## 四、测试流程说明

### 4.1 测试代码

- 仓库：[https://github.com/wlff123/OpenViking/tree/ov_test_0407](https://github.com/wlff123/OpenViking/tree/ov_test_0407)
- OV 测试脚本目录：`benchmark-ov/locomo/openclaw/`
- MemCore 测试脚本目录：`benchmark-mc/locomo/openclaw/`

### 4.2 测试步骤

**OpenViking(V2) 测试流程：**

1. **环境准备**：启动 OpenClaw Gateway 和 OpenViking（remote 模式，独立进程）
2. **数据导入（Import）**：将 LoCoMo 数据集的对话历史导入 OpenViking 进行记忆压缩（`import_to_ov.py`），同时通过 OpenClaw 导入对话数据（`eval.py ingest`）
3. **QA 评估**：对每个样本的所有问题，调用 OpenClaw Gateway 进行问答（`eval.py qa`，并发度 3）
4. **Judge 评分**：使用 doubao LLM 对 QA 结果进行自动评分（`judge.py`，并发度 5）
5. **结果统计**：汇总各样本、各类别的准确率和 Token 消耗（`stat_judge_result.py`）

以上步骤由 `run_full_eval.sh` 脚本自动编排执行（除环境准备外）：

```bash
bash run_full_eval.sh --with-claw-import
```

**MemoryCore(+Embedding) 测试流程：**

1. **环境准备**：根据 `config.toml` 自动停止旧 Gateway → 清理环境（归档旧数据 + 清除 session/memory/索引）→ 生成 `openclaw.json` 配置 → 启动 OpenClaw Gateway（独立窗口进程）
2. **数据注入（Ingest）**：将 LoCoMo 数据集的对话历史逐轮注入 OpenClaw Agent（`eval.py ingest`），每轮会话注入后触发 MemCore 记忆压缩，自动提取关键事实并按日期写入 `memory/YYYY-MM-DD.md` 文件，同时通过 Embedding 模型建立向量索引
3. **QA 评估**：对每个样本的所有问题，调用 OpenClaw Gateway 进行问答（`eval.py qa`，并发度 5）。每个问题使用独立 session 避免并发冲突，prompt 注入时间上下文引导 Agent 先检索记忆再回答
4. **Judge 评分**：使用 doubao LLM 对 QA 结果进行自动评分（`judge.py`，并发度 10）
5. **结果统计**：汇总各样本、各类别的准确率和 Token 消耗（`stat_judge_result.py`）
6. **数据归档**：将 memory 文件、session 记录、QA 结果、OpenClaw 配置等完整归档到 `archive/` 目录

以上步骤由 `run_benchmark.py` 脚本自动编排执行：

```bash
python run_benchmark.py --config config.toml
```

支持 `--resume` 参数在中断后从断点继续（跳过清理步骤，自动补跑未完成的问题）。

## 五、补充说明

### 5.1 无提示词的MemoryCore测试结果


| Sample | Conv ID | 问题数 | 准确率       | Input (no-cache) | Cache Read | Output | Total Input |
| ------ | ------- | --- | --------- | ---------------- | ---------- | ------ | ----------- |
| 0      | conv-26 | 152 | **0.00%** | 167,443          | 0          | 5,347  | 167,443     |
| 8      | conv-49 | 156 | **0.00%** | 194,572          | 0          | 10,299 | 194,572     |


无提示词时 MemoryCore 未生成记忆文件，compact 也未触发 memory flush，导致记忆检索无内容可返回，所有问题准确率为 0%。