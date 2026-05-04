# 故障排查

> **关于 AI agent**: 本文档的 agent 相关排查(MCP 工具调用、心跳、任务接收等)
> 主要基于 **Claude Code** 的行为。其他 agent (Codex CLI、Gemini CLI) 也支持
> Coop, 但它们的工具调用模式略有不同, 出现问题时排查方式会有差异。
> Coop Server 协议层是统一的, server 端日志和 `coop doctor` / `coop smoke-test`
> 等通用工具对所有 agent 都有效。

## 通用排查步骤

遇到问题时按以下顺序排查:

```bash
# 1. 验证 server 是否运行
coop ping --host <COOP_SERVER_IP> --token-file ~/.coop/token

# 2. 全面诊断
coop doctor

# 3. 查看 server 日志
ssh <Coop Server 所在机器>
docker compose -f /path/to/coop-server/docker-compose.yml logs --tail=100

# 4. 查看 worker 状态
coop list-workers

# 5. 查看任务状态
coop list-tasks
```

## Worker 未注册成功

**症状**:worker 启动后,`coop list-workers` 看不到该实例。

**可能原因 1**:MCP 未连接

在 Claude Code 中输入 `/mcp` 命令查看 MCP server 状态。

- `connected` ✓ → 配置正常,问题在 persona / Claude 行为,跳至原因 2
- `failed to connect` → 网络或地址问题
- `No MCP servers configured` → `.mcp.json` 未被识别

修复:
```bash
# 重新生成 .mcp.json
cd ~/coop-worker
coop setup-mcp --dir .
```

**可能原因 2**:Claude 未主动调用 register_worker

CLAUDE.md 是项目说明，Claude Code 启动时不会自动"执行"它，需要触发。

修复：在 worker Claude 终端里直接说：
```
按 CLAUDE.md 执行启动动作: 读 projects.json, 调 register_worker, 进入 wait_for_task 循环
```

或更直接：
```
立即调用 mcp__coop__register_worker, worker_id="worker-A", hostname="$(hostname)"
然后调用 mcp__coop__wait_for_task
```

**可能原因 3**:token 错误

server 日志中可见 `鉴权失败: 来自 ip-of-worker`。需要重新分发 token 文件。

## Worker 收到任务但未执行

**症状**:Coordinator 派发任务后,server 日志显示 `任务 X 派给 worker-A`,但 worker 似乎收到任务后无动作。

修复:输入 `/mcp` 确认连接正常,然后向 worker 明确指示:
```
你现在收到任务了, 按 CLAUDE.md 流程: 解析 description, 定位工程路径, 创建 worktree, 执行任务, 调用 submit_work
```

## Clarification 答复后 worker 仍在等待

**症状**:Coordinator 调用 `respond_clarification` 后,server 日志显示已答复,但 worker 仍阻塞在 `wait_for_clarification`。

**可能原因**:worker 未调用 `wait_for_clarification`,使用了其他方式等待。

修复:向 worker 明确指示:
```
你 request_clarification 之后必须立即调 wait_for_clarification(task_id) 长轮询等答复
```

## Server 启动失败

### 问题:Permission denied 写入 /data/

```
PermissionError: [Errno 13] Permission denied: '/data/coop.db'
```

修复:调整宿主机 data 目录权限。Docker 中以 uid 10000 运行:
```bash
mkdir -p data
sudo chown 10000:10000 data
```

或使用 root 运行(不推荐):编辑 Dockerfile 删除 `USER coop` 行。

### 问题:Port 7777 被占用

```
[Errno 98] Address already in use
```

修复:
```bash
# 查看占用进程
sudo lsof -iTCP:7777 -sTCP:LISTEN

# 修改端口
vim config.yaml  # server.port: 8888
docker compose down
docker compose up -d
```

### 问题:mDNS 不工作

`coop discover` 无法找到 Coop Server。

**原因 1**:Docker 未使用 host network

确认 docker-compose.yml 中有 `network_mode: host`。若使用 bridge 模式,mDNS 广播无法到达局域网。

**原因 2**:防火墙阻挡 mDNS

mDNS 使用 UDP 5353 端口,多播地址为 224.0.0.251。

```bash
# macOS
sudo pfctl -sa | grep 5353  # 查看防火墙规则

# Linux
sudo ufw allow 5353/udp
```

**原因 3**:跨网段

mDNS 不跨路由器(设计如此)。若 Coordinator 与 worker 不在同一子网,必须通过 `--host` 手动指定。

### 问题:心跳监控误报 worker offline

`docker compose logs` 中出现 `worker xxx 心跳超时`,但 worker 实际仍在运行。

**可能原因**:Claude Code 未调用 `heartbeat` 工具。Claude 可能认为"等待任务期间无需心跳"。

修复:在 worker persona 中强调心跳的必要性:
```
每隔 30 秒调一次 heartbeat 工具,执行任务时也不要遗漏。
长任务时如果你没办法定时调,先调一次 heartbeat 再开始,或者把任务拆小。
```

也可以临时调高 `heartbeat.timeout_sec`(如 300 秒)以提供更宽松的超时窗口。

## 任务状态异常

### Task 卡在 PENDING

worker 未拉取。可能原因:
- worker 不在线(通过 `coop list-workers` 验证)
- worker_id 拼写错误(任务的 assignee 与 worker 注册的 ID 不匹配)

修复:取消任务,使用正确的 worker_id 重新派发。

### Task 卡在 IN_PROGRESS

worker 已接收任务但未汇报。可能是 worker 崩溃但尚未被心跳检测到(超时窗口未到)。

修复:等待心跳超时后自动转为 abandoned 状态,或由 Coordinator 主动 `cancel_task` 后重新派发。

### Task 卡在 SUBMITTED

worker 已提交,但 Coordinator 尚未 review。

修复:确认 Coordinator 端的 Claude 实例是否仍在运行。若已断开或卡住,重启 Coordinator Claude 以重新调用 `wait_for_event`。

### Task 卡在 CLEANING

Coordinator 已发出清理指令,worker 未确认。可能 worker 在清理过程中卡住或断开。

修复:手动登录 worker 机器执行 `git worktree remove`,然后让 worker Claude 调用 `acknowledge_cleanup`;或者直接修改 server 端 DB:

```bash
docker compose exec coop-server python -c "
import sqlite3
conn = sqlite3.connect('/data/coop.db')
conn.execute('UPDATE tasks SET status=\"closed\" WHERE task_id=\"T-XXX\"')
conn.commit()
"
```

## 性能问题

### Worker 数量增加后派单变慢

3-5 个 worker 的规模下不应出现性能问题。若发生,排查方向:

- SQLite 文件是否位于网络盘上(应位于本地 SSD)
- WAL 文件是否定期执行 checkpoint:

```bash
docker compose exec coop-server python -c "
import sqlite3
conn = sqlite3.connect('/data/coop.db')
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
print('done')
"
```

## 阅读 server 日志

```
2026-05-03 07:30:46 INFO [__main__] Coop Server 启动
2026-05-03 07:30:46 INFO [coop_server.auth] 已生成新 token
2026-05-03 07:30:46 INFO [coop_server.db] DB schema 已就绪 (version=1)
2026-05-03 07:30:50 INFO [coop_server.heartbeat] 心跳监控启动
2026-05-03 07:30:50 INFO [coop_server.discovery] mDNS 广播已启动
2026-05-03 07:30:50 INFO [coop_server.server] Coop Server 全部组件启动完成

2026-05-03 07:31:13 INFO [coop_server.store] worker 上线: worker-A@host-A
2026-05-03 07:31:13 INFO [coop_server.coordinator_tools] 派发 T-001 -> worker-A (priority=normal)
2026-05-03 07:31:13 INFO [coop_server.worker_tools] worker-A 拿到任务 T-001 (即时)
2026-05-03 07:31:13 INFO [coop_server.worker_tools] worker-A 提交了 T-001
2026-05-03 07:31:13 INFO [coop_server.coordinator_tools] 请求清理 T-001
2026-05-03 07:31:13 INFO [coop_server.worker_tools] worker-A 已清理 T-001
```

正常工作流的日志形态如上。任何 ERROR 或 WARNING 都需要关注。

## 反馈与支持

若以上方案均无法解决问题,请收集以下信息后提交 issue:

```bash
# 系统信息
docker compose version
docker compose ps

# 配置(注意脱敏 token)
cat config.yaml
cat docker-compose.yml

# 最近 200 行日志
docker compose logs --tail=200 > /tmp/coop-logs.txt

# DB 状态
docker compose exec coop-server python -c "
import sqlite3
conn = sqlite3.connect('/data/coop.db')
print('workers:', conn.execute('SELECT COUNT(*) FROM workers').fetchone())
print('tasks by status:')
for row in conn.execute('SELECT status, COUNT(*) FROM tasks GROUP BY status'):
    print(' ', row)
print('unconsumed events:', conn.execute('SELECT COUNT(*) FROM events WHERE consumed=0').fetchone())
"
```
