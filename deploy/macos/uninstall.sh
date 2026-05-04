#!/usr/bin/env bash
# 卸载 macOS 上的 Coop Server
#
# 默认只移除 launchd 服务,保留数据。加 --purge 才删数据。

set -euo pipefail

PREFIX="$(pwd)/coop-server"
PURGE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --purge) PURGE=true; shift ;;
        -h|--help)
            cat <<EOF
用法: $0 [选项]

选项:
    --prefix PATH    安装目录 (默认: ./coop-server)
    --purge          也删除数据目录 (含 token 和 DB,谨慎使用!)

不加 --purge 只移除服务,数据保留。
EOF
            exit 0
            ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

PLIST_NAME="com.coop.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
PIDFILE="$PREFIX/coop-server.pid"

# 1. 停止 launchd 服务 (如有)
if launchctl list 2>/dev/null | grep -q "$PLIST_NAME"; then
    echo "==> 停止 launchd 服务"
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

# 2. 停止 manual 模式后台进程 (如有)
if [[ -f "$PIDFILE" ]]; then
    pid=$(cat "$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
        echo "==> 停止后台进程 (PID: $pid)"
        kill -TERM "$pid" 2>/dev/null || true
        # 等优雅退出
        for i in 1 2 3 4 5; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
fi

# 3. 删除 plist
if [[ -f "$PLIST_PATH" ]]; then
    echo "==> 删除 launchd plist: $PLIST_PATH"
    rm "$PLIST_PATH"
fi

# 4. 删除软链接
if [[ -L "$HOME/bin/coopctl" ]]; then
    echo "==> 删除软链接: $HOME/bin/coopctl"
    rm "$HOME/bin/coopctl"
fi
if [[ -L "$HOME/bin/coop" ]]; then
    echo "==> 删除软链接: $HOME/bin/coop"
    rm "$HOME/bin/coop"
fi

# 5. 清理 shell rc 里 install.sh 写的 COOP_PREFIX 块
COOP_MARK_BEGIN="# >>> coop-server (managed by install.sh) >>>"
COOP_MARK_END="# <<< coop-server (managed by install.sh) <<<"
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    if [[ -f "$rc" ]] && grep -qF "$COOP_MARK_BEGIN" "$rc"; then
        echo "==> 清理 $rc 中的 COOP_PREFIX 配置"
        awk -v b="$COOP_MARK_BEGIN" -v e="$COOP_MARK_END" '
            $0 == b { skip=1; next }
            $0 == e { skip=0; next }
            !skip
        ' "$rc" > "$rc.tmp" && mv "$rc.tmp" "$rc"
    fi
done

# 6. 数据
if [[ "$PURGE" == "true" ]]; then
    if [[ -d "$PREFIX" ]]; then
        echo "==> 删除目录: $PREFIX"
        rm -rf "$PREFIX"
    fi
    echo ""
    echo "完全卸载 ✓"
else
    if [[ -d "$PREFIX" ]]; then
        echo ""
        echo "服务已卸载。数据目录保留: $PREFIX"
        echo "如要彻底删除数据 (含 token 和 DB),重新运行加 --purge"
    fi
fi
