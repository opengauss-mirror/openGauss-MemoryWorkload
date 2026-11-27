# BEAM 测试

使用用户准备的 BEAM 发布数据，支持单条 JSON、JSON 数组及 JSONL，不会自动下载数据。
每条记录需要 conversation_id、chat、probing_questions。chat 支持平铺消息、原始二维消息列表、包含 turns 的批次和 10M 数据的 plan-* 分组。
probing_questions 可以是对象、JSON 字符串或官方发布使用的 Python 字典字符串，每个问题需包含 question 和非空文本列表 rubric。

历史按原顺序写入，同一 user/assistant 轮次保持完整。普通批次目标为 12000 字符，超长单轮保持完整。
time_anchor 保留在写入正文中；消息没有自身时间时继承所在批次时间，消息自身时间优先。可解析的 ISO 日期/时间及常见英文月份日期会同步到事件 timestamp，供现有适配器传递 occurred_at 等原生历史日期字段。仅在完整轮次之间按该轮 user 的日期变化拆分写入；轮次内各消息的日期都保留在正文中。事件 timestamp 采用该轮 user 的日期，user 未提供日期时继承批次日期；无法解析的时间只保留正文，timestamp 留空。问题与 rubric 仅用于最终 QA 和评分，rubric 不写入记忆。

## 运行

先按对应记忆系统的现有文档启动后端并配置 Agent、模型和服务连接。
在仓库的 memory_bench_platform 目录执行，替换数据路径和模型接口。

~~~bash
export BEAM_BASE_URL=https://your-provider.example/v1
export BEAM_METRIC_MODEL=your-judge-model
read -rsp 'Judge API key: ' BEAM_API_KEY; echo
export BEAM_API_KEY

python -m memory_bench_platform.cli plan-run --benchmark beam --agent openclaw --memory-backend ogmemory --memory-integration backend_direct --data-path /path/to/beam.json
python -m memory_bench_platform.cli run --benchmark beam --agent openclaw --memory-backend ogmemory --memory-integration backend_direct --data-path /path/to/beam.json --run-id beam-run --qa-concurrency 2 --judge-concurrency 2
python -m memory_bench_platform.cli analyze-run --run-dir runs/beam-run
python -m memory_bench_platform.cli score-run --benchmark beam --run-dir runs/beam-run
~~~

Token 复用现有采集框架，需要按后端配置统计来源和覆盖范围。
此前的真实小样本验证使用 oGMemory backend_direct，两题均完成评分。
OpenClaw + oGMemory agent_plugin 已用两条 BEAM 结构的合成小样本验证多 Gateway 并发、身份隔离、QA、评分和 LLM Token 报告。其他系统的 BEAM 真实运行及完整规模尚未验证。

## 官方 Judge 与报告

先在 memory_bench_platform 目录安装评分可选依赖：

~~~bash
pip install -e '.[beam]'
~~~

使用 beam_official@1，官方提示词原文保存在 evaluation/judge.txt，来源与文件哈希见
evaluation/provenance.json。十类题目都对每条 rubric 独立请求 Judge，取各项 0、0.5、1 的平均分。
事件排序另按官方方式逐行读取答案、用 LLM 做一对一事实对齐，并计算 F1、归一化 Kendall tau-b
和 final_score。官方 report_results.py 对事件排序使用 tau_norm，对其余类别使用 llm_judge_score。

score-run 生成 reports/beam_scores.json。official_category_scores 先在每个样本/类别内取均值，
再对样本取均值，与官方分类报告的聚合顺序一致。mean_category_score 是这些分类均分的宏平均，
用作本工具补充汇总。mean_score 是全部已评分问题的微平均，样本题数不同时二者可能不同。
同时保留 total 和 graded，解读子集分数时需核对评分覆盖率。
manifest、场景、运行摘要和独立 scorer 统一以 mean_category_score 为主指标。通用 benchmark_score 与 BEAM 分类宏平均一致，满分题通过率单列为 full_score_pass_rate。分析报告使用 primary_metric 和 primary_score 展示该指标。
每题 rationale 保存官方逐项评分响应，以及事件排序的对齐和顺序指标。
reports/beam_official_inputs/ 导出样本答案与 rubric，可交给官方脚本复核。

Workload 复用现有模型接口、Judge 线程池、HTTP 重试和报告框架。
Judge 使用用户指定的模型。每题共享900秒总预算，各条 rubric 在该题内顺序评分。
Judge 请求不计入记忆侧或 Agent Token。官方评分比旧版一次请求全部 rubric 需要更多模型调用。
对非法数值严格报 Judge 错误；排序无法定义 Kendall tau 时记录评分失败，保留原答案。
日期、历史写入、记忆提取和 QA 使用现有流程。与旧版自写通用 rubric Judge 的分数应区分解读。

## agent_plugin 跨样本并发

复用支持多 Gateway 的 OpenClaw 组合部署模板，将 combination 的 benchmark 改为 beam，
data_path 指向 BEAM JSON/JSONL，并在该 combination 中配置：

~~~yaml
execution:
  sample_concurrency: 2
  qa_concurrency: 2
  judge_concurrency: 4
~~~

按原流程运行 memory-bench experiment --config deployment.yaml。
Gateway 数量取样本数与 sample_concurrency 的较小值，每个 Gateway 同时处理一个样本。
各 Gateway 复用现有适配器的记忆隔离规则。每个样本内顺序写入并等待就绪，最后执行 QA。
QA 并发由实际 Gateway 数量约束；单样本会收敛到一个 Gateway 和一路 QA。
记忆侧独占计数器仍按整轮统计一次。其他 Token 来源沿用现有配置及去重机制。

## 数据来源与评分契约

每轮场景 metadata.dataset 保存实际输入文件名、SHA-256 和可选发布标识。
设置 BEAM_DATA_REVISION 可记录数据来源标签；还可以使用现有 CLI 参数
--version-override beam-release-data=发布标识。未指定时不会推测数据版本。
这些信息随 records/benchmark_scenario.json 归档，评分源码 provenance 仅标识评分实现。

manifest 的 judging.handler 声明评分实现，平台传入运行契约加载的提示词，
因此替换 prompt_template 会同步改变实际 Judge 请求。
scorer 返回标准 primary_metric、metrics、artifacts，同时继续保存 BEAM 专用报告。

缺失答案由公共 Judge 标记为 missing-answer、零分且答案为空时，BEAM 报告接受该结果并计入零分；其他已评分结果继续校验官方评分来源。

## Judge 扩展协议

judging.handler_protocol 必须声明 judge-handler/1，脚本必须导出同版本 PROTOCOL_VERSION，
evaluate(request, invoke) 和 aggregate(request)。平台在构建契约时检查版本及可调用签名。
这是进程内 Python 扩展接口，request 和 response 使用公开的 JudgeRequest/JudgeResponse 模型，
均可序列化为 JSON；invoke 由平台提供，用于共享模型请求、重试和时间预算。
evaluate 返回含 protocol_version、有限的 0–1 score、rationale 的对象，平台统一校验。
aggregate 使用版本化 AggregationRequest/AggregationResponse 模型，请求包含带 sample_id、category、score 的记录列表，响应返回声明的 primary_metric、value 和指标。
入口代码是可信 Skill 代码，加载检查会执行模块顶层，因此模块顶层不能启动模型请求。

独立 scorer 优先使用 records/run_contract.json 归档的 Judge 配置；没有归档契约的旧运行才解析当前 manifest。归档入口缺失、协议错误或主指标声明不匹配时明确报错，不会静默切换实现。运行时和独立 scorer 共用入口解析及聚合响应校验。

score-run 可带或省略 --data-path；BEAM scorer 接受该可选参数以兼容统一 CLI，评分和复核导出均使用该轮运行归档，不重新读取传入的数据路径。

归档的 Judge 入口记录 Skill 标识、相对路径、协议版本及实现文件 SHA-256。
独立 scorer 从已安装的对应 Skill 目录解析相对路径，执行前检查目录边界及摘要。
新归档在实现内容一致时可以跨工作区复核；旧归档没有摘要时仅兼容原可信位置。
实现缺失、越界、Skill 不匹配或摘要变化时明确失败，不改用当前 manifest 中的其他入口。
