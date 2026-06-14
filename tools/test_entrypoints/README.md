# tools/test_entrypoints

统一存放 LoCoMo / OpenClaw / OpenViking 测试入口的共享辅助脚本。

## 当前文件

- `remote_run_lock.sh`
  - 远端独占运行包装器。
  - 目标：避免多个 benchmark run 同时修改 OpenClaw 插件配置、OpenViking 数据目录或共享 gateway 进程。

## 预期新增

- `probe_remote_env.py`
  - 探测 OpenClaw gateway / OpenViking / 插件配置 / auth profile / 版本要求。
- `reset_remote_locomo_env.py`
  - 为 LoCoMo 跑数做数据备份、清理、恢复。
- `collect_run_artifacts.py`
  - 统一收集 CSV / JSON / log / summary 到 run 目录。
