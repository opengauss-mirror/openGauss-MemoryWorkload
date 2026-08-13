# locomo-test-kit

用这套脚本跑 LoCoMo small 测试。下面以 `ogmem` 为例，用户拿到测试文件后按顺序执行即可。

## 1. 准备环境

从 MemoryWorkloadTest 仓库根目录统一安装平台和 LoCoMo 执行层：

```bash
cd /path/to/MemoryWorkloadTest
```

安装依赖：

```bash
python3 -m pip install -e .
python3 -m locomo_test.cli --help
```

`locomo_test` 会调用 `memory_bench_platform` 的桥接模块，因此仓库根目录是官方安装入口。
只有在 `memory-bench-platform` 已单独安装或可从包索引获取时，才应进入
`locomo_test` 子目录单独安装。

需要提前准备：

- Docker 可用。
- `ogmem:520` 镜像可用。
- `openclaw-ogmemory:poc1_416` 镜像可拉取。
- Judge LLM 的 `api_key`、`base_url`、`model`。

## 2. 启动 ogmem 和 openclaw

```bash
docker rm -f ogmem 2>/dev/null || true
docker run -d --name ogmem --network host \
  -v /data1/sundechao/ogmem/testfs/ogmemory.yaml:/etc/ogmem/config.yaml:ro \
  -v /data1/sundechao/ogmem/testfs/ogmem:/data/agfs \
  ogmem:520

docker rm -f openclaw_ogmem 2>/dev/null || true
mkdir -p /data1/sundechao/ogmem/testfs/ogmem_ws
docker run -d --name openclaw_ogmem --network host \
  -v /data1/sundechao/ogmem/testfs/openclaw:/home/node/.openclaw \
  -v /data1/sundechao/ogmem/testfs/openclaw.json:/home/node/.openclaw/openclaw.json \
  -v /data1/sundechao/ogmem/testfs/ogmem_ws:/tmp/ogmem_ws \
  swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/openclaw-ogmemory:poc1_416
```

检查服务：

```bash
curl -s http://127.0.0.1:8090/api/v1/health
curl -s http://127.0.0.1:18790/health
docker logs --tail 100 openclaw_ogmem 2>&1 | grep 'remote services verified'
```

## 3. 配置测试

复制环境配置：

```bash
cp configs/env.toml.example configs/env.toml
```

编辑 `configs/env.toml`，至少填这些值：

```toml
[gateway]
port = 18790
token = "<openclaw auth token>"
state_dir = "/data1/sundechao/ogmem/testfs/openclaw"

[ogmem]
api_url = "http://127.0.0.1:8090"
docker_container = "ogmem"
wait_timeout = 900
wait_interval = 2.0
log_tail = 500

[judge]
api_key = "<judge api key>"
base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"
model = "<judge model>"
api_format = "openai"
parallel = 5
```

创建 `configs/ogmem-small.toml`：

```toml
[general]
name = "ogmem-small"
env_file = "env.toml"
dataset = "small"
memory_mode = "ogmem"
parallel = 1
user = "ogmem-small"
agent_id = "main"
output_dir = "/data1/sundechao/omv2/locomo/openclaw/output"

[session]
policy = "isolated"

[steps]
health_check = true
ingest = true
qa = true
judge = true
stats = true
```

## 4. 可选：清理旧数据

如果要干净重跑，先清理：

```bash
rm -rf /data1/sundechao/ogmem/testfs/openclaw/agents/main/sessions/*
rm -rf /data1/sundechao/ogmem/testfs/openclaw/agents/main/archive/*
rm -rf /data1/sundechao/ogmem/testfs/openclaw/agents/main/agent/*
rm -rf /data1/sundechao/ogmem/testfs/openclaw/tasks/*
rm -rf /data1/sundechao/ogmem/testfs/openclaw/logs/*
rm -rf /data1/sundechao/ogmem/testfs/ogmem/*
rm -rf /data1/sundechao/ogmem/testfs/ogmem_ws/*
rm -rf output/ogmem-small

docker restart ogmem
docker restart openclaw_ogmem
```

## 5. 运行测试

先做健康检查：

```bash
python3 -m locomo_test.cli check configs/ogmem-small.toml
```

跑完整 small：

```bash
python3 -m locomo_test.cli run configs/ogmem-small.toml
```

只验证 ingest 和 QA，不跑 judge：

```bash
python3 -m locomo_test.cli run configs/ogmem-small.toml --only health_check,ingest,qa
```

## 6. 查看进度和结果

看流水线日志：

```bash
tail -f output/ogmem-small/pipeline.log
```

看 ogmem 每个 session 是否抽取完成：

```bash
docker logs --since 30m ogmem 2>&1 | grep 'after_turn background extract done'
```

不要只用 `docker logs --tail 500 ogmem` 查历史完成日志，QA 阶段日志很多，可能把 ingest 阶段日志挤出最后 500 行。

看最终结果：

```bash
python3 -m json.tool output/ogmem-small/meta.json
```

主要输出文件：

```text
output/ogmem-small/qa_results.csv
output/ogmem-small/qa_diagnostics.json
output/ogmem-small/meta.json
output/ogmem-small/pipeline.log
```

## OpenViking 闭环诊断

当 `memory_mode = "openviking"` 时，`qa_results.csv` 会额外写出以下字段：

- `ov_memory_written`
- `ov_token_emitted`
- `ov_index_available`
- `ov_closure_state`

其中 `ov_closure_state` 当前重点关注：

- `memory_closed_loop_ready`
  - 说明 token、memory、index 三段信号都闭合。
- `memory_written_but_index_unavailable`
  - 说明 memory 已写入、token 已产出，但 index consistency 仍异常，`search/find/recall` 仍不可用。

即便不跑 `stats`，QA 阶段结束后也会自动生成：

```text
output/<run-name>/qa_diagnostics.json
```

其中包含 run 级聚合：

- `issues`
- `ov_closure_counts`
- `ov_closure_summary`

例如：

```json
{
  "issues": {
    "openviking_index_missing_records_max": 52,
    "openviking_memory_written_but_index_unavailable": 3
  },
  "ov_closure_counts": {
    "memory_written_but_index_unavailable": 3
  },
  "ov_closure_summary": {
    "dominant_state": "memory_written_but_index_unavailable",
    "has_index_unavailable": true,
    "has_memory_written": true,
    "has_token_emitted": true
  }
}
```

## 常见问题

如果健康检查失败：

```bash
docker logs --tail 100 ogmem
docker logs --tail 100 openclaw_ogmem
```

如果 ingest 看起来慢，先看：

```bash
tail -f output/ogmem-small/pipeline.log
docker logs --since 30m ogmem 2>&1 | grep 'after_turn background extract done'
curl -s http://127.0.0.1:8090/api/v1/token_stats | python3 -m json.tool
```

`ogmem` 模式会等每个 session 的 `after_turn background extract done` 后再继续下一个 session，所以 ingest 时间主要取决于 ogmem 后台抽取耗时。

如果代理导致请求失败，运行前清掉代理：

```bash
unset https_proxy HTTP_PROXY http_proxy HTTPS_PROXY all_proxy ALL_PROXY
```
