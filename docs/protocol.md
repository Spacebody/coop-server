# MCP 协议参考

完整的 MCP 工具列表、参数和返回值。所有工具走 Streamable HTTP transport,
endpoint `/mcp/`,鉴权用 `Authorization: Bearer <token>`。

## Worker 端工具 (10)

worker Claude 用这些工具。

### register_worker

worker 启动时声明上线。

**参数**:
- `worker_id`: str — 唯一标识(机器名 / 团队成员名)
- `hostname`: str — 主机名(诊断用)

**返回**: `{ok: true, worker: {...}}` 或 `{ok: false, error: "..."}`

### heartbeat

定期(默认 30s)调一次,声明仍存活。

**参数**:
- `worker_id`: str

**返回**: `{ok: true}` 或 `{ok: false, error: "worker 未注册"}`

### deregister_worker

主动下线。从 server 删除 worker 记录,持有的 in-progress 任务标 abandoned。

**参数**:
- `worker_id`: str

**返回**: `{ok: true}` 或 `{ok: false, error: "..."}`

### wait_for_task

长轮询等待任务派发。

**参数**:
- `worker_id`: str
- `timeout_sec`: int (默认 60, 最大 600)

**返回**:
- `{status: "assigned", task: {task_id, description, priority, ...}}` — 收到任务
- `{status: "no_task"}` — 超时无任务

### submit_work

任务做完,提交结果。

**参数**:
- `worker_id`: str
- `task_id`: str
- `project`: str — 工程逻辑名(协调者也认识的)
- `branch`: str — git 分支名
- `commit_sha`: str — 提交 hash
- `summary`: str — 简要说明完成的工作内容
- `extra`: dict (可选) — 附加信息(测试结果、审查要点等)

**返回**: `{ok: true}` 或 `{ok: false, error: "..."}`

### report_blocked

向 Coordinator 报告无法执行该任务。

**参数**:
- `worker_id`: str
- `task_id`: str
- `reason`: str — 自然语言解释

**返回**: `{ok: true}`

### request_clarification

任务描述不清,问协调者。

**参数**:
- `worker_id`: str
- `task_id`: str
- `question`: str

**返回**: `{ok: true}` (问题已记录,等 wait_for_clarification)

### wait_for_clarification

阻塞等协调者答复。

**参数**:
- `worker_id`: str
- `task_id`: str
- `timeout_sec`: int (默认 60)

**返回**:
- `{status: "answered", answer: "..."}`
- `{status: "no_answer"}` — 超时

### wait_for_cleanup_request

提交后阻塞等协调者请求清理(review 完了)。

**参数**:
- `worker_id`: str
- `task_id`: str
- `timeout_sec`: int (默认 60)

**返回**:
- `{status: "cleanup_requested"}`
- `{status: "no_request"}` — 超时

### ack_cleanup

收到清理指令,告诉 server 已经清理完(删 worktree 等)。

**参数**:
- `worker_id`: str
- `task_id`: str

**返回**: `{ok: true}` (任务转 closed)

## 协调者端工具 (9)

协调者 Claude 用这些工具。

### list_workers

查看 worker 列表。

**参数**:
- `online_only`: bool (默认 true) — 只看在线的(idle/working/blocked),false 也包括 OFFLINE

**返回**: `{ok: true, workers: [{worker_id, hostname, status, current_task_id, last_heartbeat, ...}]}`

### list_tasks

查看任务列表。

**参数**:
- `status`: str (可选) — 按状态过滤

**返回**: `{ok: true, tasks: [...]}`

### prune_offline_workers

物理删除所有 OFFLINE 的 worker 记录。

**参数**: 无

**返回**: `{ok: true, deleted_count: N}`

### publish_task

派任务给指定 worker。

**参数**:
- `task_id`: str — 唯一任务 ID(协调者自己起)
- `assignee`: str — worker_id
- `description`: str — 自然语言任务描述
- `priority`: str (默认 "normal") — high/normal/low
- `parent_task_id`: str (可选) — 子任务用
- `depends_on`: list[str] (可选) — 依赖的其他 task_id

**返回**:
- `{ok: true, task: {...}}`
- `{ok: false, error: "worker xxx 不存在"}`
- `{ok: false, error: "worker xxx 已失联 (OFFLINE)..."}` — 不能派给 OFFLINE worker

### cancel_task

取消任务。

**参数**:
- `task_id`: str
- `reason`: str (可选)

**返回**: `{ok: true}` 或 `{ok: false, error: "..."}`

### request_cleanup

通知 worker 可以清理工作区(review 完成,任务关闭)。

**参数**:
- `task_id`: str

**返回**: `{ok: true}` 或 `{ok: false, error: "任务状态不对(必须是 submitted)"}`

### wait_for_event

长轮询等待 worker 端事件。

**参数**:
- `timeout_sec`: int (默认 60)
- `event_types`: list[str] (可选) — 只关心某些类型

**返回**:
- `{status: "event", event: {type, task_id, worker_id, payload, ...}}`
- `{status: "no_event"}`

事件类型:
- `worker_registered` — 新 worker 上线
- `worker_offline` — worker 失联(心跳超时或主动 deregister)
- `work_submitted` — worker 提交了任务
- `worker_blocked` — worker 报 blocked
- `clarification_requested` — worker 问问题
- `cleanup_done` — worker ack 清理完成

### respond_clarification

回答 worker 的提问。

**参数**:
- `task_id`: str
- `answer`: str

**返回**: `{ok: true}` 或 `{ok: false, error: "..."}`

### get_clarifications

查看任务的提问历史。

**参数**:
- `task_id`: str

**返回**: `{ok: true, clarifications: [{question, answer, asked_at, answered_at}, ...]}`

## 错误响应

所有工具失败时返回 `{ok: false, error: "中文描述"}`,不抛异常。错误类型:

- `worker xxx 不存在` — assignee 不在 server 记录里
- `worker xxx 已失联 (OFFLINE)` — worker 心跳超时
- `task_id 重复` — publish_task 用了已存在的 task_id
- `任务状态不对` — 比如对未 submit 的任务 request_cleanup
- `worker 未注册` — heartbeat 之前没 register

## 长轮询超时

server 端 `MAX_TIMEOUT_SEC = 600`。client 传 `timeout_sec` 超过这个值会被截断。

实际部署建议 worker / 协调者用 60-180 秒, 太长会让 HTTP keep-alive 出问题。

## 一致性保证

- 所有写操作单事务原子 (worker 状态变更 + 事件插入 + 通知 waiter 在一个事务里)
- SQLite WAL 模式,读不阻塞写
- waiter 唤醒是 best-effort, 真实状态以 DB 为准 (client 被唤醒后必须重读 DB)

## 事件投递语义

至多一次 (at-most-once):
- 事件落 DB 后才通知 waiter
- 但 waiter 阻塞过久 / 客户端断开会丢通知
- 所以 client 应该:
  - 长轮询超时后立刻重试
  - 重新连接后调 `wait_for_event` 会自然拿到未处理的历史事件
  
事件不会被重复消费(每个 wait_for_event 拿到的事件 server 会标 consumed)。
