# 运维手册

按部署方式不同操作命令不同，但底层数据格式一致，可以互相迁移。

## 关于部署路径

下面所有命令都用 `$PREFIX` 代指部署目录。**默认部署到执行 install.sh 时所在目录的 `coop-server` 子目录**，比如：

```bash
cd ~/work
./coop-server-final/deploy/macos/install.sh
# 装到 ~/work/coop-server/

# 或者用 --prefix 自定义
./coop-server-final/deploy/macos/install.sh --prefix ~/somewhere/coop
# 装到 ~/somewhere/coop/
```

后面的命令例子里出现的 `~/.local/coop-server` 是**示意路径**——你需要替换成自己的实际部署路径。

## macOS Native 部署 (推荐)

### 目录结构

部署后 `$PREFIX` 目录下：

```
$PREFIX/                          ← 比如 ~/work/coop-server 或自定义路径
├── .venv/                        ← Python 虚拟环境
├── config.yaml                   ← 配置文件
├── coopctl                       ← 服务管理脚本
├── data/
│   ├── coop.db                   ← SQLite 数据库
│   ├── coop.db-wal               ← WAL 临时文件
│   ├── coop.db-shm               ← 共享内存索引
│   └── token                     ← 鉴权 token (0600)
└── logs/
    ├── coop.log                  ← 应用主日志 (滚动)
    ├── coop.log.1                ← 历史日志
    ├── launchd.out.log           ← launchd 标准输出
    └── launchd.err.log           ← launchd 标准错误

~/Library/LaunchAgents/
└── com.coop.server.plist         ← launchd 配置 (用 launchd 模式时才有)
```

### 服务管理 (coopctl)

```bash
coopctl start          # 启动服务
coopctl stop           # 停止服务
coopctl restart        # 重启
coopctl status         # 查看运行状态 (PID/端口)
coopctl logs           # 看主日志最近 50 行
coopctl logs -f        # 跟踪日志
coopctl launchd-logs   # 看 launchd 输出 (排查启动失败)
coopctl token          # 打印 token (用于分发给 worker)
coopctl config         # 打印配置文件路径
```

`coopctl` 在部署目录里 (`~/.local/coop-server/coopctl`)，如果 `~/bin` 在 PATH 中，安装脚本会自动建软链接。

### 开机自启

`launchd` plist 里 `RunAtLoad` 已设为 true，登录后自动启动。`KeepAlive` 在异常退出时自动重启。

如果想临时禁用开机启动：

```bash
launchctl unload ~/Library/LaunchAgents/com.coop.server.plist
# 后续要恢复:
launchctl load ~/Library/LaunchAgents/com.coop.server.plist
```

### 配置改动

```bash
# 编辑配置
vim ~/.local/coop-server/config.yaml

# 重启生效
coopctl restart
```

### 升级

```bash
# 1. 备份数据
cp -r ~/.local/coop-server/data ~/coop-backup-$(date +%Y%m%d)

# 2. 解压新版源码
tar -xzf coop-server-vNEW.tar.gz
cd coop-server-final

# 3. 跑一键部署脚本 (会复用现有 config 和 data,但更新代码)
./deploy/macos/install.sh

# 脚本会:
#   - 复用现有 venv (升级依赖)
#   - 不覆盖现有 config.yaml
#   - 不动 data/ 目录
#   - 重新加载 launchd
```

### 卸载

```bash
# 仅移除服务,数据保留
./deploy/macos/uninstall.sh

# 彻底删除 (含 token 和 DB!)
./deploy/macos/uninstall.sh --purge
```

---

## Docker 部署 (可选)

如果是 Linux 服务器，Docker 部署更标准。macOS 上不推荐用 Docker，因为不支持 host network 模式，mDNS 不工作。

### 启动 / 停止 / 重启

```bash
docker compose up -d
docker compose down
docker compose restart
docker compose logs -f
docker compose ps
```

### 进容器排查

```bash
docker compose exec coop-server sh
```

### 升级

```bash
docker compose stop
tar -czf coop-data-$(date +%Y%m%d).tar.gz data/
git pull   # 或解压新版
docker compose build
docker compose up -d
docker compose logs --tail=50 | grep schema
```

---

## 通用操作 (与部署方式无关)

### 命令行查询

不进容器或服务也能通过 `coop` CLI 查：

```bash
coop list-workers    # 看在线 worker
coop list-tasks --status submitted    # 待 review 的任务
coop ping            # 验证连接
```

### 备份

数据全在 `data/` 目录(macOS native) 或 `./data/` (docker)。

**冷备(简单)**:

```bash
# macOS
coopctl stop
tar -czf ~/coop-data-$(date +%Y%m%d).tar.gz -C ~/.local/coop-server data/
coopctl start

# Docker
docker compose stop
tar -czf coop-data-$(date +%Y%m%d).tar.gz data/
docker compose start
```

**热备 (推荐)**: 用 SQLite 自带的 .backup 命令做一致性备份,无需停服:

```bash
# macOS native
PREFIX=~/.local/coop-server
$PREFIX/.venv/bin/python -c "
import sqlite3
src = sqlite3.connect('$PREFIX/data/coop.db')
dst = sqlite3.connect('$HOME/coop-backup-$(date +%Y%m%d).db')
src.backup(dst)
print('done')
"
```

建议 cron 每天凌晨备份，保留 7 天。

### 监控

简单监控 (每分钟):

```bash
# /etc/cron.d/coop-monitor (Linux) 或 launchd plist (macOS)
* * * * * curl -fs http://localhost:7777/health > /dev/null \
    || echo "coop down" | mail -s alert ops@team
```

关键日志关键词：

```bash
# 心跳告警
grep "标记.*worker offline" ~/.local/coop-server/logs/coop.log

# 鉴权失败 (安全事件)
grep 鉴权失败 ~/.local/coop-server/logs/coop.log

# 严重错误
grep ERROR ~/.local/coop-server/logs/coop.log
```

### Token 轮换

**macOS native:**

```bash
coopctl stop
rm ~/.local/coop-server/data/token
coopctl start
# 新 token 自动生成
cp ~/.local/coop-server/data/token /shared-drive/coop-token
# 等网盘同步,worker 那边的 client.yaml 不用动
```

**Docker:**

```bash
docker compose stop
rm data/token
docker compose up -d
cp data/token /shared-drive/coop-token
```

通知 worker 团队重启 Claude Code (让 MCP client 用新 token 重连)。

### 数据库健康

```bash
# macOS native
PYTHON=~/.local/coop-server/.venv/bin/python
DB=~/.local/coop-server/data/coop.db

# 看任务统计
$PYTHON -c "
import sqlite3
conn = sqlite3.connect('$DB')
for row in conn.execute('SELECT status, COUNT(*) FROM tasks GROUP BY status'):
    print(row)
"

# 看 worker 数
$PYTHON -c "
import sqlite3
conn = sqlite3.connect('$DB')
for row in conn.execute('SELECT status, COUNT(*) FROM workers GROUP BY status'):
    print(row)
"

# 看积压未消费事件
$PYTHON -c "
import sqlite3
conn = sqlite3.connect('$DB')
print(conn.execute('SELECT COUNT(*) FROM events WHERE consumed=0').fetchone())
"
```

事件长期不消费可能意味着协调者死了或者断连，要排查。

### 数据清理

```bash
PYTHON=~/.local/coop-server/.venv/bin/python
DB=~/.local/coop-server/data/coop.db

# 清理 30 天前的已关闭/已取消任务
$PYTHON -c "
import sqlite3
from datetime import datetime, timedelta, timezone
threshold = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
conn = sqlite3.connect('$DB')
n = conn.execute('''
    DELETE FROM tasks
    WHERE status IN (\"closed\", \"cancelled\", \"abandoned\")
    AND dispatched_at < ?
''', (threshold,)).rowcount
conn.commit()
print(f'清理了 {n} 个旧任务')
"

# 清理已消费事件
$PYTHON -c "
import sqlite3
from datetime import datetime, timedelta, timezone
threshold = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
conn = sqlite3.connect('$DB')
n = conn.execute('''
    DELETE FROM events WHERE consumed=1 AND created_at < ?
''', (threshold,)).rowcount
conn.commit()
conn.execute('VACUUM')
print(f'清理了 {n} 个旧事件')
"
```

可以做成 cron 每周跑一次。
