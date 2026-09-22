# Multi-user Replay Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; execute inline in the existing feature worktree.

**Goal:** 配置多个独立用户的负载和实例，并产出每用户性能报告。
**Architecture:** 单协调器管理阶段和实例；配置、执行适配器、调度、报告分离；复用单用户回放计时和 Memory Skill 协议。
**Tech Stack:** Python 3.11、Pydantic 2、PyYAML、ThreadPoolExecutor、subprocess、pytest。
**Spec:** docs/superpowers/specs/2026-09-22-multi-user-replay-design.md

## Global Constraints

- 保留旧 --data-path 路径；新增 --workload-config 与之互斥。
- 用户到实例固定绑定，用户间并发、同用户会话顺序执行；全局 add/drain/search 屏障。
- 实例环境只通过显式参数传递，不修改进程环境。
- OpenClaw 使用明确的 openmem-agent/1 映射；未知语义字段预检失败。
- 外部服务不停止；managed 服务只清理本轮启动的进程组。
- 不保存原始内容、凭据或私有端点；缺少内部证据不报告可信归因。

## Review Focus

- 同文件多用户、跨运行身份不冲突。
- 共享 OpenClaw 插件全局配置不被每用户覆盖。
- 慢用户、坏响应和启动失败不丢失其他用户的指标。
- 配置路径相对文件解析，拒绝未知字段、非有限数和错误引用。
- 无界队列、实例环境串扰、错误百分位聚合与敏感内容归档。

## Task 1: Configuration and preflight

Files: multi_user_config.py, tests/test_multi_user_replay.py.
Interface: load_config(path) -> MultiUserConfig; resolved workload paths and user/instance bindings.

- [ ] 先编写重复用户、缺失引用、相对文件和未知字段测试。
- [ ] `pytest -q tests/test_multi_user_replay.py -k config`，确认失败后实现严格模型。
- [ ] 逐行预检 JSONL，OpenClaw 字段仅允许已定义映射；校验 Memory Skill 协议。
- [ ] 运行相同测试验证通过。

## Task 2: Execution and owned processes

Files: multi_user_runtime.py, multi_user_replay.py, integration.py.
Interfaces: InstanceRuntime.start()/stop()/set_phase(); run_multi_user(config, output_dir, run_id).

- [ ] 编写两个假 HTTP/Memory 实例路由测试、共享实例并发上限测试。
- [ ] 增加显式 environment/timeout 参数，复用 request_id 和有界超时。
- [ ] 单线程协调器公平派发每用户下一请求，线程池仅执行已获实例槽的任务。
- [ ] 按实例管理 prepare/set_phase/finalize，执行隔离 probe；不支持作用域隔离时失败。
- [ ] 管理子进程组和健康检查，finally 清理并保留部分事件。
- [ ] 测试用户失败、启动失败、超时和中断清理。

## Task 3: CLI, reports and documentation

Files: cli.py, multi_user_report.py, external_report_import.py, README.md, example YAML.

- [ ] 增加 --workload-config 专用路由，校验 mode 与 --agent 的一致性。
- [ ] 请求事件增量落盘，汇总生成用户/实例/整轮 JSON、CSV 与 HTML。
- [ ] 从原始事件计算百分位；QPS 使用各分组实际阶段时间。
- [ ] 报告导入校验字段与类型，复用平台分析入口。
- [ ] 加入 CLI 到报告的端到端测试、内容哨兵和旧入口兼容测试。

## Task 4: Verification and delivery

- [ ] 运行新增测试及平台/LoCoMo 回归测试；区分已知 macOS /proc 限制。
- [ ] 自审每个目标与配置示例，执行 git diff --check。
- [ ] 用当前仓库邮箱提交并更新已授权的 real-workload PR 分支。
