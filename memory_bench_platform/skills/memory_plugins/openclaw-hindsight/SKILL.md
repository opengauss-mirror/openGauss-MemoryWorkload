# OpenClaw + Hindsight

官方插件 1.2.0，自部署 API，原生自动 retain/recall。专用串行 HTTP Gateway；通过现有 memory-plugin/1 生命周期接入。历史回执与后台完成在 after_agent 检查，QA 只读；失败保留证据且不自动重试。

配置、边界及运行命令见 docs/hindsight-agent-plugin.md。
