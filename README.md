# Coop

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-183%20passed-brightgreen.svg)](#测试)

支持多个 AI 编程 agent 在局域网内协作开发的 MCP server。

一台机器上的 agent 担任 **Coordinator**,负责派发任务、review 与关闭任务;其他机器上的 agent 担任 **Worker**,负责接收任务、编写代码与提交。中间由一个轻量的 MCP server 进行通信中转。

协议层基于标准 MCP,任何兼容 MCP 的 AI agent 均可接入。当前测试主要在 [Claude Code](https://claude.com/product/claude-code) 上完成;Codex CLI、Gemini CLI、Cursor 等同样可用,接入方式见下文。

## 适用场景

- 团队成员使用 AI 编程 agent 共同开发同一项目,需要任务分发与协调
- 由一个 agent 统筹全局,跨机器派发任务给其他 agent
- 在内部局域网中协作,不希望将代码或任务记录上传至云服务

## 核心概念

| 术语 | 含义 |
|---|---|
| **Coop Server** | MCP server 进程,整个团队 / 局域网仅运行一个,作为通信中枢 |
| **Coordinator**(角色) | 运行在某台机器上的 AI agent,负责派发任务、review、关闭任务 |
| **Worker**(角色) | 运行在某台机器上的 AI agent,负责接收任务、编写代码、提交 |
| `coop` | 命令行工具,用于诊断、模拟、压测等 |
| `coopctl` | Coop Server 进程管理命令 |

Coordinator 和 Worker 是 agent 的运行时角色,与所在机器或所用 LLM 无关——决定角色的是工作目录中的 system prompt 与 MCP 配置。

## 架构概览

```
机器 A (Mac):                          机器 B / C (Mac):
┌──────────────────┐                  ┌──────────────────┐
│ Coop Server      │                  │ Worker           │
│ (后台进程)        │◄────── MCP ─────►│ (AI agent)       │
└────────┬─────────┘                  └──────────────────┘
         │ MCP
   ┌─────┴──────┐
   │ Coordinator│
   │ (AI agent) │
   └────────────┘
```

Coop Server 可以与 Coordinator 部署在同一台机器,也可以独立部署。整个团队仅运行一个 Coop Server,数据不在多个 server 实例间共享。

## 特性

- **协议无关**:任何 MCP 兼容的 AI agent 均可接入
- **薄协议设计**:server 仅负责通信中转,业务决策由 LLM 完成
- **零中心化基础设施**:SQLite 持久化,mDNS 自动发现,无需 Redis 或消息队列
- **反馈式派单**:Worker 无法执行任务时通过 `report_blocked` 反馈,无需预先声明能力
- **本地优先**:Bearer token 鉴权,数据保留在局域网内
- **完整工具链**:一键部署、状态查询、压测、模拟器(无需 LLM 即可验证)

## 快速开始

### 先决条件

- macOS(开发机)或 Linux(仅 server)
- Python 3.11+
- 任意 MCP 兼容的 AI 编程 agent(推荐 [Claude Code](https://claude.com/product/claude-code))

### 1. 部署 Coop Server(机器 A)

```bash
git clone https://github.com/Spacebody/coop-server.git
cd coop
./deploy/macos/install.sh
```

执行后:

- 创建 `./coop-server/` 部署目录(自包含,可备份与迁移)
- 自动启动 Coop Server 进程
- 在 `~/bin/coop` 创建软链接(若 `~/bin` 已在 PATH 中)

### 2. 验证 Coop Server

```bash
coop doctor          # 全部检查通过即可正常使用
coop smoke-test      # 端到端通信测试
```

### 3. 部署 Worker 客户端(机器 B、C 等)

将源码包传输至目标机器:

```bash
cd ~/work
tar -xzf coop.tar.gz
cd coop
./deploy/macos/install-worker.sh
# 交互式询问 token 路径(从机器 A 共享盘获取)与 Coop Server 地址
```

### 4. 启动协作(以 Claude Code 为例)

```bash
# 机器 A:Coordinator
cd ~ && mkdir coord-work && cd coord-work
coop init-coordinator --dir .
claude
# 在 Claude Code 中输入"按 CLAUDE.md 启动"

# 机器 B:Worker
cd ~ && mkdir worker-work && cd worker-work
coop init-worker --dir .
# 编辑 projects.json,填写本机工程路径
claude
# 在 Claude Code 中输入"按 CLAUDE.md 启动"
```

详细步骤参见 [`docs/operations.md`](docs/operations.md)。

## 用其他 AI agent 接入

Coop Server 协议层基于标准 MCP,任何兼容 MCP 的 agent 均可接入。当前 `coop init-coordinator` 与 `coop init-worker` 仅生成 Claude Code 配置(`.mcp.json` 与 `CLAUDE.md`),其他 agent 需要手动配置。

### MCP server 连接信息

无论使用哪种 agent,均需要以下信息连接 Coop Server:

```
URL:    http://<coop-server-host>:7777/mcp/
Token:  Bearer <token>  (位于 $PREFIX/data/token,通常通过共享网盘分发)
```

### Codex CLI

在 `~/.codex/config.toml` 中加入:

```toml
[mcp_servers.coop]
url = "http://<coop-server-host>:7777/mcp/"
headers = { Authorization = "Bearer YOUR_TOKEN_HERE" }
```

具体格式参考 [Codex MCP 文档](https://github.com/openai/codex)。

### Gemini CLI

参考 [Gemini CLI MCP 集成](https://github.com/google-gemini/gemini-cli) 文档,通常在 `~/.gemini/config.json` 中加入 MCP server 配置。

### Cursor / Windsurf 等编辑器

这些 IDE 通常提供内置的 MCP 配置界面,填入上述 URL 与 token 即可。

### 让 agent 进入 Coordinator / Worker 角色

不同 agent 加载 system prompt / persona 的方式不同。可参考 `personas/coordinator.md` 与 `personas/worker.md`(为 Claude Code 编写),按所用 agent 的习惯改写。

核心指令:

- **Coordinator**:启动时调用 `list_workers`,之后等待人类指令派发任务
- **Worker**:启动时调用 `register_worker`,之后循环调用 `wait_for_task` 接收任务

欢迎社区贡献其他 agent 的接入示例(参见 [CONTRIBUTING.md](CONTRIBUTING.md))。

## 文档

- [`docs/architecture.md`](docs/architecture.md) — 架构总览与设计决策
- [`docs/protocol.md`](docs/protocol.md) — MCP 工具完整 API
- [`docs/operations.md`](docs/operations.md) — 详细运维手册
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — 故障排查
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — 贡献指南
- [`CHANGELOG.md`](CHANGELOG.md) — 版本历史

## CLI 工具

### `coop` — 客户端工具

```
连接诊断:
  discover            通过 mDNS 扫描局域网内的 Coop Server
  doctor              诊断本机环境
  ping                测试与 Coop Server 的连接

状态查询:
  list-workers        查看在线 Worker (--all 包含 OFFLINE)
  list-tasks          查看任务
  prune-workers       清理失联 Worker 记录

测试与模拟:
  smoke-test          端到端通信验证 (无需 LLM)
  test-clarification  测试双向交互
  test-blocked        测试反馈式派单
  simulate-worker     启动不依赖 LLM 的 Worker 模拟器
  simulate-coordinator 启动不依赖 LLM 的 Coordinator 模拟器 (交互式派发任务)
  stress-test         稳定性压测,失败时自动写入日志

初始化 (Claude Code 适配):
  setup-mcp           生成 .mcp.json
  init-coordinator    初始化 Coordinator 工作目录
  init-worker         初始化 Worker 工作目录
  init-client-config  生成 client.yaml 模板
```

### `coopctl` — Coop Server 管理

```
start / stop / restart  Coop Server 进程控制
status                  查看运行状态
logs / launchd-logs     查看日志
token                   显示当前 token
config                  查看配置
reset --yes             清空数据库并重启
```

## 测试

183 个单元测试与集成测试,约 11 秒完成:

```bash
pip install -e ".[dev]"
pytest
```

运行端到端协议层验证(需要本地启动 Coop Server):

```bash
coop smoke-test
coop stress-test --iterations 30
```

测试不依赖 LLM——`simulate-worker` 与 `simulate-coordinator` 直接通过 MCP 协议验证通信链路,适用于 CI 或未安装 agent 的环境。

## 设计哲学

> Worker 不上报能力,Coordinator 不维护能力清单。任务直接派发,无法执行时由 Worker 反馈 `report_blocked`。

这种反馈式派单避免了维护 "worker 能力声明" 这类高成本元数据,将决策权完全交给 LLM。详细的设计决策记录见 [架构文档](docs/architecture.md)。

## 现状与限制

**当前可用**:

- macOS 一键部署(server 与 worker)
- Claude Code 完整工作流(派发任务、review、清理)
- 任意 MCP 兼容 agent 的协议层接入

**计划支持**(欢迎 PR):

- Linux / Windows 一键部署脚本
- 其他 agent 的配置生成命令(`setup-codex` / `setup-gemini` 等)
- 大规模场景(10+ Worker)的性能测试
- 用于查看任务进展的 Web Dashboard

**设计上不计划支持**:

- 公网部署的安全加固(mTLS、OAuth 等)——Coop 的部署目标是受信任的局域网
- worker 之间的细粒度权限隔离——拥有 token 即可派发任务给任意 worker
- 多 Coop Server 之间的 federation——同一团队建议运行单个实例

## License

MIT,详见 [LICENSE](LICENSE)。

## 贡献

欢迎提交 PR、issue 或参与设计讨论。提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

优先关注的方向:

- 其他 AI agent 的接入示例(Codex CLI、Gemini CLI、Cursor 等)
- Linux 部署脚本(systemd 适配)
- 实际使用反馈与性能数据

## 相关项目

- [MCP](https://modelcontextprotocol.io) — Model Context Protocol 规范
- [Claude Code](https://claude.com/product/claude-code) — Anthropic 官方 CLI 编程工具
- [Codex CLI](https://github.com/openai/codex) — OpenAI 的 CLI 编程工具
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) — Google 的 CLI 编程工具
