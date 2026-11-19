# 从零跑 LoCoMo + OpenClaw + oGMemory

目标是在一台 Linux 测试机上，把一段历史交给 OpenClaw，由 oGMemory 官方插件通过 compact 提取记忆，再用新会话答题并生成准确率和 Token 报告。先跑一题看链路，再跑四段历史的 LoCoMo-small。所有命令按顺序在**同一个 Bash 终端**执行。

## 1. 准备环境与源码

需要 Git、curl、Python 3.11（含 venv）、Docker Engine 和 Docker Compose v2。以下部署用 Linux host networking。核对机器架构和镜像支持的平台，尤其是官方 openGauss 镜像；已验证小样本的机器为 ARM64。准备宿主和容器都能访问的 OpenAI 兼容聊天和 embedding 服务，以及各自的模型名和密钥。18789、18090、18091 端口要空闲。

```bash
set -euo pipefail
umask 077
export LAB="$HOME/locomo-og-lab"
export OG="$LAB/ogmemory"
export WL="$LAB/workload/memory_bench_platform"
mkdir -p "$LAB/data"
docker info >/dev/null
docker compose version
uname -m
cd "$LAB"
```

首次安装执行一次。下面的 Workload 分支已包含图片描述输入修复；记录实际克隆的提交，方便以后复现。

```bash
git clone -b feat/plugin-sample-concurrency https://gitcode.com/weixin_44204324/openGauss-MemoryWorkload.git "$LAB/workload"
git -C "$LAB/workload" rev-parse HEAD | tee "$LAB/workload-commit.txt"
git clone -b dev https://gitcode.com/opengauss/oGMemory.git "$OG"
git -C "$OG" checkout --detach 1e88d97b7aa6d940e06e69e9205bb48d1bd57600
python3.11 -m venv "$LAB/.venv"
source "$LAB/.venv/bin/activate"
python -m pip install -e "$WL" -e "$LAB/workload/locomo_test" python-dotenv pyyaml
```

## 2. 启动记忆服务

在专用克隆中编辑官方 Compose 的配置。下面使用官方示例的数据库、AGFS 和后端服务，默认示例后端镜像标签为 `20261114`。源代码 checkout 固定了插件和部署脚本，后端二进制版本由 `OGMEM_IMAGE` 决定。要与某个 dev 构建逐版本对比，请在这里填该构建的镜像并记录 digest。

```bash
cd "$OG"
cp .env.example .env
chmod 600 .env
nano .env
```

在已有条目上改值，保存后退出。`LLM_BASE_URL` 和 `EMBEDDING_BASE_URL` 填提供方给出的 API 根地址（通常以 `/v1` 结尾）。只在自己的 .env 填真实密钥。

| .env 字段 | 本例的值 |
|---|---|
| COMPOSE_PROJECT_NAME、DATA_ROOT | 分别为 `locomo-og-lab`、`./data` |
| LLM_PROVIDER、LLM_MODEL | `openai`、`gpt-6-luna`（需要服务商实际提供） |
| LLM_BASE_URL、LLM_API_KEY | 聊天服务地址、密钥 |
| EMBEDDING_PROVIDER、EMBEDDING_MODEL | `openai`、`BAAI/bge-m3`（以服务提供的名称为准） |
| EMBEDDING_BASE_URL、EMBEDDING_API_KEY、EMBEDDING_DIMENSION | 向量服务地址、密钥、`1024` |
| OGMEM_AFTER_TURN_THRESHOLD | `1000000` |
| OGMEM_HTTP_HOST_PORT、OGMEM_MCP_HOST_PORT | `18090`、`18091` |

本例把记忆抽取设为 eager、关闭 dual pass，提取输出上限 16384。以下命令修改专用克隆的**部署配置**，随后由官方启动脚本渲染。

```bash
python - <<'PY'
from pathlib import Path
import yaml
p = Path("deploy/compose/ogmemory.yaml")
v = yaml.safe_load(p.read_text())
for section in ("memory", "extraction"):
    v.setdefault(section, {}).update(mode="eager", dual_pass_enabled=False)
v["llm"]["max_tokens"] = 16384
p.write_text(yaml.safe_dump(v, sort_keys=False, allow_unicode=True))
PY
bash deploy/compose/deploy.sh start
bash deploy/compose/deploy.sh status
export OGMEM_API_URL=http://127.0.0.1:18090
curl -fsS "$OGMEM_API_URL/api/v1/health"
```

让 Agent 和 Judge 复用刚配置的聊天模型。密钥只进入当前进程环境和专用配置文件，请勿开启 `set -x`。本例后端是本次测试独占的实例，读取其 Token 计数器前后差值才有意义。

```bash
cfgval() { python -c 'from dotenv import dotenv_values; import sys; v=dotenv_values(sys.argv[1])[sys.argv[2]]; assert v; print(v)' "$1" "$2"; }
export LOCOMO_BASE_URL="$(cfgval "$OG/.env" LLM_BASE_URL)"
export LOCOMO_API_KEY="$(cfgval "$OG/.env" LLM_API_KEY)"
export LOCOMO_METRIC_MODEL="$(cfgval "$OG/.env" LLM_MODEL)"
export MEMORY_BENCH_ANSWER_MODEL="$LOCOMO_METRIC_MODEL"
export OGMEM_API_KEY="$(cfgval "$OG/data/locomo-og-lab/secrets.env" OG_ROOT_API_KEY)"
: "${OGMEM_API_KEY:?未读取到专用服务密钥}" "${LOCOMO_API_KEY:?未读取到模型密钥}"
export MEMORY_BENCH_TOKEN_USAGE=1 MEMORY_BENCH_MEMORY_TOKEN_SOURCE=native
export MEMORY_BENCH_OGMEMORY_EXCLUSIVE=1
export MEMORY_BENCH_OGMEMORY_CONTAINER="$(cd "$OG" && docker compose ps -q ogmemory)"
: "${MEMORY_BENCH_OGMEMORY_CONTAINER:?请先检查 compose status}"
```

## 3. 启动 OpenClaw 和 oGMemory 插件

新机器可用 npm 发布包构建 Gateway 镜像，也可把 `CLAW_IMAGE` 改成已有的相同版本镜像。实际小样本使用 OpenClaw 2026.7.1-2。下面将 Gateway 配置、会话和读取回执的状态文件都放在 `$LAB/gateway`，首次准备时该目录必须尚未创建。

```bash
export CLAW_IMAGE=locomo-openclaw:2026.7.1-2
docker build -t "$CLAW_IMAGE" - <<'DOCKER'
FROM node:24-bookworm
RUN npm install -g openclaw@2026.7.1-2
ENTRYPOINT ["openclaw"]
DOCKER
docker run --rm "$CLAW_IMAGE" --version
export GW="$LAB/gateway"
export OPENCLAW_AGENT_ID=main OPENCLAW_TRANSPORT=http
export OGMEM_GATEWAY_CONTAINER=locomo-og-claw
export OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
export OPENCLAW_STATE_DIR="$GW/state" OPENCLAW_CONFIG_PATH="$GW/openclaw.json"
export OGMEM_PLUGIN_STATE_FILE="$GW/phase.json"
unset OG_AUTH_ACCOUNT_ID
cd "$WL"
python - <<'PY'
import json, os
from pathlib import Path
e = os.environ
model = e["LOCOMO_METRIC_MODEL"]
provider = dict(baseUrl=e["LOCOMO_BASE_URL"], apiKey=e["LOCOMO_API_KEY"],
                api="openai-completions", models=[dict(id=model, name=model)])
seed = {"models": {"providers": {"bench": provider}},
        "agents": {"defaults": {"model": {"primary": "bench/"+model}}}}
(Path(e["LAB"])/"openclaw-seed.json").write_text(json.dumps(seed))
PY
python skills/memory_plugins/openclaw-ogmemory/scripts/run_lifecycle.py \
  --source-config "$LAB/openclaw-seed.json" --upstream-plugin "$OG/plugins/openclaw" \
  --destination "$GW" --api-url "$OGMEM_API_URL" --openclaw-runtime-dir /runtime --gateway-port 18789
mkdir -p "$GW/state" "$GW/workspace"
export OPENCLAW_GATEWAY_TOKEN="$(python -c 'import os,json; print(json.load(open(os.environ["OPENCLAW_CONFIG_PATH"]))["gateway"]["auth"]["token"])')"
docker run -d --name "$OGMEM_GATEWAY_CONTAINER" --network host \
  --label "memory-bench.runtime=$GW" --user "$(id -u):$(id -g)" \
  -e HOME=/runtime -e OPENCLAW_STATE_DIR=/runtime/state -e OPENCLAW_CONFIG_PATH=/runtime/openclaw.json \
  -v "$GW:/runtime" --entrypoint openclaw "$CLAW_IMAGE" gateway run
export OPENCLAW_BIN="$GW/openclaw-host"
cat > "$OPENCLAW_BIN" <<'SH'
#!/bin/sh
exec docker exec "$OGMEM_GATEWAY_CONTAINER" openclaw "$@"
SH
chmod +x "$OPENCLAW_BIN"
curl -fsS --retry 30 --retry-connrefused --retry-delay 2 "$OPENCLAW_GATEWAY_URL/readyz"
"$OPENCLAW_BIN" plugins list
```

在列表里确认 `og-memory-context-engine` 已加载。检查本机 CLI 能否调用 compact：下面故意不传 session key，预期为缺少 key 的 `INVALID_REQUEST`。它不会提交任何历史。

```bash
"$OPENCLAW_BIN" gateway call sessions.compact --params '{}' --json || true
```

若返回 pairing required 或权限不足，运行 `"$OPENCLAW_BIN" devices list --json`，核对请求的 `deviceId` 与 `$GW/state/identity/device.json` 中的 `deviceId` 一致且客户端是本机 CLI，再执行 `"$OPENCLAW_BIN" devices approve <该请求的requestId>`。重做上面的空参数检查后再开始测试。

## 4. 准备可复现的小数据

从 LoCoMo 作者仓库下载原始数据。本例生成两个输入。`smoke.json` 用 conv-26 的第 8 段和一道图片相关题验证真实链路。`small.json` 用该对话的前四段和证据全部落在其中的 QA，与本次工作使用的小集口径一致。Workload 默认评分第 1 至 4 类。

```bash
cd "$LAB/data"
curl -fL https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json -o locomo10.json
sha256sum locomo10.json
python - <<'PY'
import json
from pathlib import Path
rows = json.loads(Path("locomo10.json").read_text())
row = next(r for r in rows if r["sample_id"] == "conv-26")
conv = row["conversation"]
def write(name, ids, questions):
    c = {k: conv[k] for k in ("speaker_a", "speaker_b")}
    for i in ids:
        for k in (f"session_{i}", f"session_{i}_date_time"):
            c[k] = conv[k]
    Path(name).write_text(json.dumps([{"sample_id": row["sample_id"], "conversation": c,
                                      "qa": questions}], ensure_ascii=False))
q = [q for q in row["qa"] if q["question"].strip().lower() ==
     "what kind of pot did mel and her kids make with clay?"]
assert len(q) == 1
write("smoke.json", [8], q)
ids = range(1, 5)
turns = {t["dia_id"] for i in ids for t in conv[f"session_{i}"]}
qs = [q for q in row["qa"] if q.get("evidence") and set(q["evidence"]) <= turns]
assert sum(int(q["category"]) in (1,2,3,4) for q in qs) == 35
write("small.json", ids, qs)
PY
```

## 5. 跑测试，打开报告

先执行下面整段。`run_locomo smoke` 会写历史、等待提取、发起 QA，再交给 Judge；每次调用函数都会生成独立的 run ID。确认冒烟测试完成并有评分，再运行 small。新手先用串行，便于逐步排查。

```bash
cd "$WL"
run_locomo() {
  local dataset="$1"
  export RUN_ID="ogmemory-$dataset-$(date -u +%Y%m%dT%H%M%SZ)"
  python -m memory_bench_platform.cli run \
    --benchmark locomo --agent openclaw --memory-backend ogmemory \
    --memory-integration agent_plugin --data-path "$LAB/data/$dataset.json" \
    --run-id "$RUN_ID" --sample-concurrency 1 --qa-concurrency 1 --judge-concurrency 1
  export RUN_DIR="$WL/runs/$RUN_ID"
  python -m memory_bench_platform.cli analyze-run --run-dir "$RUN_DIR"
  echo "报告目录：$RUN_DIR/reports"
  cat "$RUN_DIR/reports/analysis.md"
  cat "$RUN_DIR/reports/token_summary.md"
}
run_locomo smoke
```

确认 smoke 有 1 道评分后，在同一终端执行：

```bash
run_locomo small
```

| 想看什么 | 打开哪个文件 |
|---|---|
| 正式准确率、有效性、评分覆盖率 | `reports/summary.json` 和 `reports/analysis.md`；先核对 small 的应评 35 题、实际评分数和降级记录 |
| 每道题的答案、判定 | `reports/case_results.json` |
| 记忆侧 LLM、Agent 接收历史、Agent 回答的 Token | `reports/token_summary.md` 和 `reports/token_summary.json`。Cache Read 按报告的输入口径看，勿再次加到总数；未观测到的用量保留未知 |
| 浏览器可读报告 | `reports/run_report.html`；将该文件复制到自己的电脑打开 |

如果进程显示完成但评分数不足，先查看该 run 的步骤产物以及 `docker logs "$OGMEM_GATEWAY_CONTAINER"`。历史写入、原生 compact、就绪回执、QA、Judge 分别检查；跳过和未评分应按报告原样保留。调试时保留原 run 目录。

## 6. 停止服务

```bash
docker stop "$OGMEM_GATEWAY_CONTAINER"
cd "$OG"
bash deploy/compose/deploy.sh stop
```

以上命令保留历史数据、会话和报告。重看结果只需打开已有报告，无需再次调用模型。

**验证范围**：本次实际验证为一段历史和一道 QA，原始图片描述进入 ingest 请求，最终答案提到狗脸杯，Judge 判对。真实实验使用独立的 dev 后端和模型代理观察用量；教程提供的是可迁移的官方 Compose 部署和独占原生计数器配置。没有把新机器部署流程或整个 35 题评测计入此次实测结果。
