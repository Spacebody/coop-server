#!/usr/bin/env bash
# 在 macOS 上把 coop CLI 装为 worker 客户端
#
# 跟 server 端 install.sh 不同:
#   - 不部署 server, 只装 coop CLI
#   - 询问/接受 token_file 路径和Coop Server 地址,生成本机 client.yaml
#
# 用法:
#   交互式 (默认):
#     ./deploy/macos/install-worker.sh
#
#   非交互式 (CI/脚本):
#     ./deploy/macos/install-worker.sh \
#         --token-file /path/to/token \
#         --host 192.168.1.42 \
#         [--port 7777] [--prefix ./coop-worker]

set -euo pipefail

# ===========================================================
# 默认参数 & 解析
# ===========================================================

PREFIX="$(pwd)/coop-worker"
SOURCE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TOKEN_FILE=""
HOST=""
PORT="7777"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --token-file)
            TOKEN_FILE="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        -h|--help)
            cat <<EOF
用法: $0 [选项]

选项:
    --prefix PATH        安装目录 (默认: ./coop-worker)
    --token-file PATH    token 文件路径 (本机能访问到的位置,通常是共享网盘)
    --host IP            Coop Server IP 地址 (server 部署机器的局域网 IP)
    --port N             Coop Server 端口 (默认 7777)
    -h, --help           显示帮助

如果不传 --token-file / --host, 会进入交互模式询问用户。
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

PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

if [[ $PYTHON_MAJOR -lt 3 ]] || [[ $PYTHON_MAJOR -eq 3 && $PYTHON_MINOR -lt 11 ]]; then
    echo "错误: 需要 Python 3.11+,当前 $PYTHON_MAJOR.$PYTHON_MINOR" >&2
    exit 1
fi
echo "    Python $PYTHON_MAJOR.$PYTHON_MINOR ✓"

# ===========================================================
# 交互式询问 (如果命令行没传)
# ===========================================================

if [[ -z "$TOKEN_FILE" ]]; then
    echo ""
    echo "需要 token 文件路径 (server 机器分发到共享网盘的 token)"
    echo "示例: /Users/${USER:-yourname}/CompanyDrive/coop/token"
    while true; do
        read -p "token 文件完整路径: " TOKEN_FILE
        TOKEN_FILE=$(echo "$TOKEN_FILE" | sed "s|^~|$HOME|")
        if [[ -z "$TOKEN_FILE" ]]; then
            echo "  路径不能为空" >&2
            continue
        fi
        if [[ ! -f "$TOKEN_FILE" ]]; then
            echo "  ⚠ 文件不存在: $TOKEN_FILE"
            read -p "  确定继续吗? (token 文件可能稍后才能同步过来) [y/N]: " confirm
            case "$confirm" in
                [yY]*) break ;;
                *) continue ;;
            esac
        else
            break
        fi
    done
fi

if [[ -z "$HOST" ]]; then
    echo ""
    echo "需要Coop Server 的地址"
    echo "示例: 192.168.50.252  (server 机器的局域网 IP)"
    echo "      留空使用 mDNS 自动发现"
    read -p "Coop Server IP (留空跳过): " HOST || HOST=""
fi

if [[ -z "$PORT" ]]; then
    PORT="7777"
fi

# 提前展示用户输入,让他确认
echo ""
echo "==> 配置确认"
echo "    安装目录: $PREFIX"
echo "    Token 文件: $TOKEN_FILE"
if [[ -n "$HOST" ]]; then
    echo "    Coop Server: $HOST:$PORT"
else
    echo "    Coop Server: (mDNS 自动发现,需 server 在同一局域网)"
fi

# ===========================================================
# 创建目录 + venv
# ===========================================================

echo ""
echo "==> 创建目录: $PREFIX"
mkdir -p "$PREFIX"

VENV="$PREFIX/.venv"
if [[ ! -d "$VENV" ]]; then
    echo "==> 创建 venv: $VENV"
    python3 -m venv "$VENV"
fi

PIP="$VENV/bin/pip"

echo "==> 安装/更新 coop CLI"
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -e "$SOURCE_DIR"
echo "    依赖已就绪 ✓"

# ===========================================================
# 生成 client.yaml
# ===========================================================

CLIENT_CONFIG="$PREFIX/client.yaml"

WRITE_CONFIG=true
if [[ -f "$CLIENT_CONFIG" ]]; then
    echo "==> 客户端配置已存在: $CLIENT_CONFIG"
    read -p "    是否覆盖? [y/N]: " overwrite
    case "$overwrite" in
        [yY]*) ;;  # 继续覆盖
        *) echo "    保留现有配置" ; WRITE_CONFIG=false ;;
    esac
fi

if [[ "$WRITE_CONFIG" == "true" ]]; then
    echo "==> 生成客户端配置: $CLIENT_CONFIG"
    {
        echo "# Coop worker 客户端配置 (跟 venv 一起部署在 $PREFIX 下)"
        echo ""
        echo "token_file: \"$TOKEN_FILE\""
        echo ""
        if [[ -n "$HOST" ]]; then
            echo "coordinator:"
            echo "  host: \"$HOST\""
            echo "  port: $PORT"
        else
            echo "# 留空 coordinator 块,由 mDNS 自动发现 Coop Server"
            echo "# coordinator:"
            echo "#   host: \"192.168.1.42\""
            echo "#   port: 7777"
        fi
    } > "$CLIENT_CONFIG"
    echo "    已写入 ✓"
fi

# ===========================================================
# ~/bin 软链接 (可选,方便用户调用)
# ===========================================================

VENV_COOP="$VENV/bin/coop"

if [[ -d "$HOME/bin" ]]; then
    if [[ -L "$HOME/bin/coop" ]] || [[ ! -e "$HOME/bin/coop" ]]; then
        ln -sf "$VENV_COOP" "$HOME/bin/coop"
        echo "==> 已创建软链接: $HOME/bin/coop"
    fi
else
    echo "==> 提示: $HOME/bin 不存在,未创建全局软链接"
fi

# ===========================================================
# 验证 token 文件可读
# ===========================================================

echo ""
echo "==> 验证 token 文件可读"
if [[ -f "$TOKEN_FILE" ]]; then
    TOKEN_LEN=$(wc -c < "$TOKEN_FILE" | tr -d ' ')
    echo "    ✓ token 已加载 (长度 $TOKEN_LEN)"
else
    echo "    ⚠ token 文件还不存在,等共享网盘同步后再试"
fi

# ===========================================================
# 输出后续步骤
# ===========================================================

echo ""
echo "============================================================"
echo " Worker 部署完成 ✓"
echo "============================================================"
echo ""
echo "CLI 工具:"
if [[ -L "$HOME/bin/coop" ]]; then
    COOP_CMD="coop"
    echo "  ~/bin/coop 已软链接,直接用: coop <子命令>"
else
    COOP_CMD="$VENV_COOP"
    echo "  完整路径: $VENV_COOP"
    echo "  建议加 alias: echo 'alias coop=\"$VENV_COOP\"' >> ~/.zshrc"
fi
echo ""
echo "客户端配置: $CLIENT_CONFIG"
echo ""
echo "接下来:"
echo "  1. 验证连通性:"
echo "     $COOP_CMD doctor"
echo ""
echo "  2. 跑通信测试:"
echo "     $COOP_CMD smoke-test"
echo ""
