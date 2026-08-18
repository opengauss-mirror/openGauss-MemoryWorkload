# LoCoMo packaged test entrypoints

统一存放 LoCoMo / OpenClaw / OpenViking 测试入口的共享辅助脚本。

## 当前文件

- `remote_run_lock.sh`
  - 远端独占运行包装器。
  - 目标：避免多个 benchmark run 同时修改 OpenClaw 插件配置、OpenViking 数据目录或共享 gateway 进程。
- `probe_remote_env.py`
  - 探测 OpenClaw gateway / OpenViking / 插件配置 / auth profile / 版本要求。
- `reset_remote_locomo_env.py`
  - 为 LoCoMo 跑数生成远端环境重置计划。
- `collect_run_artifacts.py`
  - 统一收集 CSV / JSON / log / summary 到 run 目录。
- `run_official_locomo_small.sh`
  - 官方 LoCoMo `small` 稳定 wrapper。
  - 远端运行 `phaseA`，本地补 judge 与 `meta.json`。
- `run_official_locomo_sample.sh`
  - sample 级别 wrapper，复用 `run_official_locomo_small.sh`。
- `run_locomo_test_remote.sh`
  - `locomo_test` 主入口的远端稳定 wrapper。
  - 自动同步本地 `locomo_test` 代码、生成远端 `env.toml`，并在容器内跑完整 pipeline。
- `diagnose_openviking_split.py`
  - 远端 OpenViking local vectordb 运行态/持久态分叉诊断脚本。
  - 统一收集以下证据：
    - `observer/vikingdb`
    - `search/find`
    - `search/search`
    - `system/consistency`
    - fresh backend `collection_exists / collection_meta / account_rows`
    - 复制后 candidate store 条数
  - 目标：把“平台提取问题”和“OpenViking 本地 vectordb 持久化残缺”明确区分开。

## 预期新增

- `validate_three_entrypoints.sh`
  - 顺序触发三条入口验证的辅助脚本。
