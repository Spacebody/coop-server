#!/usr/bin/env bash
# macOS 上 native 部署 Coop Server
#
# 做的事:
#  1. 创建 venv
#  2. 装 coop-server (从当前源码)
#  3. 创建数据目录
#  4. 生成配置文件 (如果不存在)
#  5. 安装 launchd plist
#  6. 启动服务
#
# 用法:
#   cd coop-server-final
#   ./deploy/macos/install.sh [--prefix /custom/path]

set -euo pipefail

# ===========================================================
# 默认参数 & 解析
# ===========================================================

# 默认装到当前目录下的 coop-server 子目录
# (可以用 --prefix 自定义,比如装到 ~/.local/coop-server)
PREFIX="$(pwd)/coop-server"
SOURCE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PORT=7777
USE_LAUNCHD=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --no-launchd)
            USE_LAUNCHD=false
            shift
            ;;
        -h|--help)
            cat <<EOF
用法: $0 [选项]

选项:
    --prefix PATH    安装目录 (默认: ./coop-server,即当前目录下的 coop-server 子目录)
    --port N         监听端口 (默认: 7777)
    --no-launchd     不安装 launchd 服务,只生成 venv/配置/启动脚本
    -h, --help       显示帮助

不带 --no-launchd 时会安装 launchd 服务实现开机自启 + 崩溃重启。
带 --no-launchd 时需要你手动启动 server (coopctl run-foreground 或
nohup coopctl start-bg)。
EOF
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 1
            ;;
    esac
done

# ===========================================================
# 检查环境
# ===========================================================

echo "==> 检查环境"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "错误: 此脚本仅适用于 macOS" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "错误: 找不到 python3。请先安装: brew install python@3.12" >&2
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

if [[ $PYTHON_MAJOR -lt 3 ]] || [[ $PYTHON_MAJOR -eq 3 && $PYTHON_MINOR -lt 11 ]]; then
    echo "错误: 需要 Python 3.11+,当前 $PYTHON_VERSION" >&2
    exit 1
fi
echo "    Python $PYTHON_VERSION ✓"

# ===========================================================
# 创建目录结构
# ===========================================================

echo "==> 创建目录: $PREFIX"
mkdir -p "$PREFIX"/{data,logs}

# ===========================================================
# 创建 venv
# ===========================================================

VENV="$PREFIX/.venv"
if [[ ! -d "$VENV" ]]; then
    echo "==> 创建 venv: $VENV"
    python3 -m venv "$VENV"
fi

PIP="$VENV/bin/pip"
PYTHON="$VENV/bin/python"

echo "==> 安装/更新依赖"
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -e "$SOURCE_DIR"

echo "    依赖已就绪 ✓"

# ===========================================================
# 配置文件
# ===========================================================

CONFIG="$PREFIX/config.yaml"
if [[ ! -f "$CONFIG" ]]; then
    echo "==> 生成配置文件: $CONFIG"
    cat > "$CONFIG" <<EOF
server:
  host: "0.0.0.0"
  port: $PORT
  transport: "streamable_http"

auth:
  enabled: true
  token_file: "$PREFIX/data/token"
  token_length: 32

database:
  path: "$PREFIX/data/coop.db"

heartbeat:
  worker_interval_sec: 30
  timeout_sec: 90
  check_interval_sec: 30

discovery:
  enabled: true
  service_type: "_coop._tcp.local."
  service_name: "coop-server"
  advertise_host: ""

logging:
  level: "INFO"
  file: "$PREFIX/logs/coop.log"
  rotate_max_bytes: 10485760
  rotate_backup_count: 5
EOF
    echo "    已写入 ✓"
else
    echo "==> 配置文件已存在,跳过: $CONFIG"
fi

# ===========================================================
# 客户端配置 (本机访问 server 用的 client.yaml)
# ===========================================================

CLIENT_DIR="$PREFIX/client"
CLIENT_CONFIG="$CLIENT_DIR/client.yaml"
CLIENT_TOKEN="$CLIENT_DIR/token"

mkdir -p "$CLIENT_DIR"

# token 软链接 (避免重复存,保持单一来源)
if [[ ! -L "$CLIENT_TOKEN" && ! -e "$CLIENT_TOKEN" ]]; then
    ln -s "$PREFIX/data/token" "$CLIENT_TOKEN"
    echo "==> 创建 token 软链接: $CLIENT_TOKEN -> $PREFIX/data/token"
fi

# client.yaml (指向本机 server)
if [[ ! -f "$CLIENT_CONFIG" ]]; then
    echo "==> 生成客户端配置: $CLIENT_CONFIG"
    cat > "$CLIENT_CONFIG" <<EOF
# 本机客户端配置 (跟 server 一起部署在 $PREFIX 下)
# 装在这里方便备份/迁移整个 PREFIX 一起带走

token_file: "$CLIENT_TOKEN"

coordinator:
  host: "127.0.0.1"
  port: $PORT
EOF
    echo "    已写入 ✓"
else
    echo "==> 客户端配置已存在,跳过: $CLIENT_CONFIG"
fi

# ===========================================================
# launchd plist (可选)
# ===========================================================

PLIST_NAME="com.coop.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LAUNCHD_LOG="$PREFIX/logs/launchd.out.log"
LAUNCHD_ERR="$PREFIX/logs/launchd.err.log"

if [[ "$USE_LAUNCHD" == "true" ]]; then
    echo "==> 生成 launchd plist: $PLIST_PATH"
    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>coop_server</string>
        <string>--config</string>
        <string>$CONFIG</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PREFIX</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>$LAUNCHD_LOG</string>

    <key>StandardErrorPath</key>
    <string>$LAUNCHD_ERR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$VENV/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF
else
    echo "==> 跳过 launchd plist (--no-launchd)"
fi

# ===========================================================
# 安装 coopctl 服务管理脚本
# ===========================================================

CTL="$PREFIX/coopctl"
echo "==> 安装服务管理脚本: $CTL"
cat > "$CTL" <<'CTLEOF'
#!/usr/bin/env bash
# coopctl - Coop Server 服务管理
#
# 同时支持两种模式:
#   - launchd 模式: 系统服务,开机自启 (有 plist)
#   - manual 模式: 手动启停 (没有 plist)
# 根据 plist 是否存在自动选择。

set -euo pipefail

PREFIX="__PREFIX__"
VENV_PYTHON="$PREFIX/.venv/bin/python"
CONFIG="$PREFIX/config.yaml"
PLIST_NAME="com.coop.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG="$PREFIX/logs/coop.log"
LAUNCHD_OUT="$PREFIX/logs/launchd.out.log"
LAUNCHD_ERR="$PREFIX/logs/launchd.err.log"
PIDFILE="$PREFIX/coop-server.pid"

# 检测当前模式
if [[ -f "$PLIST_PATH" ]]; then
    MODE="launchd"
else
    MODE="manual"
fi

# manual 模式下查 server PID
manual_pid() {
    if [[ -f "$PIDFILE" ]]; then
        local pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
        # PID 文件残留,清理
        rm -f "$PIDFILE"
    fi
    return 1
}

cmd="${1:-help}"
shift || true

case "$cmd" in
    start)
        if [[ "$MODE" == "launchd" ]]; then
            if launchctl list | grep -q "$PLIST_NAME"; then
                echo "服务已在运行"
            else
                launchctl load "$PLIST_PATH"
                echo "服务已启动 (launchd)"
            fi
        else
            # manual 模式: 后台启动
            if pid=$(manual_pid); then
                echo "服务已在运行 (PID: $pid)"
            else
                nohup "$VENV_PYTHON" -m coop_server --config "$CONFIG" \
                    > "$LAUNCHD_OUT" 2> "$LAUNCHD_ERR" &
                echo $! > "$PIDFILE"
                # 等启动稳定
                sleep 2
                if pid=$(manual_pid); then
                    echo "服务已启动 (PID: $pid, 后台运行)"
                else
                    echo "启动失败,查看日志: cat $LAUNCHD_ERR" >&2
                    rm -f "$PIDFILE"
                    exit 1
                fi
            fi
        fi
        ;;

    stop)
        if [[ "$MODE" == "launchd" ]]; then
            if launchctl list | grep -q "$PLIST_NAME"; then
                launchctl unload "$PLIST_PATH"
                echo "服务已停止"
            else
                echo "服务未运行"
            fi
        else
            if pid=$(manual_pid); then
                kill -TERM "$pid"
                # 等优雅退出
                for i in $(seq 1 10); do
                    if ! kill -0 "$pid" 2>/dev/null; then
                        rm -f "$PIDFILE"
                        echo "服务已停止"
                        exit 0
                    fi
                    sleep 0.5
                done
                # 还活着就 KILL
                kill -KILL "$pid" 2>/dev/null || true
                rm -f "$PIDFILE"
                echo "服务已强制停止"
            else
                echo "服务未运行"
            fi
        fi
        ;;

    restart)
        "$0" stop || true
        sleep 1
        "$0" start
        ;;

    run-foreground)
        # 不管 mode,都是前台跑 (调试用,Ctrl+C 退出)
        echo "前台模式启动 (Ctrl+C 退出)"
        exec "$VENV_PYTHON" -m coop_server --config "$CONFIG"
        ;;

    status)
        if [[ "$MODE" == "launchd" ]]; then
            echo "模式: launchd"
            if launchctl list | grep -q "$PLIST_NAME"; then
                line=$(launchctl list | grep "$PLIST_NAME")
                pid=$(echo "$line" | awk '{print $1}')
                exit_code=$(echo "$line" | awk '{print $2}')
                if [[ "$pid" == "-" ]]; then
                    echo "服务已加载但未运行 (上次退出码: $exit_code)"
                else
                    echo "服务运行中 (PID: $pid)"
                fi
            else
                echo "服务未加载"
            fi
        else
            echo "模式: manual"
            if pid=$(manual_pid); then
                echo "服务运行中 (PID: $pid)"
            else
                echo "服务未运行"
            fi
        fi

        if command -v lsof >/dev/null 2>&1; then
            port=$(grep "port:" "$CONFIG" | head -1 | awk '{print $2}')
            echo "配置端口: $port"
        fi
        ;;

    logs)
        # 默认 tail 主日志,加 -f 跟踪
        follow="${1:-}"
        if [[ "$follow" == "-f" ]]; then
            tail -f "$LOG"
        else
            tail -50 "$LOG"
        fi
        ;;

    launchd-logs)
        echo "=== stdout ==="
        tail -20 "$LAUNCHD_OUT" 2>/dev/null || echo "(空)"
        echo ""
        echo "=== stderr ==="
        tail -20 "$LAUNCHD_ERR" 2>/dev/null || echo "(空)"
        ;;

    token)
        cat "$PREFIX/data/token"
        ;;

    config)
        echo "$CONFIG"
        ;;

    reset)
        # 清空所有数据库内容(worker 注册/任务/事件),token 保留
        # 用于残留状态导致协议异常时的恢复
        force=""
        if [[ "${1:-}" == "--yes" ]]; then
            force="yes"
        fi

        if [[ "$force" != "yes" ]]; then
            echo "⚠️  这会清空以下数据 (无法恢复):"
            echo "    - 所有 worker 注册状态"
            echo "    - 所有任务 (含进行中/已提交/已完成)"
            echo "    - 所有事件历史"
            echo "    - 所有未答复的 clarification"
            echo ""
            echo "保留:"
            echo "    - token (worker 端不用重新配)"
            echo "    - 配置文件"
            echo "    - 日志"
            echo ""
            read -p "确认重置? (输入 yes 继续): " confirm
            if [[ "$confirm" != "yes" ]]; then
                echo "已取消"
                exit 0
            fi
        fi

        # 1. 停服务
        was_running=false
        if "$0" status 2>&1 | grep -q "服务运行中"; then
            was_running=true
            echo "==> 停止服务"
            "$0" stop > /dev/null
            sleep 1
        fi

        # 2. 删 DB 文件 (含 WAL 和 SHM)
        echo "==> 清空数据库"
        rm -f "$PREFIX/data/coop.db" \
              "$PREFIX/data/coop.db-wal" \
              "$PREFIX/data/coop.db-shm"
        echo "    已删除"

        # 3. 重启服务 (server 启动时会自动初始化新 DB)
        if [[ "$was_running" == "true" ]]; then
            echo "==> 启动服务"
            "$0" start
        else
            echo "==> 服务原本未运行,不自动启动"
            echo "    需要时跑: coopctl start"
        fi
        ;;

    help|*)
        cat <<HELP
coopctl - Coop Server 服务管理

用法: coopctl <命令>

通用命令:
    start            启动服务 (后台)
    stop             停止服务
    restart          重启
    status           查看运行状态
    run-foreground   前台启动 (调试用,Ctrl+C 退出)
    logs [-f]        查看主日志
    launchd-logs     查看 launchd/启动期 stdout/stderr
    token            打印 token (用于配置 worker)
    config           打印配置文件路径
    reset [--yes]    清空所有数据库状态后重启 (token 保留, --yes 跳过确认)
    help             显示此帮助

当前模式: $MODE
HELP
        ;;
esac
CTLEOF

# 替换占位符
sed -i.bak "s|__PREFIX__|$PREFIX|g" "$CTL" && rm "$CTL.bak"
chmod +x "$CTL"

# ===========================================================
# 创建 ~/bin 软链接 (可选,方便用户调用)
# ===========================================================

VENV_COOP="$VENV/bin/coop"

if [[ -d "$HOME/bin" ]]; then
    # coopctl
    if [[ -L "$HOME/bin/coopctl" ]] || [[ ! -e "$HOME/bin/coopctl" ]]; then
        ln -sf "$CTL" "$HOME/bin/coopctl"
        echo "    已创建软链接: $HOME/bin/coopctl"
    fi
    # coop CLI (用户直接敲 coop 就能用,不用激活 venv)
    if [[ -f "$VENV_COOP" ]]; then
        if [[ -L "$HOME/bin/coop" ]] || [[ ! -e "$HOME/bin/coop" ]]; then
            ln -sf "$VENV_COOP" "$HOME/bin/coop"
            echo "    已创建软链接: $HOME/bin/coop"
        fi
    fi
else
    echo "    提示: $HOME/bin 不存在,未创建全局软链接"
    echo "    可以手动加 alias 或自己 mkdir ~/bin (并加入 PATH)"
fi

# ===========================================================
# 启动
# ===========================================================

echo ""
echo "==> 启动服务"

if [[ "$USE_LAUNCHD" == "true" ]]; then
    # 如果已经加载过,先卸载
    if launchctl list | grep -q "$PLIST_NAME"; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    fi

    launchctl load "$PLIST_PATH"

    # 等几秒看是否真的起来了
    sleep 3

    if launchctl list | grep -q "$PLIST_NAME"; then
        line=$(launchctl list | grep "$PLIST_NAME")
        pid=$(echo "$line" | awk '{print $1}')
        if [[ "$pid" != "-" ]]; then
            echo "    服务运行中 (PID: $pid) ✓"
        else
            echo "    警告: 服务已加载但未运行,查看日志:" >&2
            echo "    cat $PREFIX/logs/launchd.err.log" >&2
            exit 1
        fi
    else
        echo "    错误: 服务未启动" >&2
        exit 1
    fi
else
    # manual 模式: 用 coopctl 后台启动
    echo "    (manual 模式,用 coopctl start 启动后台服务)"
    "$CTL" start
fi

# 健康检查
echo ""
echo "==> 健康检查"
sleep 1
if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "    /health 端点响应正常 ✓"
else
    echo "    /health 端点无响应,但服务可能仍在初始化"
    echo "    稍后用 coopctl status 检查"
fi

# ===========================================================
# 输出 token 和后续步骤
# ===========================================================

TOKEN_FILE="$PREFIX/data/token"

echo ""
echo "============================================================"
echo " 部署完成 ✓"
echo "============================================================"
echo ""
echo "服务管理:"
echo "  $CTL <命令>"
if [[ -L "$HOME/bin/coopctl" ]]; then
    echo "  也可直接: coopctl <命令>     (~/bin 已软链接)"
fi
echo ""
echo "CLI 工具 (coop 命令):"
if [[ -L "$HOME/bin/coop" ]]; then
    echo "  ~/bin/coop 已软链接,直接用: coop <子命令>"
else
    echo "  完整路径: $VENV/bin/coop"
    echo "  建议加 alias 到 ~/.zshrc:"
    echo "    echo 'alias coop=\"$VENV/bin/coop\"' >> ~/.zshrc"
    echo "    source ~/.zshrc"
fi
echo ""
echo "Token 文件: $TOKEN_FILE"
if [[ -f "$TOKEN_FILE" ]]; then
    TOKEN=$(cat "$TOKEN_FILE")
    echo "Token 内容: $TOKEN"
fi
echo ""
echo "接下来:"
echo "  1. 把 token 文件复制到共享网盘 (供其他机器作为 worker 接入):"
echo "     cp $TOKEN_FILE /path/to/your/shared-drive/coop-token"
echo ""
echo "  2. 本机客户端配置已自动生成在: $CLIENT_CONFIG"
echo "     coop CLI 会自动找到它(无需配置环境变量)。"
echo ""
echo "  3. 验证:"
if [[ -L "$HOME/bin/coop" ]]; then
    echo "     coop doctor"
else
    echo "     $VENV/bin/coop doctor"
fi
echo ""
