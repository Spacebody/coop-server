# 架构总览

Coop 是一个让多台 AI 编程 agent 在局域网内协作开发的系统。本文档描述高层架构与关键设计决策,面向需要理解或改造系统的开发者。

> **关于 LLM 中立性**:Coop Server 协议层基于标准 MCP,任何支持 MCP 的 agent 均可接入。文档示例与 `init-coordinator` / `init-worker` 工具针对 Claude Code 优化,但架构上不依赖任何特定 LLM。

## 术语

以下术语在整个项目中保持一致使用:

| 术语 | 含义 |
|---|---|
| **Coop Server** | 基础设施进程,运行在某台机器上,通过 MCP 协议为 Coordinator 与 Worker 提供通信中转。整个团队 / 局域网仅运行一个 |
| **Coordinator**(角色) | 运行在某台机器上的 AI agent(Claude Code / Codex CLI / Gemini CLI 等)进入 coordinator 模式。负责派发任务、review、关闭任务 |
| **Worker**(角色) | 运行在某台机器上的 AI agent 进入 worker 模式。负责接收任务、编写代码、提交 |
| **`coop`** | 命令行工具,用于诊断、模拟、压测等 |
| **`coopctl`** | Coop Server 进程管理命令 |

**关键认知**:

- Coop Server 是**进程**,是基础设施
- Coordinator 与 Worker 是 **AI agent 的运行时角色**,与所在机器或所用 LLM 无关
- Coordinator 与 Worker 之间**不直接通信**,所有交互均经过 Coop Server
- Coop Server 与 Coordinator **可以**部署在同一台机器(简化部署),也可以独立部署

## 核心问题

AI 多人协作场景需要解决以下问题:

- 多台机器各运行一个 AI agent 实例,如何区分协调者与执行者?
- 任务如何从 Coordinator 派发到 Worker?
- Worker 完成后如何通知 Coordinator review?
- 网络中断、机器故障时如何恢复?

Coop 针对这些问题提供了最简化的解决方案。

## 设计原则

### 1. 薄协议层

Server **只做通信中转**,不规定:

- 任务的拆解方式(由 Coordinator 决定)
- Worker 的执行方式(由 Worker 决定)
- 失败处理策略(Coordinator 根据 reason 判断)

Server 仅负责:

- Worker 注册 / 心跳 / 失联检测
- 任务派发(根据 assignee 路由,不做能力匹配)
- 事件通知(submit / blocked / clarification 等)
- 持久化所有状态

### 2. 反馈式派单

Coordinator **不需要**预先知道哪个 Worker 适合执行哪些任务。直接派发,Worker 收到后自行判断:

- 能执行 → 直接执行
- 不能执行 → `report_blocked` 反馈给 Coordinator
- 信息不足 → `request_clarification` 进行双向交互

Coordinator 根据 Worker 反馈决定下一步(重新分配、调整任务、取消)。这种设计避免了维护 "worker 能力声明" 这类高成本元数据。

### 3. 长轮询与事件驱动

Worker 调用 `wait_for_task` 阻塞等待任务,Coordinator 调用 `wait_for_event` 阻塞等待事件。
选择长轮询而非 SSE / WebSocket 的原因:

- MCP 协议原生支持工具调用,无需额外协议层
- 客户端实现简单(AI agent 调用工具是原生交互方式)
- 超时自然降级(client 设置 timeout,server 设置 max_timeout)

### 4. 持久化优先

所有状态写入 SQLite(WAL 模式),server 重启不丢失数据:

- Worker 注册状态
- 任务状态机(pending / in_progress / submitted / closed / cancelled / abandoned)
- 事件历史
- 心跳时间戳
- Clarification 问答对

Worker 与 Coordinator 自身均是无状态的,权威状态仅保存在 server 端。

## 系统组件

```
┌─────────────────────────────────────────────┐
│           Coop Server (单进程)               │
│                                             │
│  ┌───────────────┐  ┌──────────────────┐    │
│  │ MCP Server    │  │ Heartbeat        │    │
│  │ (Streamable   │  │ Monitor          │    │
│  │  HTTP)        │  │ (后台协程)        │    │
│  └───────┬───────┘  └────────┬─────────┘    │
│          │                   │              │
│  ┌───────┴───────────────────┴───────────┐  │
│  │           Waiters                     │  │
│  │  (任务通知 / 事件 / 清理 / 答复)       │  │
│  └───────────────┬───────────────────────┘  │
│                  │                          │
│  ┌───────────────┴───────────────────────┐  │
│  │           SQLite Store                │  │
│  │   workers / tasks / events / aux      │  │
│  └───────────────────────────────────────┘  │
│                                             │
│  ┌───────────────────────────────────────┐  │
│  │   mDNS Advertiser (可选,默认开)        │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
              ▲                    ▲
              │ HTTP/MCP           │ HTTP/MCP
   ┌──────────┴────────┐  ┌────────┴──────────┐
   │ Coordinator Agent │  │ Worker Agent      │
   │ (派任务、review)   │  │ (接任务、提交)     │
   └───────────────────┘  └───────────────────┘
```

## 数据模型

### Worker

```
worker_id        unique str    e.g. "alice-mbp"
hostname         str
status           enum          idle / working / blocked / offline
current_task_id  str | null
last_heartbeat   timestamp
registered_at    timestamp
```

### Task

```
task_id          unique str    e.g. "T-001"
assignee         worker_id     fk
description      text          自然语言任务描述
priority         enum          high / normal / low
status           enum          pending / in_progress / submitted /
                               closed / cancelled / abandoned
parent_task_id   task_id | null
depends_on       json array
project          str | null    worker submit 时填
branch           str | null    worker submit 时填
commit_sha       str | null    worker submit 时填
summary          str | null    worker submit 时填
dispatched_at    timestamp
submitted_at     timestamp | null
closed_at        timestamp | null
```

### Event

```
event_id         autoincrement
event_type       str           worker_registered / work_submitted /
                               worker_blocked / clarification_requested /
                               clarification_answered / cleanup_done / ...
worker_id        str | null
task_id          str | null
payload          json
created_at       timestamp
consumed         bool
```

### Clarification

```
task_id          fk
question         text
answer           text | null
asked_at         timestamp
answered_at      timestamp | null
```

## 任务生命周期

```
                    publish_task
   PENDING ────────────────────────────► IN_PROGRESS
                                              │
                       ┌──────────────────────┤
                       │                      │
                       │  worker 主动:         │
                       │  - submit_work       │
                       │  - report_blocked    │
                       │  - request_clari     │
                       │                      │
              ┌────────┴────────┐    ┌────────┴───────┐
              │   SUBMITTED     │    │   BLOCKED      │
              └────────┬────────┘    │   (worker 状态) │
                       │             └────────┬───────┘
                       │                      │
              协调者 request_cleanup     协调者 cancel_task
                       │                  或重派给别人
                       ▼                      │
              ┌─────────────────┐             │
              │ ack_cleanup     │             ▼
              └────────┬────────┘    ┌────────────────┐
                       │             │   CANCELLED    │
                       ▼             └────────────────┘
              ┌─────────────────┐
              │     CLOSED      │
              └─────────────────┘

Worker 离线超过 90 秒且持有任务 → 任务转为 ABANDONED
Worker 主动 deregister → 持有任务转为 ABANDONED,worker 记录被删除
```

## 等待机制(Waiters)

`coop_server/waiters.py` 实现了若干 keyed 等待器:

- `task_notifications`(key: worker_id)—— Coordinator 派发任务时唤醒 Worker
- `coord_events`(key: 全局)—— Worker 提交或报错时唤醒 Coordinator
- `cleanup_requests`(key: task_id)—— Coordinator 请求清理时唤醒 Worker
- `clarification_answers`(key: task_id)—— Coordinator 答复时唤醒 Worker

实现基于 `asyncio.Queue` + 字典。client 调用 `wait_for_*` 时:

1. 先扫描 DB 检查是否已有数据(避免 race condition)
2. 没有则阻塞在对应的 `queue.get(timeout)` 上
3. 超时返回 no_event,由 client 自行重试

## 鉴权

- Bearer token(32 字符 hex),server 启动时自动生成
- 所有 MCP 调用均校验 `Authorization: Bearer <token>`
- token 文件权限 0600
- 实际部署时 token 通过共享网盘分发给 Worker

不采用 mTLS / OAuth 等方案是因为目标场景是**受信任的局域网**(同一公司或团队内),引入这类方案的复杂度不成比例。

## 服务发现

mDNS 广播 `_coop._tcp.local.` 服务,Worker 通过 zeroconf 自动发现 Coop Server 地址。底层使用 `zeroconf` 库,与 macOS 原生 mDNSResponder 兼容。

当跨子网或企业网封锁 mDNS 时,可在 Worker `client.yaml` 中显式配置 host 作为 fallback。

## 部署模式

### 单机开发

Server、Coordinator、Worker 全部运行在本机,用于开发调试。`client.yaml` 指向 127.0.0.1。

### 局域网协作

Server 部署在一台 macOS 上,其他机器安装 Worker CLI。通过共享网盘分发 token,mDNS 自动发现地址。

### Docker(Linux)

`docker-compose.yml` 用于启动 server。macOS 上 Docker 不支持 host network,mDNS 无法正常工作,需要手动配置 host;Linux 上 Docker 没有此限制。

## 关键代码位置

```
coop_server/
├── server.py            MCP server 主入口,工具注册
├── store.py             SQLite 数据访问层
├── db.py                连接管理(WAL,外键)
├── models.py            数据模型与状态枚举
├── waiters.py           长轮询等待机制
├── heartbeat.py         心跳监控与 OFFLINE 标记
├── auth.py              Bearer token
├── discovery.py         mDNS 广播
├── worker_tools.py      Worker 端 MCP 工具实现
├── coordinator_tools.py Coordinator 端 MCP 工具实现
└── __main__.py          启动入口

cli/
├── __main__.py          coop CLI 入口
├── config.py            客户端配置加载(多优先级)
├── mcp_call.py          短 / 长会话 MCP client
├── discovery.py         mDNS 客户端
└── simulate.py          SimulatedWorker / SimulatedCoordinator
                         (用于 smoke-test / stress-test 等)
```

## 关键设计决策记录

### 为什么不使用消息队列(Redis / RabbitMQ)

- 部署复杂度过高
- 单台 Mac 配合 SQLite 即可满足 3-5 个 Worker 的规模
- WAL 模式 SQLite 实测每秒可处理数百次工具调用
- MCP 协议本身即请求 / 响应模式,引入队列反而割裂语义

### 为什么 Worker 不上报能力

最初版本设计过此机制,后来发现:

- Worker 能力是动态的(checkout 不同分支后可执行的任务集合不同)
- 维护"能力声明"会偏离真实状态
- 反馈式派单(publish + report_blocked)能自然解决问题,且更简单

### 为什么 Coordinator 是 AI agent 而非 Web UI

- Coordinator 需要做高层判断(任务拆解、代码 review、改派决策),这是 LLM 的强项
- Web UI 仅能由人工手动派发,失去 AI 自主协作的意义
- 如需可视化界面,可单独提供 read-only dashboard,与 MCP server 解耦

### 为什么使用长轮询而非 SSE / WebSocket

- MCP 协议原生支持工具调用,长轮询可基于现有机制
- AI agent 调用 MCP 工具是原生交互方式,无需额外协议层
- SSE / WebSocket 需要额外的 client 实现,与 MCP 设计冲突
- 在目标规模下,长轮询性能足够(timeout 60 秒,每 Worker 每分钟一次)
