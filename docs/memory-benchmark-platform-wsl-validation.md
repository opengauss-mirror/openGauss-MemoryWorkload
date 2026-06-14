# 记忆测试平台 workflow/case 验证记录

## 1. 验证目标

基于当前 `memory_bench_platform` 实现，验证 workflow/case 主链在本地和远端环境下是否已形成最小闭环：

- `snap-research/locomo`
- `OpenClaw` CLI / OpenViking plugin 接入口
- `volcengine/OpenViking`

## 2. 验证环境

- 平台代码路径：`/mnt/d/code/Agent/test/memory_bench_platform`
- LoCoMo 外部仓库路径：`/tmp/mbp-ext/locomo`
- OpenViking 外部仓库路径：`/tmp/mbp-ext/OpenViking`
- OpenViking 虚拟环境：`/tmp/mbp-ov-venv`
- OpenViking HOME：`/tmp/mbp-ov-home`

## 3. LoCoMo 对接结果

### 3.1 外部数据集校验

执行：

```bash
python3 -m memory_bench_platform.cli validate \
  --benchmark locomo \
  --data-path /tmp/mbp-ext/locomo/data/locomo10.json
```

结果：

- `status = ok`
- `sample_count = 10`
- `has_qa = true`
- 首个 `sample_id = conv-26`

### 3.2 基于 LoCoMo CaseSource 的本地最小闭环

执行：

```bash
python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent generic-cli
```

结果：

- 成功创建 run 目录
- 成功写出 `records/cases.json`
- 成功写出 `records/steps.json`
- 成功写出 `records/step_results.json`
- 成功写出 `records/traces.json`
- 成功写出 `records/judge_results.json`
- 成功写出 `records/metrics.json`
- 成功写出 `reports/summary.json`
- 成功写出 `reports/case_results.json`
- 成功写出 `artifacts/monitor/cpu_status.csv`

结论：

- 平台已经不再只是 `task -> answer -> summary` 最小骨架，而是可以完成：
  `LoCoMo CaseSource -> step 执行 -> answer 提取 -> builtin judge -> metrics/report/archive`

### 3.3 LongMemEval CaseSource 对接状态

当前状态：

- 已按官方字段格式实现 `LongMemEval` 的 `CaseSource`
- 已支持解析字段：
  - `question_id`
  - `question_type`
  - `question`
  - `answer`
  - `question_date`
  - `haystack_session_ids`
  - `haystack_dates`
  - `haystack_sessions`
  - `answer_session_ids`

当前验证：

```bash
python3 -m memory_bench_platform.cli validate --benchmark longmemeval
```

结果：

- 在未提供数据文件时，返回 `status = missing_source`
- 平台可以明确识别“技能已实现，但当前缺少官方数据文件”

结论：

- `LongMemEval` 已不再是空壳 skill
- 但还需要引入真实官方数据文件后，才能完成最终外部验证

## 4. Native workflow case 对接结果

执行：

```bash
python3 -m memory_bench_platform.cli validate --benchmark ovtest-memory
python3 -m memory_bench_platform.cli run --benchmark ovtest-memory --agent generic-cli
```

结果：

- `validate` 返回 `source_kind = native_workflow`
- `run.json` 中 `source_kind = native_workflow`
- `step_results.json` 中包含 `bash` 与 `wait` 两类 step
- `traces.json` 中包含 `step_started / step_finished / gate_passed / case_judge_*`
- `case_results.json` 返回 `passed = true`

结论：

- 平台已经不再只支持问答型 benchmark case
- 原生 workflow/native case 已能进入统一执行内核

### 4.1 真实 OpenViking `/health` workflow

在远端容器 `123.60.114.206:10008 / jcp-dev` 中执行：

```bash
OVTEST_HEALTH_URL=http://127.0.0.1:1933/health \
python3 -m memory_bench_platform.cli run \
  --benchmark ovtest-health \
  --agent generic-cli
```

结果：

- `run.json.source_kind = native_workflow`
- `step_results.json` 中记录了真实 HTTP 200 返回
- `case_results.json` 返回 `passed = true`
- `summary.json` 中包含 run 级 CPU / memory 摘要

结论：

- 原生 workflow case 不只是本地 mock
- `http operator` 已在真实 OpenViking 服务上完成外部验证

### 4.2 真实 OpenViking `ov admin/create-account -> add-memory -> find` workflow

在远端容器中执行：

```bash
OVTEST_SERVER_URL=http://127.0.0.1:1933 \
OVTEST_ROOT_KEY=ov-root-namespace-test-20260517 \
python3 -m memory_bench_platform.cli run \
  --benchmark ovtest-admin-memory \
  --agent generic-cli
```

结果：

- `cleanup / create-account / add-memory / settle / find-memory` 全部作为 step 成功执行
- 成功写出：
  - `records/cases.json`
  - `records/steps.json`
  - `records/step_results.json`
  - `records/traces.json`
  - `records/judge_results.json`
  - `records/metrics.json`
  - `reports/summary.json`
  - `reports/case_results.json`
- `create-account` step 成功拿到真实 `user_key`
- `add-memory` step 成功返回 `{"ok":true,...}`
- `find-memory` step 成功返回真实 `ov find` JSON
- 最终 `case_results.json` 为 `passed = false`

失败原因：

- 该环境下 `add-memory` 返回 `memories_extracted = 0`
- 后续 `find` 未检索到期望的 `Go over Python` 记忆

结论：

- 真实 `ov` CLI 原生 workflow 已经可以进入平台主链并完整归档
- 当前失败反映的是环境/语义结果问题，而不是平台执行闭环缺失

## 5. OpenViking 对接结果

### 5.1 源码结构校验

执行：

```bash
python3 -m memory_bench_platform.cli validate \
  --memory-backend openviking \
  --source-path /tmp/mbp-ext/OpenViking \
  --api-base https://ark.cn-beijing.volces.com/api/coding/v3 \
  --api-key 626b6c9a-f0d4-4a05-b8dd-75664219a2a0 \
  --vlm-model doubao-seed-2.0-pro \
  --embedding-model doubao-embedding-vision
```

结果：

- `README.md` 存在
- `pyproject.toml` 存在
- `docs/` 目录存在
- 平台成功生成建议的 `vlm` / `embedding` 配置模板

### 5.2 WSL 本地安装与 doctor

执行：

```bash
python3 -m venv /tmp/mbp-ov-venv
source /tmp/mbp-ov-venv/bin/activate
python -m pip install openviking
```

之后写入最小 `ov.conf` 并执行：

```bash
HOME=/tmp/mbp-ov-home \
OPENVIKING_CONFIG_FILE=/tmp/mbp-ov-home/.openviking/ov.conf \
/tmp/mbp-ov-venv/bin/openviking-server doctor
```

结果：

- `Config: PASS`
- `Python: PASS`
- `Native Engine: PASS`
- `AGFS: PASS`
- `Embedding: PASS`
- `VLM: PASS`
- `Disk: PASS`
- `All checks passed`

### 5.3 WSL 本地起服务与健康检查

执行：

```bash
HOME=/tmp/mbp-ov-home \
OPENVIKING_CONFIG_FILE=/tmp/mbp-ov-home/.openviking/ov.conf \
OPENVIKING_CLI_CONFIG_FILE=/tmp/mbp-ov-home/.openviking/ovcli.conf \
/tmp/mbp-ov-venv/bin/openviking-server
```

并验证：

```bash
curl http://127.0.0.1:1933/health
ov health
```

结果：

- `/health` 返回 `{"status":"ok","healthy":true,"version":"0.3.24","auth_mode":"dev"}`
- `ov health` 返回 `Connected (Healthy)`

结论：

- OpenViking 已在 WSL 中完成最小本地安装、配置、doctor 校验和健康运行验证。

## 6. OpenClaw / OpenViking 对接结果

### 5.1 CLI 基础可用性

执行：

```bash
openclaw --version
```

结果：

- 当前版本：`OpenClaw 2026.3.12 (6472949)`

补充：

- 之后在 WSL 内已额外安装较新版本 `OpenClaw 2026.6.6`
- 但该本地新版本环境中未直接得到可用的 `openclaw openviking` 子命令，因此本地 WSL 不作为最终最小成功对接证据

### 5.2 平台侧 agent 校验

执行：

```bash
python3 -m memory_bench_platform.cli validate --agent openclaw
```

结果：

- 找到了 `openclaw` 可执行文件
- `version_exit_code = 0`
- `plugin_version_supported = false`
- 最低插件要求版本：`2026.4.8`
- `openclaw health` 超时
- `openclaw openviking status --json` 超时

### 5.3 远端容器最小成功对接

在远端容器 `123.60.114.206:10008 / jcp-dev` 中执行验证：

- `openclaw --version` 返回 `OpenClaw 2026.4.8`
- `curl http://127.0.0.1:1933/health` 返回 OpenViking healthy
- `openclaw health` 正常返回 agent 状态
- `openclaw openviking status --json` 返回 plugin 已加载，`slotActive = true`
- 平台 `validate` 命令

```bash
PYTHONPATH=/tmp/memory_bench_platform \
python3 -m memory_bench_platform.cli validate \
  --benchmark locomo \
  --data-path /tmp/mbp-ext/locomo/data/locomo10.json \
  --agent openclaw \
  --memory-backend openviking \
  --source-path /home/jcp/agent/code/OpenViking \
  --api-base https://ark.cn-beijing.volces.com/api/coding/v3 \
  --api-key ...
```

返回：

- benchmark `status = ok`
- agent `status = ok`
- OpenClaw `plugin_version_supported = true`
- memory backend `status = ok`

- 平台 `run` 命令（最小 case 数据）

```bash
PYTHONPATH=/tmp/memory_bench_platform \
OPENCLAW_BIN=/usr/local/bin/openclaw \
OPENCLAW_AGENT_ID=locomo-eval \
python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent openclaw \
  --data-path /tmp/mbp-ext/locomo/data/locomo10_min.json
```

成功创建：

- `run.json`
- `records/cases.json`
- `records/steps.json`
- `records/step_results.json`
- `records/traces.json`
- `records/judge_results.json`
- `records/metrics.json`
- `artifacts/step-stdout/*.json`
- `artifacts/monitor/cpu_status.csv`
- `reports/case_results.json`
- `reports/summary.json`

并且：

- `step_results.json` 中记录了真实 `openclaw agent --json` 的结构化结果提取
- `judge_results.json` 中记录了 builtin judge 的最终判定
- `metrics.json` 中记录了 step duration、retry_count 和 run 级 CPU 指标

### 5.4 与 OpenViking 插件文档对照

根据 OpenViking 官方 OpenClaw 插件文档：

- Node.js 要求：`>= 22`
- OpenClaw 要求：`>= 2026.4.8`

当前环境：

- Node.js 满足要求
- OpenClaw 版本低于插件要求

结论：

- 当前 WSL 旧环境中的 `OpenClaw` CLI 曾不满足最低版本要求，平台能正确识别该兼容性缺口。
- 最终最小成功对接证据来自远端容器环境，而不是本地旧版 WSL 环境。

## 7. 当前判断

### 已验证通过

- workflow/case 主模型文档与计划
- 外部 LoCoMo 数据集接入
- `LoCoMo -> CaseSource -> generic-cli -> builtin judge -> report` 本地闭环
- `LongMemEval` 官方格式兼容的 CaseSource 与缺数据诊断
- `OpenClaw` agent operator 最小执行链路
- OpenViking 源码校验
- OpenViking WSL 本地安装、doctor、health
- 远端容器中的 `OpenClaw + OpenViking plugin + LoCoMo` 最小 workflow/case 闭环
- 原生 `ovtest-memory` workflow case 的本地闭环
- 原生 `ovtest-health` workflow case 的远端闭环
- 原生 `ovtest-admin-memory` workflow case 的远端完整执行与归档
- CPU / memory 资源摘要已进入 summary 与 metrics

### 已验证失败 / 仍有限制

- 本地旧版 WSL `OpenClaw` 环境不满足最低版本要求
- `LongMemEval` 虽已支持官方格式，但尚未接入真实官方数据文件
- builtin judge 仍是最小规则，不是最终 LLM judge
- `ovtest-admin-memory` 当前语义结果仍失败，尚未拿到“成功检索目标记忆”的通过样本

限制原因：

- 当前 WSL 中 `OpenClaw` 版本低于官方插件要求
- workflow executor 目前是 Python 过渡实现
- 当前真实 `ov admin/add-memory/find` workflow 已跑通闭环，但环境中 memory extraction 结果还不稳定

## 8. 下一步建议

1. 继续扩 workflow executor 的 DAG / 并发能力
2. 接入真实外部 `ovtest/OpenViking` workflow case
3. 为 builtin judge 之外增加外部/LLM judge adapter
4. 引入官方 `LongMemEval` 数据文件并完成外部验证
