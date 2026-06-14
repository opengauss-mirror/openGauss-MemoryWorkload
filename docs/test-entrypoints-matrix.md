# 测试入口可用性矩阵

## 1. 目标

统一说明当前仓库内三条 LoCoMo / OpenClaw / OpenViking 相关测试入口的职责、依赖、稳定性状态与推荐用法，避免继续把“局部跑通”“单次结果可用”“长期稳定入口”混为一谈。

## 2. 入口矩阵

| 入口 | 当前用途 | 关键依赖 | 当前状态 | 推荐级别 | 已知问题 |
| --- | --- | --- | --- | --- | --- |
| `locomo_test` | LoCoMo `small` / `locomo10` 主跑数入口 | OpenClaw gateway、OpenViking、judge LLM、远端容器 `jcp-dev` | 可跑并已产出可信结果 | 高 | 旧 compact / task 兼容层仍需清理；部分 token 统计链路过时 |
| `memory_bench_platform` | 统一 workflow/case 平台、skill 验证、归档与报告 | benchmark skill、agent skill、native workflow、OpenClaw/OpenViking | 部分可跑 | 中 | 还未统一 external runner；OpenViking 真实 benchmark 入口未封装稳定 |
| `benchmark/locomo/openclaw/phase_a_off.py` 及 `run_clean_small_in_container.sh` | 官方 LoCoMo direct-ov 基线入口 | OpenViking benchmark 目录、OpenClaw 插件配置、远端容器 `jcp-dev` | 可跑但易串口径 | 中 | 并发 run 会污染插件配置、账号前缀与评测结果 |

## 3. 当前推荐

### 3.1 结果口径

- 当前可信的 LoCoMo `small` 准确率结果，优先以 `locomo_test` 产物为准。
- `memory_bench_platform` 当前更适合做平台闭环验证、skill 验证、native workflow 验证，而不是直接作为唯一权威跑分入口。
- OpenViking 官方 `phase_a_off.py` / `run_clean_small_in_container.sh` 更适合作为底层基线脚本，必须经过独占运行包装后再复用。

### 3.2 推荐顺序

1. `locomo_test`
   - 用于当前 `small` / `locomo10` 正式跑数与 accuracy 统计。
2. `memory_bench_platform`
   - 用于平台回归、workflow/case 能力验证、统一归档。
3. 官方 benchmark 脚本
   - 用于 direct-ov 行为回归和底层问题定位。

## 4. 稳定复用前提

- 所有远端跑数前必须先做独占清场，禁止并发 `phase_a_off.py`、`locomo_test`、裸 `openclaw-gateway` 重启脚本混跑。
- OpenClaw 插件配置中的 `accountId` / `userId` / `agent_prefix` 必须与本次 run 的命名空间一致。
- OpenViking 数据目录、OpenClaw `locomo-eval` 会话态、评测输出目录需要按 run 级隔离。
- 所有入口最终都必须落到统一 artifact 目录，并能导出 `accuracy`、`category stats`、`logs`、`resource summary`。

## 5. 当前判断

### 已可作为稳定主入口

- `locomo_test`
  - 已在远端 `OpenClaw + OpenViking` 环境跑出完整 `small` 结果。
  - 当前已验证结果：`30/35 = 85.71%`。

### 已可作为平台闭环入口，但不能单独代表最终跑分口径

- `memory_bench_platform`
  - 已完成 benchmark skill / agent skill / native workflow / archive / report 最小闭环。
  - 仍需补 external runner 统一接入与真实 benchmark 结果导入。

### 可跑但还不能裸用

- 官方 `phase_a_off.py` / `run_clean_small_in_container.sh`
  - 当前环境下可以执行。
  - 但如果远端已有别的 `phase_a_off.py` 或共享 gateway/plugin 配置未隔离，会直接串口径。

## 6. 后续收敛方向

- 为三条入口补统一远端独占锁和环境探测。
- 让 `memory_bench_platform` 支持 external runner，并把官方 benchmark 脚本纳入统一归档。
- 把 `locomo_test` 中旧 compact/task 兼容逻辑替换成与当前 OpenClaw / OpenViking 接口一致的实现。
