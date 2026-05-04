# Changelog

本文件记录所有显著的版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/), 版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-05-04

首次公开发布。

### 定位

Coop 是协议层 LLM-agnostic 的协作 server, 支持任何兼容 MCP 的 AI 编程 agent 接入 (Claude Code、Codex CLI、Gemini CLI 等)。当前测试主要在 Claude Code 上完成, 其他 agent 需要自行配置 MCP server 连接。

### 术语规范

项目统一使用以下术语:

- **Coop Server**: 基础设施 server 进程
- **Coordinator** (角色): 运行在某机器上的 AI agent, 进入协调者模式
- **Worker** (角色): 运行在某机器上的 AI agent, 进入 worker 模式
- `coop`: 客户端命令行工具
- `coopctl`: Coop Server 进程管理工具

详见 README 的"核心概念"和 `docs/architecture.md` 的"术语"部分。

### 新增

- **MCP server**: 基于 `mcp` 库实现的 Streamable HTTP transport server
  - 19 个 MCP 工具 (Worker 端 10, Coordinator 端 9)
  - SQLite + WAL 持久化
  - Bearer token 鉴权
  - 长轮询事件机制
  - 心跳监控与自动 OFFLINE 标记
  - mDNS 服务发现 (`_coop._tcp.local.`)

- **`coop` CLI**: 面向运维和测试的客户端工具
  - `discover` / `doctor` / `ping`: 连接诊断
  - `list-workers` / `list-tasks` / `prune-workers`: 状态查询
  - `simulate-worker` / `simulate-coordinator`: 不依赖 LLM 的模拟器
  - `smoke-test` / `test-clarification` / `test-blocked`: 端到端测试
  - `stress-test`: 稳定性压测, 失败时自动写日志
  - `init-coordinator` / `init-worker`: 工作目录初始化

- **`coopctl`**: Coop Server 进程管理脚本
  - `start` / `stop` / `restart` / `status`: 进程控制
  - `logs` / `launchd-logs`: 日志查看
  - `reset`: 清空数据库
  - 同时支持 launchd 模式 (开机自启) 和 manual 模式

- **macOS 部署脚本**:
  - `install.sh`: server 端一键部署 (默认 `./coop-server` 子目录)
  - `install-worker.sh`: worker 端一键部署 (交互式询问 token 路径与 Coop Server 地址)
  - `uninstall.sh` / `uninstall-worker.sh`: 卸载, 默认保留数据, `--purge` 用于彻底删除

- **配置系统**:
  - 多优先级查找: 命令行参数 > `COOP_CONFIG` > `COOP_PREFIX` > 自动反推 > `~/.coop/`
  - CLI 自动从 venv 安装位置反推 PREFIX, 无需环境变量
  - 支持 server 端 (`$PREFIX/client/client.yaml`) 与 worker 端 (`$PREFIX/client.yaml`) 两种布局

- **Personas**: Coordinator 和 Worker 的 Claude Code 角色提示词

- **文档**:
  - 架构设计 (`docs/architecture.md`)
  - 协议参考 (`docs/protocol.md`)
  - 运维手册 (`docs/operations.md`)
  - 故障排查 (`docs/troubleshooting.md`)

### 测试覆盖

183 个单元测试覆盖核心模块:

- store / db (35)
- waiters (16)
- heartbeat (11)
- auth (12)
- worker_tools (21)
- coordinator_tools (22)
- cli config (29)
- discovery filter (14)
- e2e (7)
- 其他模块 (16)

[0.1.0]: https://github.com/yourusername/coop/releases/tag/v0.1.0
