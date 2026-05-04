#!/usr/bin/env bash
# 卸载 macOS 上的 Coop Worker 客户端
#
# 默认只删除 ~/bin/coop 软链接 (如果是这个 PREFIX 装的),
# 部署目录保留。加 --purge 才删除整个部署目录。

set -euo pipefail

PREFIX="$(pwd)/coop-worker"
PURGE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --purge) PURGE=true; shift ;;
        -h|--help)
            cat <<EOF
用法: $0 [选项]

选项:
    --prefix PATH    部署目录 (默认: ./coop-worker)
    --purge          删除整个部署目录 (含 client.yaml,venv)
    -h, --help       显示帮助

不加 --purge 只删除 ~/bin/coop 软链接,部署目录保留。
EOF
            exit 0
            ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

# 规范化绝对路径,便于跟软链接 target 对比
PREFIX_ABS=$(cd "$PREFIX" 2>/dev/null && pwd) || PREFIX_ABS="$PREFIX"
VENV_COOP_ABS="$PREFIX_ABS/.venv/bin/coop"

# ===========================================================
# 1. 删除 ~/bin/coop 软链接 (仅当指向当前 PREFIX 时)
# ===========================================================

if [[ -L "$HOME/bin/coop" ]]; then
    LINK_TARGET=$(readlink "$HOME/bin/coop")
    if [[ "$LINK_TARGET" == "$VENV_COOP_ABS" ]]; then
        echo "==> 删除软链接: $HOME/bin/coop -> $LINK_TARGET"
        rm "$HOME/bin/coop"
    else
        echo "==> 跳过软链接 $HOME/bin/coop"
        echo "    它指向 $LINK_TARGET (不是当前 PREFIX 装的,可能是 server 端)"
    fi
fi

# ===========================================================
# 2. 数据
# ===========================================================

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
        echo "Worker 已卸载。部署目录保留: $PREFIX"
        echo "  - client.yaml 和 venv 都还在"
        echo "如要彻底删除,重新运行加 --purge"
    fi
fi
