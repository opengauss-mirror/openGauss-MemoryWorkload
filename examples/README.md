# 一条命令部署并运行记忆评测

这些示例随仓库源码提供，不需要另外下载模板或编写生成脚本。它们复用
`deployment/1`、`experiment`、原生 `run` 和已有 memory-plugin 生命周期。
没有增加第二套实验格式，也不修改官方插件。

## 获取和安装

在包含这些示例的源码分支中执行。Python 要求 3.11+，Docker 要求 Compose v2。
目前本功能位于 `feat/agent-plugin-deployment-20261119`，尚未宣称已合入主分支。

```bash
git clone --branch feat/agent-plugin-deployment-20261119 \
  https://gitcode.com/weixin_44204324/openGauss-MemoryWorkload.git
cd openGauss-MemoryWorkload
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
docker compose version
```

## 可选：配置评分并发

在所选示例的 `deployment.yaml` 中找到 `combinations` 条目，在 `memory_integration` 同级添加以下配置：

```yaml
    execution:
      judge_concurrency: 4
```

运行 `memory-bench experiment --config deployment.yaml` 即可。OpenClaw 插件示例逐题执行 QA，Judge 最多同时评分四题。直接连接记忆后端的组合还可以配置 QA 并发。[完整配置说明](../docs/deployment-yaml.md#配置-qa-和-judge-并发)

## 选择组合

六套组合使用同一官方 Gateway 镜像 `ghcr.io/openclaw/openclaw:2026.6.9`。各记忆系统的后端镜像和插件版本见对应示例。

| 示例目录 | 部署内容 | 额外输入/限制 |
|---|---|---|
| `ogmemory-openclaw` | OpenClaw、AGFS 版 oGMemory、独立 openGauss | 官方插件目录；数据库镜像为 ARM64 的 `opengauss-distributed:0328` 时，需要 ARM64 主机；该镜像按官方 Compose 使用 privileged |
| `mem0-openclaw` | OpenClaw 内的官方 Mem0 OSS 插件、独立 pgvector | 官方 `@mem0/openclaw-mem0@1.2.0`，保留安装后的 node_modules 依赖树 |
| `memos-openclaw` | OpenClaw 内的官方 MemOS Local | [一键部署教程](#memos-local-与-openclaw-全量-locomo)，官方插件 2.0.8，一次部署可顺序运行全部样本 |
| `openviking-openclaw` | OpenClaw、原生 OpenViking HTTP 服务 | 官方 context-engine 插件须有 autoCapture/autoRecall/baseUrl；提供与镜像匹配的原生后端配置 |
| `hindsight-openclaw` | OpenClaw、原生 Hindsight HTTP 服务 | [一键部署教程](#hindsight--openclaw部署并运行-locomo)，使用官方镜像、内置 PostgreSQL 和官方插件 1.2.0 |
| `jiuwenmemory-openclaw` | OpenClaw 内置 MCP、原生 JiuwenMemory 服务 | 服务须提供 memory_add/memory_search；提供镜像所需原生配置及后端依赖 |

对于 OpenViking 和 JiuwenMemory 的原生服务模板，`MEMORY_CONFIG_PATH` 指向已有的官方后端配置，
`MEMORY_CONFIG_TARGET`、`MEMORY_DATA_TARGET` 和端口应与该镜像的入口程序一致。
模板不猜测不同版本的数据库、模型和启动参数，也不自动部署该原生配置指向的外部依赖。
默认检查 `/health`；实际镜像不同则修改 YAML 中健康检查路径和启动 command。
挂载配置并不保证镜像自动读取它，必须确认入口程序的默认配置路径。
请按对应镜像版本准备配置和外部依赖。

## oGMemory 完整步骤

### 1. 镜像和插件

本地准备三类镜像：OpenClaw 2026.6.9、oGMemory AGFS Compose 镜像、openGauss。
镜像名称在 `.env` 中填写；启用 `local_images_only`，缺失时启动前报错，不会下载。
先执行 `docker pull ghcr.io/openclaw/openclaw:2026.6.9`。模板通过官方镜像内的 `openclaw gateway run` 启动 Gateway。
oGMemory 镜像须包含 `docker/Dockerfile.compose` 中的 AGFS、Python、psycopg2，
启动后生成 `/tmp/ogmem.resolved.yaml`；此模板不适用于 SQL-only 镜像。

官方插件取自与后端镜像匹配版本的 oGMemory 源码：

```bash
git clone https://atomgit.com/opengauss/oGMemory.git /home/taoying/oGMemory
# 检出与实际镜像构建版本匹配的 commit，不要任意混用 dev 与旧镜像。
git -C /home/taoying/oGMemory checkout <镜像对应的commit>
test -f /home/taoying/oGMemory/plugins/openclaw/openclaw.plugin.json
```

将该目录填写为 `PLUGIN_PATH`；旧版源码的插件目录可能为
`openclaw_context_engine_plugin`，以实际文件为准。平台检查原生开关能力。

### 2. 数据

`DATA_PATH` 是宿主机绝对路径，指向原始 LoCoMo JSON 格式的已准备数据。
可以填写现有 `locomo-small.json`；四个历史 session 不等于四个 sample。
完整官方数据可从以下地址取得（含多个 sample，不是 small）：

```bash
mkdir -p data
curl -fL --retry 3 \
  https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json \
  -o data/locomo10.json
```

平台不自动下载数据或静默截断数据集。MemOS 示例需要单 sample 数据。

### 3. 填配置

```bash
cd examples/ogmemory-openclaw
cp .env.example .env
chmod 600 .env
vi .env
```

必须修改本地镜像名称、插件目录、数据路径、LLM、Embedding、Judge 和密码。
`OGMEM_API_KEY` 是本次部署自己设置的随机服务密钥，不是模型密钥。
数据库容器使用独立卷和镜像自带的 postgres 数据库，不连接现有业务数据库。

`.env` 支持 `export NAME=value` 和引号，不执行 Shell，不做 `.env` 内变量展开；
三个模型使用相同密钥时直接填写相同值。已导出的 Shell 环境变量优先。
若切换测试配置，请先取消不希望继承的 Shell 变量。
LLM、Embedding、Judge 是不同角色；聊天接口不保证提供 Embedding。
Embedding 模型输出维度必须与 EMBEDDING_DIMENSION 相符。

模板中的 oGMemory 提取使用 `after_turn_threshold=1000000` 和原生 session compact。
QA 关闭自动捕获、开启召回，每题新建会话。这不是阈值 200 自动抽取场景。

### 4. 一键部署和测试

```bash
memory-bench experiment --config deployment.yaml
```

无需 source `.env`，也无需传 `--profiling-config`。命令自动执行部署、健康检查、
写入/提交/就绪/QA、评分、离线分析，并打印实验报告目录。
模型调用会产生费用。服务就绪只证明部署成功，不证明模型正确回答或抽取。

示例的 `experiment.fresh: true` 会拒绝复用已有同名部署；它不会擅自清空已有部署。
`cleanup_owned_deployment: true` 会在成功或失败后清理本命令创建的部署。
已有部署永远不会被实验命令自动删除。希望失败后排查容器时将该字段设为 false。
中断进程通常能进入清理；SIGKILL/主机掉电无法保证，需手动检查和清理。

### 5. 报告、状态和清理

在相同工作目录操作，状态和报告位于当前目录下：

* `runs/experiments/<实验ID>/experiment.json`：每一步执行结果及 suite 路径。
* 同目录的容器日志：最多保留每个容器最近 10000 行，清理前归档，权限 0600。
* `runs/<run-id>/reports/`：准确率和逐题分析，以实际报告为准。
* `runs/<run-id>/records/`、`logs/`：调用记录及错误。

日志和模型内容可能含隐私；不要直接公开整个运行目录。
报告中的执行成功与答案正确是不同指标。系统/接口失败不能解释为准确率为零。
实验会核对原生 `reports/summary.json`：有效但有错题的 `partial` 可正常完成，
缺少报告、failed/invalid 或 `run_validity.valid=false` 则返回非零退出码。

若关闭自动清理，或需要分步调试：

```bash
memory-bench deploy --config deployment.yaml
memory-bench deploy status --deployment ogmemory-openclaw-locomo
memory-bench run --deployment ogmemory-openclaw-locomo --combination ogmemory-openclaw
memory-bench undeploy --deployment ogmemory-openclaw-locomo
```

`undeploy` 删除本部署卷中的测试数据，保留 reports/logs；不删除镜像、数据集或官方插件。
修改 `.env` 或配置模板后需要重建部署，不能继续把旧进程当作新配置运行。
Docker 默认地址池耗尽时，使用已有 `network_subnets` 配置分配管理员确认可用的网段；
不要 prune 其他任务网络。

## OpenViking + OpenClaw：部署并运行全量 LoCoMo

以下步骤在 Linux 的同一个 Bash 终端执行。先完成上面的“获取和安装”，保持虚拟环境激活，从仓库根目录开始。需要 Docker Compose v2、npm、可从宿主机和容器访问的 OpenAI 兼容聊天及向量接口。示例为单租户测试启动独立服务，使用 OpenViking 自带的本地向量存储。

### 1. 准备镜像、插件和数据

```bash
set -eu
REPO="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ov-locomo.XXXXXX")"
echo "本次结果目录：$WORK"
cp -r "$REPO/examples/openviking-openclaw/." "$WORK/"
cd "$WORK"
export MEMORY_IMAGE=openviking-cn-beijing.cr.volces.com/volcengine/openviking:v0.5.4
docker pull "$MEMORY_IMAGE"

# 安装到独立目录，插件的依赖须位于随后挂入 Gateway 的插件目录内。
npm install --prefix "$PWD/vendor" --install-strategy=nested --omit=dev --no-audit --no-fund \
  @openviking/openclaw-plugin@2026.5.8
export PLUGIN_PATH="$PWD/vendor/node_modules/@openviking/openclaw-plugin"
test -f "$PLUGIN_PATH/openclaw.plugin.json"

git clone --quiet --depth 1 --branch master https://gitcode.com/weixin_44204324/locomo-test.git data-source
export DATA_PATH="$PWD/data-source/data/locomo10.json"
echo "5da1af63b66d5bba1c00e40eecff7a825ba202927f5c1e563670f89d0dfbfad4  $DATA_PATH" | sha256sum -c -
```

OpenViking 镜像使用官方中国区仓库的版本标签。也可将 `MEMORY_IMAGE` 设为 `ghcr.io/volcengine/openviking:v0.5.4` 再执行 pull。[官方镜像](https://github.com/volcengine/OpenViking/pkgs/container/openviking) · [v0.5.4 官方部署配置](https://github.com/volcengine/OpenViking/blob/v0.5.4/docker-compose.yml)。

拉取与其余组合相同的官方 OpenClaw 镜像。

```bash
export OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.6.9
docker pull "$OPENCLAW_IMAGE"
docker run --rm --network none "$OPENCLAW_IMAGE" openclaw --version
```

### 2. 配置模型和测试租户

聊天接口供记忆提取、Agent 回答和 Judge 共用。向量接口单独配置。请填写自己服务实际支持的模型与维度；容器内的 `127.0.0.1` 指向容器自身。

```bash
read -rp '聊天 API Base URL（含 /v1）：' LLM_BASE_URL
read -rp '聊天模型名（例如 gpt-5.6-luna）：' LLM_MODEL
read -rsp '聊天 API Key：' LLM_API_KEY; echo
read -rp 'Embedding API Base URL（含 /v1）：' EMBEDDING_BASE_URL
read -rp 'Embedding 模型名（例如 BAAI/bge-m3）：' EMBEDDING_MODEL
read -rp 'Embedding 输出维度（例如 1024）：' EMBEDDING_DIMENSION
read -rsp 'Embedding API Key：' EMBEDDING_API_KEY; echo
export LLM_BASE_URL LLM_MODEL LLM_API_KEY EMBEDDING_BASE_URL EMBEDDING_MODEL EMBEDDING_DIMENSION EMBEDDING_API_KEY
umask 077
export PLUGIN_PATH="$(realpath -e "$PLUGIN_PATH")"
export DATA_PATH="$(realpath -e "$DATA_PATH")"
export MEMORY_CONFIG_PATH="$PWD/ov.conf"
export MEMORY_CONFIG_TARGET=/app/.openviking/ov.conf MEMORY_DATA_TARGET=/data/openviking MEMORY_PORT=1933
export MEMORY_API_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export LOCOMO_BASE_URL="$LLM_BASE_URL" LOCOMO_METRIC_MODEL="$LLM_MODEL" LOCOMO_API_KEY="$LLM_API_KEY"
python - <<'PY'
import json, os
from pathlib import Path
e = os.environ
config = {
    "server": {"host": "0.0.0.0", "port": 1933, "auth_mode": "trusted", "root_api_key": e["MEMORY_API_KEY"]},
    "storage": {"workspace": e["MEMORY_DATA_TARGET"], "vectordb": {"backend": "local"}},
    "embedding": {"dense": {"provider": "openai", "model": e["EMBEDDING_MODEL"],
        "api_base": e["EMBEDDING_BASE_URL"], "api_key": e["EMBEDDING_API_KEY"],
        "dimension": int(e["EMBEDDING_DIMENSION"]), "input": "text"}},
    "vlm": {"provider": "openai", "model": e["LLM_MODEL"], "api_base": e["LLM_BASE_URL"],
        "api_key": e["LLM_API_KEY"], "temperature": 0.1, "timeout": 300},
    "auto_generate_l0": False, "auto_generate_l1": False
}
Path(e["MEMORY_CONFIG_PATH"]).write_text(json.dumps(config))
# 向插件和就绪检查传入同一个测试租户。
p = Path("configs/openclaw.json")
claw = json.loads(p.read_text())
claw["plugins"]["entries"]["openviking"]["config"].update(accountId="locomo", userId="bench")
p.write_text(json.dumps(claw, indent=2) + "\n")

keys = [line.split("=", 1)[0] for line in Path(".env.example").read_text().splitlines()
        if line and not line.startswith("#")]
Path(".env").write_text("".join(f"{k}={e[k]}\n" for k in keys))
PY
```

这段命令在本次工作目录生成私有的 `.env` 和 `ov.conf`，并更新复制出来的 `configs/openclaw.json`。示例为独立的本机部署使用原生 `trusted` 鉴权，每次生成随机服务密钥，插件显式发送 `accountId` 和 `userId`。在已有的多租户服务上使用 `api_key` 模式时，数据接口需要租户用户密钥；root 管理密钥用于数据接口会返回 `PERMISSION_DENIED`。

### 3. 部署、测试、看报告

```bash
memory-bench experiment --config deployment.yaml
python - <<'PY'
import json
from pathlib import Path
p = max(Path("runs").glob("*/reports/summary.json"), key=lambda f: f.stat().st_mtime)
s = json.loads(p.read_text())
print("正式准确率:", s["benchmark_score"], "答对:", s["case_passed"], "/", s["case_total"])
print("评分覆盖率:", s["evaluation_coverage"], "就绪率:", s["checkpoint_ready_rate"],
      "有效性:", s["run_validity"]["valid"])
for name in ("run_report.html", "case_results.json", "token_summary.md"):
    print((p.parent / name).resolve())
PY
```

验收重点是 `run_validity.valid=true`、就绪率和评分覆盖率均为 1。答错也可以完成一次有效评测。要核实后端实际抽取了多少条，查看同一 run 的 `artifacts/step-stdout/*wait-ready.json` 中的 `output.task_result.memories_extracted`。评分成功也可能伴随自动召回降级。检查 `runs/experiments/<实验ID>/` 下的 Gateway 日志，搜索 `auto-recall search failed` 和 `injecting`，按时间与具体 QA 对照。Token 按报告实际观测范围解读。默认实验结束自动清理专用容器，报告保留在 `$WORK/runs`。

如果 Docker 报 `could not find an available, non-overlapping IPv4 address pool`，请管理员分配两段空闲且互不重叠的子网，在工作副本 `deployment.yaml` 的顶层 `network_subnets.openviking-openclaw` 中分别设置 `storage_net`、`agent_net_openviking_openclaw`，再运行实验命令。

## Mem0 + OpenClaw：用官方镜像运行全量 LoCoMo

本例使用 OSS 模式。官方 npm 插件 `@mem0/openclaw-mem0@1.2.0` 在 Gateway 进程内执行记忆抽取和召回，向量保存在独立的 pgvector 容器。需要聊天和 Embedding 服务的凭据，无需 Mem0 Cloud 账号。先完成本页“获取和安装”，以下命令在同一个 Bash 终端执行。[官方插件说明](https://github.com/mem0ai/mem0/tree/main/integrations/openclaw)

### 1. 准备官方软件和数据

```bash
set -eu
umask 077
REPO="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mem0-locomo.XXXXXX")"
echo "本次结果目录：$WORK"
cd "$WORK"
cp -r "$REPO/examples/mem0-openclaw/configs" "$REPO/examples/mem0-openclaw/deployment.yaml" .
export OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.6.9
export PGVECTOR_IMAGE=pgvector/pgvector:pg16
docker pull "$OPENCLAW_IMAGE"
docker pull "$PGVECTOR_IMAGE"
mkdir -p vendor
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD/vendor:/install" --entrypoint npm "$OPENCLAW_IMAGE" \
  install --prefix /install --omit=dev --no-audit --no-fund @mem0/openclaw-mem0@1.2.0
export PLUGIN_PATH="$PWD/vendor/node_modules/@mem0/openclaw-mem0"
test -s "$PLUGIN_PATH/dist/index.js"

git clone --quiet --depth 1 --branch master https://gitcode.com/weixin_44204324/locomo-test.git data-source
export DATA_PATH="$PWD/data-source/data/locomo10.json"
echo "5da1af63b66d5bba1c00e40eecff7a825ba202927f5c1e563670f89d0dfbfad4  $DATA_PATH" | sha256sum -c -
```

### 2. 配置模型和 Gateway

填写容器能访问的 OpenAI 兼容接口。向量维数要等于 Embedding 模型的实际输出。下面会为本次 pgvector 生成独立密码，并让 Judge 默认沿用回答模型。

```bash
read -rp '聊天 API Base URL（含 /v1）：' LLM_BASE_URL
read -rp '聊天模型名：' LLM_MODEL
read -rsp '聊天 API Key：' LLM_API_KEY; echo
read -rp 'Embedding API Base URL（含 /v1）：' EMBEDDING_BASE_URL
read -rp 'Embedding 模型名：' EMBEDDING_MODEL
read -rp 'Embedding 输出维度：' EMBEDDING_DIMENSION
read -rsp 'Embedding API Key：' EMBEDDING_API_KEY; echo
export LLM_BASE_URL LLM_MODEL LLM_API_KEY EMBEDDING_BASE_URL EMBEDDING_MODEL EMBEDDING_DIMENSION EMBEDDING_API_KEY
read -rp 'Judge 模型（回车沿用聊天模型）：' JUDGE_MODEL
export LOCOMO_BASE_URL="$LLM_BASE_URL" LOCOMO_METRIC_MODEL="${JUDGE_MODEL:-$LLM_MODEL}" LOCOMO_API_KEY="$LLM_API_KEY"
export DB_PASSWORD="$(python -c 'import secrets; print(secrets.token_hex(24))')"
export REPO
python - <<'PY'
from pathlib import Path
import json, os
e = os.environ
template = Path(e["REPO"]) / "examples/mem0-openclaw/.env.example"
keys = [s.split("=", 1)[0] for s in template.read_text().splitlines() if s and not s.startswith("#")]
Path(".env").write_text("".join(f"{k}={e[k]}\n" for k in keys))
p = Path("configs/openclaw.json")
config = json.loads(p.read_text())
oss = config["plugins"]["entries"]["openclaw-mem0"]["config"]["oss"]
oss["embedder"]["config"].pop("embeddingDims", None)
p.write_text(json.dumps(config, indent=2) + "\n")
PY
```

### 3. 部署、测试、查看结果

```bash
memory-bench experiment --config deployment.yaml
python - <<'PY'
import json
from pathlib import Path
p = max(Path("runs").glob("*/reports/summary.json"), key=lambda f: f.stat().st_mtime)
summary = json.loads(p.read_text())
print("正式准确率", summary["benchmark_score"], "评分覆盖率", summary["evaluation_coverage"])
print("运行有效", summary["run_validity"]["valid"])
for name in ("run_report.html", "case_results.json", "token_summary.md"):
    print((p.parent / name).resolve())
print("步骤回执", p.parent.parent / "artifacts/step-stdout")
PY
```

单段验收时，在历史写入和 QA 的步骤回执中分别检查 `plugin_checks.after_agent.output`：`native_marker` 为 `auto-captured` 或 `injecting`，`memory_count` 为对应条数。本配置的 Token 报告包含 Agent 接收历史和回答的用量。记忆侧抽取和 Embedding 用量缺失时显示为未知，不能把 Agent 两阶段的和当作系统总用量。

## Hindsight + OpenClaw：部署并运行 LoCoMo

在 Linux 的同一个 Bash 终端执行。准备 Git、Python 3.11、Docker Compose v2，以及宿主机和容器均可访问的 OpenAI 兼容聊天接口。示例用 Hindsight 官方完整镜像，镜像自带 PostgreSQL、Embedding 和重排模型。配置一个支持结构化输出的聊天模型，可同时用于 Hindsight、OpenClaw 和 Judge。[Hindsight 官方容器说明](https://hindsight.vectorize.io/developer/installation)

### 1. 安装 Workload 并创建测试目录

先完成上面的“获取和安装”，保持虚拟环境已激活，从仓库根目录执行。

```bash
REPO="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/hindsight-locomo.XXXXXX")"
cp -r "$REPO/examples/hindsight-openclaw/." "$WORK/"
cd "$WORK"
install -m 600 .env.example .env
printf '结果目录 %s\n' "$WORK"
```

### 2. 获取镜像和官方插件

```bash
export MEMORY_IMAGE=ghcr.io/vectorize-io/hindsight:1.2.1
export OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.6.9
docker pull "$MEMORY_IMAGE"

docker pull "$OPENCLAW_IMAGE"

mkdir -p vendor/hindsight-openclaw
export PLUGIN_PATH="$PWD/vendor/hindsight-openclaw"
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -v "$PLUGIN_PATH:/plugin" -w /plugin node:24-trixie-slim sh -ec '
    archive=$(npm pack --silent @vectorize-io/hindsight-openclaw@1.2.0)
    tar -xzf "$archive" --strip-components=1
    rm "$archive"
    npm install --omit=dev --ignore-scripts --legacy-peer-deps
  '
test -f "$PLUGIN_PATH/openclaw.plugin.json"
```

Workload 会只读挂载整个插件目录，并检查插件版本及文件摘要。请保留目录中的 `node_modules`。Hindsight 在容器内监听 `8888`，数据库写入本次部署的命名卷 `/home/hindsight/.pg0`。

### 3. 生成 LoCoMo 小样本并配置模型

以下命令生成 `conv-26` 的四段历史和 35 道题。先按证据所在会话筛选，再取筛选后第 2 至 36 题。

```bash
git clone --quiet --depth 1 --branch master \
  https://gitcode.com/weixin_44204324/locomo-test.git data-source
printf '%s  %s\n' \
  5da1af63b66d5bba1c00e40eecff7a825ba202927f5c1e563670f89d0dfbfad4 \
  data-source/data/locomo10.json | sha256sum -c -

python - <<'PY'
import json
import re
from pathlib import Path

source = json.loads(Path("data-source/data/locomo10.json").read_text())
sample = next(item for item in source if item["sample_id"] == "conv-26")
allowed = {"speaker_a", "speaker_b"} | {
    f"session_{i}{suffix}" for i in range(1, 5) for suffix in ("", "_date_time")
}
conversation = {k: v for k, v in sample["conversation"].items() if k in allowed}
questions = [
    q for q in sample["qa"]
    if q.get("evidence") and all(re.fullmatch(r"D[1-4]:[0-9]+", e) for e in q["evidence"])
][1:36]
assert len(conversation) == 10 and len(questions) == 35
Path("locomo-small.json").write_text(json.dumps([
    {"sample_id": sample["sample_id"], "conversation": conversation, "qa": questions}
], ensure_ascii=False))
PY
export DATA_PATH="$PWD/locomo-small.json"

read -r -p '聊天 API Base URL（含 /v1）: ' LLM_BASE_URL
read -r -p '聊天模型名: ' LLM_MODEL
read -rs -p 'API Key: ' LLM_API_KEY
printf '\n'
export LLM_BASE_URL LLM_MODEL LLM_API_KEY
export LOCOMO_BASE_URL="$LLM_BASE_URL"
export LOCOMO_METRIC_MODEL="$LLM_MODEL"
export LOCOMO_API_KEY="$LLM_API_KEY"
export MEMORY_BENCH_JUDGE_MAX_TOKENS=2048
```

Judge 可以换成独立接口，修改三个 `LOCOMO_*` 变量即可。较大的评分输出上限给推理模型留出输出判分的空间。若模型服务在宿主机上，填写容器可达的地址。

### 4. 一键部署、测试并查看结果

```bash
memory-bench experiment --config deployment.yaml
```

命令依次启动 Hindsight 和 OpenClaw、检查服务、提交历史并等待后台写入完成，然后运行 QA 和评分。默认自动清理本次创建的容器及数据库卷，保留结果文件。

```bash
python - <<'PY'
import json
from pathlib import Path

p = max(Path("runs").glob("*/reports/summary.json"), key=lambda f: f.stat().st_mtime)
s = json.loads(p.read_text())
for key in ("benchmark_score", "case_passed", "case_total", "evaluation_coverage",
            "checkpoint_ready_rate", "run_validity"):
    print(key, s.get(key))
print("qa_paths", s.get("resource_summary", {}).get("evaluation", {}).get("qa_paths"))
print((p.parent / "run_report.html").resolve())
PY
```

复核时查看有效性、就绪率和评分覆盖率，再用 `qa_paths` 确认问题是否走了记忆召回。实验状态与清理前归档的容器日志位于 `runs/experiments/<实验ID>/`。镜像拉取失败时，先完成第 2 步再运行实验。若关闭了自动清理，可在同一个工作目录执行 `memory-bench undeploy --deployment hindsight-openclaw-locomo`。

实测环境使用 Hindsight 官方 Python 包 1.2.1、独立 PostgreSQL、外部向量服务及 ONNX 重排，验证了同一 OpenClaw 插件的写入、召回和评分链路。上文的官方镜像启动、内置数据库与内置模型仍待容器实测。

## JiuwenMemory + OpenClaw：通过官方 MCP 运行全量 LoCoMo

OpenClaw 通过 Streamable HTTP 连接 JiuwenMemory v0.2.1 的官方 MCP 服务。历史阶段由 Agent 调用 `memory_add`，QA 阶段由只读 Agent 调用 `memory_search`。本例使用 JiuwenMemory 的 local profile 和外部聊天、向量模型。官方 Docker Compose 面向另一种 HTTP 服务，这里从官方 release 源码构建 MCP 镜像。[官方 MCP 说明](https://github.com/openJiuwen-ai/agent-memory/blob/v0.2.1/docs/features/F03-mcp-surface.md)

### 1. 准备镜像和数据

先完成本页的“获取和安装”。本示例将 Gateway 固定为 OpenClaw 2026.6.9，以保留 MCP 回执核验所需的完整会话标识。以下命令从 Workload 仓库根目录开始，在同一个 Bash 终端执行。

```bash
set -eu
umask 077
export REPO="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/jiuwen-locomo.XXXXXX")"
echo "本次结果目录：$WORK"
cd "$WORK"
EXAMPLE="$REPO/examples/jiuwenmemory-openclaw"
cp -r "$EXAMPLE/configs" "$EXAMPLE/deployment.yaml" "$EXAMPLE/.env.example" .
export OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.6.9
docker pull "$OPENCLAW_IMAGE"
docker run --rm --network none "$OPENCLAW_IMAGE" openclaw --version

git clone --quiet --depth 1 --branch v0.2.1 https://github.com/openJiuwen-ai/agent-memory.git
export MEMORY_IMAGE=workload-jiuwen-mcp:v0.2.1
cat >Dockerfile.mcp <<'DOCKERFILE'
FROM python:3.11-slim-bookworm
WORKDIR /app
COPY . .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --timeout 120 --retries 5 ".[mcp,security]"
CMD ["python", "-m", "jiuwen_memory_entry.mcp_server", "/config/config.yml"]
DOCKERFILE
docker build --build-arg PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}" \
  -t "$MEMORY_IMAGE" -f Dockerfile.mcp agent-memory

git clone --quiet --depth 1 --branch master https://gitcode.com/weixin_44204324/locomo-test.git data-source
export DATA_PATH="$PWD/data-source/data/locomo10.json"
echo "5da1af63b66d5bba1c00e40eecff7a825ba202927f5c1e563670f89d0dfbfad4  $DATA_PATH" | sha256sum -c -
```

### 2. 配置模型、MCP 认证和部署检查

填写宿主机和容器都能访问的模型接口。单段验证使用 `gpt-6-luna` 接收历史、抽取和答题，`gpt-5.6-luna` 评分。抽取默认使用同一个模型，也可以指定同一端点上的另一模型。命令生成本次部署的 MCP 密钥，并把它同时配置在服务端和 Gateway。工作目录中的配置含凭据，应保存在本机。

```bash
read -rp '聊天 API Base URL（含 /v1）：' LLM_BASE_URL
read -rp '聊天模型名：' LLM_MODEL
read -rsp '聊天 API Key：' LLM_API_KEY; echo
read -rp 'Embedding API Base URL（含 /v1）：' EMBEDDING_BASE_URL
read -rp 'Embedding 模型名：' EMBEDDING_MODEL
read -rp 'Embedding 输出维度：' EMBEDDING_DIMENSION
read -rsp 'Embedding API Key：' EMBEDDING_API_KEY; echo
read -rp 'JiuwenMemory 抽取模型（回车沿用聊天模型）：' MEMORY_LLM_MODEL
export MEMORY_LLM_MODEL="${MEMORY_LLM_MODEL:-$LLM_MODEL}"
export LLM_BASE_URL LLM_MODEL LLM_API_KEY EMBEDDING_BASE_URL EMBEDDING_MODEL EMBEDDING_DIMENSION EMBEDDING_API_KEY
umask 077
export MEMORY_PORT=8000 MEMORY_CONFIG_PATH="$PWD/native.json" MEMORY_CONFIG_TARGET=/config/config.yml MEMORY_DATA_TARGET=/data
read -rp 'Judge 模型（回车沿用聊天模型）：' JUDGE_MODEL
export LOCOMO_BASE_URL="$LLM_BASE_URL" LOCOMO_METRIC_MODEL="${JUDGE_MODEL:-$LLM_MODEL}" LOCOMO_API_KEY="$LLM_API_KEY"
python - <<'PY'
import json, os, secrets
from pathlib import Path
import yaml
e = os.environ
key = secrets.token_urlsafe(40)
g = dict(vector_enabled=True, graph_enabled=False, rerank_enabled=False,
         chunk_size=512, llm_model=e["MEMORY_LLM_MODEL"], llm_base_url="http://127.0.0.1:8799/v1",
         llm_api_key=e["LLM_API_KEY"], llm_max_tokens=4096,
         embedder_model=e["EMBEDDING_MODEL"], embedder_base_url="http://127.0.0.1:8799/v1",
         embedder_api_key=e["EMBEDDING_API_KEY"], embedder_dim=int(e["EMBEDDING_DIMENSION"]))
api = {"globals": g, "llm": {"default": {"target": "openai"}},
       "embedder": {"default": {"target": "openai"}},
       "extractor": {"default": {"target": "llm", "params": {"llm": "default"}}},
       "security": {"default": {"target": "standard", "params": {"authenticator":
           {"target": "api_key", "params": {"root_api_key": key}}}}}}
Path(e["MEMORY_CONFIG_PATH"]).write_text(json.dumps({"profile": "local", "memory_api": api}))
p = Path("configs/openclaw.json")
claw = json.loads(p.read_text())
claw["mcp"] = {"servers": {"jiuwen": {"headers": {"Authorization": "Bearer " + key},
                                      "requestTimeoutMs": 180000}}}
p.write_text(json.dumps(claw, indent=2))
keys = [s.split("=", 1)[0] for s in Path(".env.example").read_text().splitlines()
        if s and not s.startswith("#")]
Path(".env").write_text("".join(f"{k}={e[k]}\n" for k in keys))
p = Path("deployment.yaml")
cfg = yaml.safe_load(p.read_text())
mem = cfg["memories"]["jiuwenmemory"]
mem["environment"] = {"MCP_TRANSPORT": "http", "MCP_HOST": "0.0.0.0", "MCP_PORT": e["MEMORY_PORT"]}
probe = """import asyncio, json, os
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
async def check():
    async with streamable_http_client("http://127.0.0.1:"+os.environ["MCP_PORT"]+"/mcp") as (rx,tx,_):
        async with ClientSession(rx,tx) as client:
            await client.initialize()
            assert {"memory_add","memory_search"} <= {tool.name for tool in (await client.list_tools()).tools}
asyncio.run(check())
print(json.dumps({"version":"mcp-tools/1","endpoints":{},"reachable":{},"effective_parameters":{}}))
"""
mem["healthcheck"] = ["CMD", "python", "-c", probe]
mem["verify"] = {"command": ["python", "-c", probe], "expected_version": "mcp-tools/1"}
agent = cfg["agents"]["claw"]["verify"]["command"]
agent[-1] = """(async()=>{
const res=await fetch(process.env.MEMORY_URL+'/mcp',{method:'POST',
headers:{'Content-Type':'application/json','Accept':'application/json, text/event-stream'},
body:JSON.stringify({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-03-26',
capabilities:{},clientInfo:{name:'workload',version:'1'}}})});
if(!res.ok||!(await res.text()).includes('protocolVersion'))throw Error('MCP initialize failed');
console.log(JSON.stringify({version:require('/app/package.json').version,
endpoints:{MEMORY_URL:process.env.MEMORY_URL},reachable:{memory:true},effective_parameters:{}}))
})().catch(e=>{console.error(e.message);process.exit(1)})"""
proxy = {"memory_backend": "jiuwenmemory", "jiuwen_source_id_guidance": True,
         "llm_base_url": e["LLM_BASE_URL"], "embedding_base_url": e["EMBEDDING_BASE_URL"]}
Path("proxy.json").write_text(json.dumps(proxy))
mem["config_mounts"].extend([
    {"source": str(Path(e["REPO"]) / "memory_bench_platform/memory_bench_platform/token_proxy.py"),
     "target": "/app/token_proxy.py"},
    {"source": str(Path("proxy.json").resolve()), "target": "/config/proxy.json"},
])
mem["command"] = ["sh", "-ec",
    """python -c 'from token_proxy import serve; serve("/config/proxy.json", "/tmp/usage", 8799)' &
exec python -m jiuwen_memory_entry.mcp_server /config/config.yml"""]
Path("deployment.yaml").write_text(yaml.safe_dump(cfg,sort_keys=False))
PY
```

### 3. 部署、测试、核对报告

```bash
memory-bench experiment --config deployment.yaml
python - <<'PY'
import json
from pathlib import Path
p = max(Path("runs").glob("*/reports/summary.json"), key=lambda f: f.stat().st_mtime)
s = json.loads(p.read_text())
v = s["resource_summary"]["memory_plugin_finalize"]["output"]
print("正式准确率:", s["benchmark_score"], "答对:", s["case_passed"], "/", s["case_total"])
print("评分覆盖率:", s["evaluation_coverage"], "有效性:", s["run_validity"]["valid"])
print("写入核验:", v["ingest_verification"])
print("召回核验:", v["recall_verification"])
for name in ("run_report.html", "case_results.json", "token_summary.md"):
    print((p.parent / name).resolve())
PY
```

查看 `ingest_verification.verified` 和 `recall_verification.verified` 确认实际的读写回执；`with_evidence` 表示返回非空证据的题数。Token 报告可单独查看 Agent 写入和回答。本例为记忆侧启用 Workload 独占代理的 `jiuwen_source_id_guidance`，提示抽取模型使用消息外层 UUID，避免误用 LoCoMo 的对话行号。比较不同跑次时应记录此开关。代理请求用量记录在记忆容器的 `/tmp/usage/requests.jsonl`，默认未导入汇总报告；若报告显示记忆侧用量未知，请勿计为零。需要完整统计时，可为记忆侧配置独占的 [Workload 模型代理](../docs/token-usage-accounting-design.md)。Docker 地址池耗尽时，按前面示例为 `network_subnets.jiuwenmemory-openclaw` 指定管理员分配的 `storage_net` 和 `agent_net_jiuwenmemory_openclaw`。

## MemOS Local 与 OpenClaw 全量 LoCoMo

以下命令在 Linux 的同一个 Bash 终端执行。先完成上面的“获取和安装”，保持 Python 虚拟环境激活，从仓库根目录开始。准备容器可访问的 OpenAI 兼容聊天和 Embedding 接口。MemOS Local 以官方 npm 插件的形式运行在 OpenClaw 容器内，使用本地 SQLite。[官方插件说明](https://docs.openmem.net/openclaw/local_plugin/)

### 1. 准备 OpenClaw 镜像和 MemOS 插件

```bash
REPO="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/memos-locomo.XXXXXX")"
cp -r "$REPO/examples/memos-openclaw/." "$WORK/"
cd "$WORK"
install -m 600 .env.example .env
printf '结果目录 %s\n' "$WORK"

export OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.6.9
docker pull "$OPENCLAW_IMAGE"

mkdir -p vendor/memos-local
export PLUGIN_PATH="$PWD/vendor/memos-local"
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -v "$PLUGIN_PATH:/plugin" -w /plugin \
  --entrypoint sh "$OPENCLAW_IMAGE" -ec '
    archive=$(npm pack --silent @memtensor/memos-local-plugin@2.0.8)
    tar -xzf "$archive" --strip-components=1
    rm "$archive"
    npm install --omit=dev --no-audit --no-fund
    node -e "const DB = require(\"better-sqlite3\"); const db = new DB(\":memory:\"); db.close(); console.log(\"SQLite OK\");"
  '
test -f "$PLUGIN_PATH/openclaw.plugin.json"
```

安装依赖后应看到 `SQLite OK`。请保留插件目录中的 `node_modules`，Workload 会把整个目录只读挂载到网关。实验模板固定使用 `2.0.8`。每个样本是一整段 LoCoMo conversation，包含其全部 session 和 QA。Workload 复用同一个 OpenClaw 容器，在样本切换时停止网关，再换成空 SQLite 和新的会话状态。每个样本的写入和 QA 使用同一个库。

### 2. 获取全量 LoCoMo 并配置模型

```bash
git clone --quiet --depth 1 --branch master \
  https://gitcode.com/weixin_44204324/locomo-test.git data-source
printf '%s  %s\n' \
  5da1af63b66d5bba1c00e40eecff7a825ba202927f5c1e563670f89d0dfbfad4 \
  data-source/data/locomo10.json | sha256sum -c -

export DATA_PATH="$PWD/data-source/data/locomo10.json"

read -r -p '聊天 API Base URL（含 /v1）: ' LLM_BASE_URL
read -r -p '聊天模型名: ' LLM_MODEL
read -rs -p '聊天 API Key: ' LLM_API_KEY
printf '\n'
read -r -p 'Embedding API Base URL（含 /v1）: ' EMBEDDING_BASE_URL
read -r -p 'Embedding 模型名: ' EMBEDDING_MODEL
read -rs -p 'Embedding API Key: ' EMBEDDING_API_KEY
printf '\n'
export LLM_BASE_URL LLM_MODEL LLM_API_KEY
export EMBEDDING_BASE_URL EMBEDDING_MODEL EMBEDDING_API_KEY
export LOCOMO_BASE_URL="$LLM_BASE_URL"
export LOCOMO_METRIC_MODEL="$LLM_MODEL"
export LOCOMO_API_KEY="$LLM_API_KEY"
export MEMORY_BENCH_JUDGE_MAX_TOKENS=2048
```

聊天接口用于回答和默认 Judge。Embedding 接口必须提供 `/embeddings`。如果使用无需鉴权的内部 Embedding 服务，密钥可填 `local`。Judge 若使用独立服务，设置相应的三个 `LOCOMO_*` 变量。

MemOS 插件在 QA 阶段默认最多等待召回 **30 秒**。需要调整时，在当前目录的 `.env` 中修改 `MEMOS_BEFORE_PROMPT_SOFT_TIMEOUT_MS`（单位：毫秒）；同名 Shell 环境变量优先。比较评测结果时请记录实际值，因为超时后返回的记忆无法注入本次回答。

### 3. 部署、测试、看报告

```bash
memory-bench experiment --config deployment.yaml
```

一个命令启动一个专用容器，并依次执行全部 10 个 conversation。每个 conversation 先完成历史写入和只读 QA，再切换到下一个样本。样本切换会重启同一个容器，清除上一个样本的 SQLite、会话和运行状态。Workload 保留每个样本的写入和召回凭证。运行时保持样本串行。

```bash
python - <<'PY'
import json
from pathlib import Path

p = max(Path("runs").glob("*/reports/summary.json"), key=lambda f: f.stat().st_mtime)
s = json.loads(p.read_text())
print("答对", s["case_passed"], "/", s["case_total"])
print("正式准确率", s["benchmark_score"], "评分覆盖率", s["evaluation_coverage"],
      "就绪率", s["checkpoint_ready_rate"], "有效性", s["run_validity"]["valid"])
print((p.parent / "case_results.json").resolve())
print((p.parent / "run_report.html").resolve())
PY
```

全量数据保留原有 10 个 conversation 和全部历史。Workload 跳过 category 5，正式评测共计 1,540 题。默认自动清理容器及运行数据，保留 `$WORK/runs` 中的报告。逐样本历史导出清单及写入、召回凭证位于同一次 run 的 `artifacts/memory_plugin/openclaw-memos-local/`。

验证使用两个样本。先写入一个带干扰事实的样本，再切到第二个样本并运行一道 QA，检查只召回第二个样本的历史。全量示例沿用同一入口。

## 扩展与验证边界

新增系统复用同一 env_file/config_files/plugin_runtime 接口；差异仍放在该系统的
memory-plugin `entry.deployer`，不要在公共 Agent runner 中增加供应商判断。
新增 benchmark 使用已有 skill 和 data_path，不另写部署脚本。

六种示例均需通过配置加载、原生运行配置生成、密钥类型/权限检查；
这不等于六种系统都已完成真实模型准确率测试。真实容器和模型测试结果需单独列明。
旧部署 YAML 未声明 env_file/expand_env 时保持原语义，原性能采集入口继续可用。
