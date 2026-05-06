# Changelog

本文件记录所有显著的版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/), 版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.2.1] - 2026-05-06

### 鲁棒性改进

- **系统休眠唤醒检测**:笔记本合盖、虚拟机暂停等场景下,server 进程会随宿主机一起 suspend。醒来后 wall clock 已大幅推进,以往会导致所有 worker 被误标为 OFFLINE。现在 heartbeat 监控会检测到这种异常间隔(实际睡眠时长 - 预期间隔 > 30s),续期所有非 OFFLINE worker 的心跳后跳过本轮判定,给 worker 一次正常心跳的窗口。
- **WAL 周期 checkpoint**:SQLite WAL 模式下,wal 文件长期运行可能持续增长。heartbeat 后台任务现在每约 30 分钟主动执行一次 `PRAGMA wal_checkpoint(TRUNCATE)`,把 wal 文件缩回 0。

[0.2.1]: https://github.com/Spacebody/coop-server/releases/tag/v0.2.1

## [0.2.0] - 2026-05-06

### Breaking Changes

**协议层去业务化**:`submit_work` 的固定字段 `project / branch / commit_sha` 被移除, 替换为单一的可选字段 `artifact: dict`, 用于透传任意结构的工作产出信息。这让 Coop Server 完全协议中立, 不再假设 worker 使用 git 工作流。

**修改前**:
```python
submit_work(worker_id, task_id, project, branch, commit_sha, summary)
```

**修改后**:
```python
submit_work(worker_id, task_id, summary, artifact={
    "project": "myapp",
    "branch": "feature/login",
    "commit_sha": "abc1234"
})
```

worker 可以根据自己的工作流自由设计 artifact 字段。git 工作流推荐保留上述三个键以保持兼容性, 非 git 场景(数据分析、文档撰写等)则可以放别的字段(报告链接、文件列表、测试结果等)。

### 数据库迁移

DB schema 从 v1 升级到 v2:

- 新增列 `tasks.submitted_artifact` (TEXT, 存 JSON)
- 旧列 `submitted_project / submitted_branch / submitted_commit_sha` 保留(SQLite ALTER TABLE DROP COLUMN 兼容性原因), 但代码不再写入
- 已有数据自动迁移:旧三字段合并为 `submitted_artifact = {"project": ..., "branch": ..., "commit_sha": ...}`

迁移在 `init_db` 时自动执行。从 0.1.0 升级到 0.2.0 不需要手动操作, 但需要重启 server。

### 移除 projects.json 依赖

任务信息完全由人类输入决定。Coordinator 不预存工程清单, Worker 不维护 `projects.json` 这类配置文件。派任务时, 人类在对话中给出工程路径(如 `~/code/myapp`), Coordinator 将其原样保留在 task description 中, Worker 收到后自行解析路径并执行任务。

`coop init-worker` 不再生成 `projects.json` 模板。

### 设计原则

这两项变更体现的核心原则:**Coop Server 是纯通信中枢, 不感知业务语义**。所有业务知识都封装在自由文本的 task description 和自由结构的 artifact 中, server 只负责路由和持久化。

## [0.1.0] - 2026-05-04

首次公开发布。

### 定位

Coop 是协议层 LLM-agnostic 的协作 server, 支持任何兼容 MCP 的 AI 编程 agent 接入 (Claude Code、Codex CLI、Gemini CLI 等)。当前测试主要在 Claude Code 上完成, 其他 agent 需要自行配置 MCP server 连接。

### 设计原则

**任务信息完全由人类输入决定**。Coordinator 不预存工程清单, Worker 不维护 `projects.json` 这类配置文件。
派任务时, 人类在对话中给出工程路径(如 `~/code/myapp`),Coordinator 将其原样保留在 task description 中, Worker 收到后自行解析路径并执行任务。
这与"反馈式派单"哲学一致——所有运行时信息都来自任务本身, 不依赖预先声明。

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

[0.1.0]: https://github.com/Spacebody/coop-server/releases/tag/v0.1.0
