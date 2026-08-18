# Memory Bench Platform 剩余架构债务清单

更新时间：2026-06-30

## 当前结论

当前代码已经形成三层：

1. `memory_bench_platform`
   职责：测试骨架、skill 装载、run 落盘、报告与资源监控。
2. `skills/*`
   职责：声明 benchmark / agent / memory / smoke 的机器可读契约与接入入口。
3. `locomo_test`
   职责：LoCoMo + OpenClaw + OpenViking 的现行实例化执行链路。

现阶段的主要问题，不再是“有没有分层”，而是“平台骨架里仍残留 LoCoMo/OpenViking 特化桥接逻辑，`locomo_test` 内部也仍混着 benchmark 逻辑、agent/memory 后端逻辑与诊断逻辑”。

---

## 已在本轮推进的收敛项

### 1. 平台核心补齐三 skill 组合契约

- 文件：`/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/integration.py`
- 已做内容：
  - 新增 `get_memory_manifest()`
  - 新增 `RunSkillBundle`
  - 新增 `resolve_run_skill_bundle()`
  - 新增 `build_run_contract()`
  - 新增 benchmark / agent / memory 的兼容性校验
- 目的：
  - 平台内核不再只知道 benchmark + agent；
  - `memory skill` 不再只是 agent manifest 里的字符串，而是被平台显式解析与校验；
  - 为后续把 `locomo_test` 作为“外部 benchmark 实例入口”接回平台提供统一契约。

### 2. LoCoMo 平台桥接实现迁移到 adapter namespace

- 文件：
  - `memory_bench_platform/memory_bench_platform/adapters/locomo/artifacts.py`
  - `memory_bench_platform/memory_bench_platform/adapters/locomo/diagnostics.py`
  - `memory_bench_platform/memory_bench_platform/adapters/locomo/metrics_bridge.py`
  - `memory_bench_platform/memory_bench_platform/adapters/locomo/report_bridge.py`
  - `memory_bench_platform/memory_bench_platform/adapters/locomo/runtime.py`
  - `memory_bench_platform/memory_bench_platform/adapters/locomo/timing.py`
- 兼容层：
  - 原 `locomo_test_*` 文件保留为 thin wrapper，仅转发到新 adapter 路径
- 已同步更新引用：
  - `external_report_import.py`
  - `result_analysis.py`
- 目的：
  - 把“平台骨架”和“LoCoMo 外部适配器”在目录结构上切开；
  - 后续新增 benchmark adapter 时，不再继续污染 core 根目录。

---

## 剩余债务清单

### A. 平台核心中的 LoCoMo 特化桥接仍然过重

#### A1. 外部 benchmark 适配桥接仍以 `locomo_test_*` 命名散落在 core 根目录

- 文件：
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/locomo_test_artifacts.py`
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/locomo_test_diagnostics.py`
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/locomo_test_report_bridge.py`
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/locomo_test_metrics_bridge.py`
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/locomo_test_runtime_bridge.py`
- 问题：
  - 这些文件实际上是“LoCoMo 外部适配器”，但现在挂在平台 core 顶层；
  - 导致平台代码目录看起来像“平台 + locomo 实现混编”。
- 当前状态：
  - 已完成第一阶段收敛：
    - 实现已迁入 `adapters/locomo/`
    - 根目录旧文件仅保留兼容 wrapper
  - 剩余项：
    - 后续可继续把测试也迁入 `tests/adapters/locomo/`
    - 兼容 wrapper 可在下一个稳定周期后评估是否删除
- 可执行重构项：
  1. 新建 `memory_bench_platform/memory_bench_platform/adapters/locomo/`
  2. 把上述模块迁入 `adapters/locomo/`
  3. core 侧仅保留兼容导出或 thin wrapper
  4. `result_analysis.py` / `external_report_import.py` 改为从 adapter namespace 引用
- 预期收益：
  - 平台骨架目录只保留通用能力；
  - benchmark 特化逻辑统一沉到 adapter namespace。

#### A2. `external_report_import.py` 仍然写死 LoCoMo 导入流程

- 文件：
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/external_report_import.py`
- 问题：
  - 目前本质上是 `import_locomo_test_result()`，但接口名看起来像通用导入器；
  - 后续接 LongMemEval 时会继续堆 `if benchmark == ...`。
- 可执行重构项：
  1. 定义 `ExternalBenchmarkImporter` 协议
  2. 将 LoCoMo 导入实现下沉为 `adapters/locomo/importer.py`
  3. `external_report_import.py` 只负责按 benchmark skill/entrypoint 分发
- 预期收益：
  - 新 benchmark 接入不再侵入平台总导入器。

#### A3. `result_analysis.py` 仍然同时承担通用报告聚合与 benchmark 特化解释

- 文件：
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/result_analysis.py`
- 问题：
  - 里面同时有通用 run summary、LoCoMo chain diagnosis、official_small 诊断；
  - 一个文件承载过多来源。
- 可执行重构项：
  1. 保留 `result_analysis.py` 只做通用聚合
  2. 新增 `analysis_sources/locomo.py`
  3. 新增 `analysis_sources/official_small.py`
  4. 按 `external_result.source` 或 benchmark id 分派分析器
- 预期收益：
  - 报告主入口稳定，外部 benchmark 分析可插拔。

### B. `locomo_test` 内部职责仍过厚

#### B1. `eval.py` 仍然过大，混了多层职责

- 文件：
  - `/mnt/d/code/Agent/test/locomo_test/locomo_test/eval.py`
- 问题：
  - 同时包含：
    - LoCoMo 数据解析
    - OpenClaw session / state_dir 扫描
    - OpenViking recall / consistency / token 查询
    - ingest/QA 执行主流程
    - direct recall 格式化与 rerank
    - 输出诊断落盘
  - 后续每修一处都容易影响其它链路。
- 当前状态：
  - 已完成第二阶段的四步：
    - `session_store.py` 已抽出
    - `recall_rendering.py` 已抽出
    - `openviking_backend.py` 已抽出 OpenViking HTTP / task / commit / recall / consistency / token usage / reindex 后端逻辑
    - `memory_backend_adapter.py` 已建立 `OpenVikingMemoryBackend` 适配接口，并让 `eval.py` 主流程通过 adapter 调用
  - `eval.py` 现在主要还剩：
    - benchmark 主流程编排
    - 局部诊断与结果合并
- 可执行重构项：
  1. 继续缩小 `eval.py`
     - 仅保留 benchmark 主流程编排
  2. 后续迁移历史测试 patch 点
     - 从 `locomo_test.eval.query_ov_*` 逐步迁移到 `memory_backend_adapter`
  3. 在稳定周期后评估删除 `eval.py` 中的 OpenViking 兼容 wrapper
- 预期收益：
  - benchmark 层和 memory backend 层边界清晰；
  - 后续复用到 LongMemEval 时可直接复用 memory backend 模块。

#### B2. `locomo_test` 对 OpenViking 的具体回路仍以函数直连方式硬耦合

- 文件：
  - `/mnt/d/code/Agent/test/locomo_test/locomo_test/eval.py`
  - `memory_bench_platform/skills/benchmarks/locomo/tooling/test_entrypoints/run_locomo_test_remote.sh`
- 问题：
  - 当前已建立 adapter 接口，`eval.py` 主流程不再直接调用 OpenViking task/commit/recall 查询函数；
  - 但历史测试和兼容入口仍 patch `eval.py` 中的 OpenViking wrapper。
- 当前状态：
  - 已新增 `locomo_test/locomo_test/memory_backend_adapter.py`
  - 已提供接口：
    - `accept_ingest_session`
    - `wait_ingest_completion`
    - `recall_for_question`
    - `read_task_usage`
    - `check_consistency`
  - `eval.py` 的 ingest accepted、final drain、QA direct recall、QA usage/consistency 已改为依赖 adapter
- 可执行重构项：
  1. 把 OpenViking 兼容 wrapper 从 `eval.py` 迁到专门兼容模块
  2. 将测试中的 monkeypatch 点迁移到 adapter 层
  3. 为后续非 OpenViking memory backend 定义同一 adapter 协议
- 预期收益：
  - LoCoMo benchmark 流程不再绑定 OpenViking 细节；
  - future memory backend 更容易替换。

### C. Skill 契约已成型，但真正驱动执行的仍主要是脚本约定

#### C1. `integration.py` 已能解析三 skill，但 CLI / workflow 还没有完全消费这份契约

- 文件：
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/cli.py`
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/workflow.py`
  - `/mnt/d/code/Agent/test/memory_bench_platform/memory_bench_platform/planner.py`
- 问题：
  - 现在 manifest 已能表达：
    - benchmark ingest unit
    - agent protocol mode
    - memory completion signal
  - 但实际执行链路仍主要靠外部脚本内部自解释。
- 可执行重构项：
  1. `planner.py` 读取 `build_run_contract()`
  2. 在 run 记录中落盘 `records/run_contract.json`
  3. CLI 的 `validate` / `run` 增加三 skill 兼容性校验输出
- 预期收益：
  - skill manifest 从“声明文件”提升到“真实执行前置契约”。

### D. 测试组织还没完全跟上架构层次

#### D1. 现有测试名仍大量沿用 `test_locomo_test_*`

- 文件：
  - `/mnt/d/code/Agent/test/memory_bench_platform/tests/test_locomo_test_*.py`
- 问题：
  - 平台层测试和 LoCoMo 适配层测试混在一起，不利于后续扩 benchmark。
- 可执行重构项：
  1. 新增 `tests/adapters/locomo/`
  2. 迁移桥接层测试到 adapter 子目录
  3. core 测试只保留 loader / integration / workflow / report skeleton
- 预期收益：
  - 测试结构与运行结构一致。

---

## 推荐执行顺序

1. 完成 `integration.py` 契约接线到 CLI / planner / workflow
2. 建立 `adapters/locomo/` 命名空间，迁移平台内 LoCoMo 特化桥接
3. 抽薄 `locomo_test/eval.py`，先拆 `session_store` 与 `openviking_backend`
4. 再尝试新增“基于 openclaw 导入的 locomo 新测试入口”，检验骨架是否真正适配

---

## 判断标准

当以下条件满足时，可认为这一轮架构债务基本收敛：

1. `memory_bench_platform` core 根目录不再散落 `locomo_test_*` 特化桥接实现
2. `run` 前可由平台直接输出三 skill 契约与兼容性检查结果
3. `locomo_test/eval.py` 明显缩薄，只保留 benchmark 主流程
4. 新增一个 LoCoMo 入口时，不需要再修改平台 core 的特判逻辑
