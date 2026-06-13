# 记忆评测平台 WSL 最小对接验证记录

## 1. 验证目标

基于当前 `memory_bench_platform` 实现，在 WSL 环境中独立验证以下最小对接链路：

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

### 3.2 基于外部数据集的最小 run

执行：

```bash
python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent generic-cli \
  --data-path /tmp/mbp-ext/locomo/data/locomo10.json
```

结果：

- 成功创建 run 目录
- 成功写出 `records/tasks.json`
- 成功写出 `artifacts/agent-output.json`
- 成功写出 `reports/summary.json`

结论：

- 平台已能消费外部 LoCoMo repo 中的正式数据文件，而不依赖工作区内的本地副本。

## 4. OpenViking 对接结果

### 4.1 源码结构校验

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

### 4.2 WSL 本地安装与 doctor

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

### 4.3 WSL 本地起服务与健康检查

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

## 5. OpenClaw / OpenViking 对接结果

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
- 平台命令

```bash
PYTHONPATH=/tmp/memory_bench_platform \
OPENCLAW_BIN=/usr/local/bin/openclaw \
OPENCLAW_AGENT_ID=locomo-eval \
python3 -m memory_bench_platform.cli run \
  --benchmark locomo \
  --agent openclaw \
  --data-path /tmp/mbp-ext/locomo/data/locomo10.json
```

成功创建：

- `run.json`
- `records/tasks.json`
- `artifacts/agent-output.json`
- `reports/summary.json`

并且 `agent-output.json` 中记录了真实 `openclaw agent --json` 调用结果。

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

## 6. 当前判断

### 已验证通过

- 平台核心协议、loader、planner、archive 骨架
- 外部 LoCoMo 数据集接入
- `generic-cli` agent 最小执行链路
- OpenViking 源码校验
- OpenViking WSL 本地安装、doctor、health
- 远端容器中的 `OpenClaw + OpenViking plugin + LoCoMo` 最小执行链路
- `openclaw` runner 已从 stub 升级为真实 CLI 调用链

### 已验证失败 / 仍有限制

- 本地旧版 WSL `OpenClaw` 环境不满足最低版本要求
- 当前平台 `run` 流程仍只执行首个 task，尚未进入完整多 task / scorer 编排

限制原因：

- 当前 WSL 中 `OpenClaw` 版本低于官方插件要求
- `ExecutionSpec / JudgeInput / JudgeResult` 还未在主执行链里闭环

## 7. 下一步建议

1. 把 `ExecutionSpec` 接入 orchestrator，而不是只保存在协议模型中
2. 让 benchmark skill 显式产出 `RenderedTaskInput`
3. 增加 `JudgeInput -> scorer -> JudgeResult` 最小闭环
4. 将资源监控输出挂入 `records/` 或 `artifacts/` 的统一归档结构
