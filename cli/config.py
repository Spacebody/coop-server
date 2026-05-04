"""客户端配置加载。

有三种配置来源,按优先级递减:

1. 命令行参数(--host, --port, --token-file, --token)
2. 环境变量(COOP_HOST, COOP_PORT, COOP_TOKEN_FILE)
3. 客户端配置文件(~/.coop/client.yaml,推荐)

配置文件示例 ~/.coop/client.yaml:
    token_file: "/Users/alice/Documents/CompanyDrive/coop/token"
    coordinator:
      host: "coop.lan"
      port: 7777

token 加载多了一个 fallback 层 (4. 默认查找路径),为了向后兼容已有部署。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 环境变量名
ENV_TOKEN_FILE = "COOP_TOKEN_FILE"
ENV_HOST = "COOP_HOST"
ENV_PORT = "COOP_PORT"
ENV_PREFIX = "COOP_PREFIX"
ENV_CONFIG = "COOP_CONFIG"


def _detect_client_config_from_executable() -> Path | None:
    """从 sys.executable 反推本机 client.yaml 路径。

    支持两种部署布局:
        server 端 (install.sh):    $PREFIX/.venv/bin/python -> $PREFIX/client/client.yaml
        worker 端 (install-worker): $PREFIX/.venv/bin/python -> $PREFIX/client.yaml

    用候选路径里有没有 client.yaml 文件来确认是哪种,避免误判。
    返回 None 表示没找到,caller 应该回退到环境变量或老路径。
    """
    import sys
    try:
        # 不要 resolve! venv 里的 python 通常是 symlink 到系统 python,
        # resolve 后会跳到 /usr/bin/... 反推就错了。
        exe = Path(sys.executable).absolute()
        if len(exe.parents) < 3:
            return None
        prefix = exe.parents[2]   # .venv/bin/python -> 上 3 级
        # server 布局: $PREFIX/client/client.yaml
        server_layout = prefix / "client" / "client.yaml"
        if server_layout.exists():
            return server_layout
        # worker 布局: $PREFIX/client.yaml
        worker_layout = prefix / "client.yaml"
        if worker_layout.exists():
            return worker_layout
    except (OSError, ValueError):
        pass
    return None


def _client_config_paths() -> list[Path]:
    """配置文件查找路径,按优先级。

    优先级:
        1. $COOP_PREFIX/client/client.yaml 或 $COOP_PREFIX/client.yaml
           (用户显式指定 PREFIX,覆盖自动检测)
        2. 自动检测: $PREFIX/client/client.yaml (server 布局)
                 或 $PREFIX/client.yaml (worker 布局)
        3. ~/.coop/client.yaml          (用户级)
        4. ~/.coop/client.yml
        5. /etc/coop/client.yaml        (系统级)
    """
    paths: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        rp = p.resolve() if p.is_absolute() else p
        if rp not in seen:
            paths.append(p)
            seen.add(rp)

    # 1. 环境变量 (用户显式优先)
    # 支持 server 端和 worker 端两种布局
    env_prefix = os.environ.get(ENV_PREFIX)
    if env_prefix:
        prefix_path = Path(env_prefix).expanduser()
        add(prefix_path / "client" / "client.yaml")  # server 端
        add(prefix_path / "client.yaml")              # worker 端

    # 2. 自动从 CLI 安装位置反推 (返回真实存在的路径)
    auto = _detect_client_config_from_executable()
    if auto is not None:
        add(auto)

    # 3-5. 用户级和系统级
    add(Path.home() / ".coop" / "client.yaml")
    add(Path.home() / ".coop" / "client.yml")
    add(Path("/etc/coop/client.yaml"))

    return paths


# 保留旧名字给老测试 / 单测 patch 用
# 注意: 这是一个属性式访问,不是真常量
def __getattr__(name: str):
    if name == "CLIENT_CONFIG_PATHS":
        return _client_config_paths()
    raise AttributeError(name)


# token 默认查找路径 (配置文件不指定 token_file 时用)
DEFAULT_TOKEN_PATHS = [
    Path.home() / ".coop" / "token",
    Path("/etc/coop/token"),
]


@dataclass(frozen=True)
class CoordinatorAddr:
    host: str | None = None
    port: int = 7777


@dataclass(frozen=True)
class ClientConfig:
    """客户端配置,从 yaml 文件加载。所有字段都可选。"""
    token_file: str | None = None
    coordinator: CoordinatorAddr = field(default_factory=CoordinatorAddr)
    workspace_dir: str | None = None
    # 来源标记 (用于诊断)
    source_path: str | None = None


def load_client_config(
    explicit_path: str | Path | None = None,
) -> ClientConfig:
    """按优先级查找配置文件并加载。

    Args:
        explicit_path: 用户显式指定的配置文件路径(命令行 --config 或类似来源)。
                       如果指定了,只会用这个,找不到就报错。
                       不指定就按 _client_config_paths() 顺序查找。
    """
    if explicit_path is not None:
        p = Path(explicit_path).expanduser()
        if not p.exists():
            raise RuntimeError(f"指定的配置文件不存在: {p}")
        return _load_yaml(p)

    # 也看环境变量 COOP_CONFIG
    env_config = os.environ.get(ENV_CONFIG)
    if env_config:
        p = Path(env_config).expanduser()
        if not p.exists():
            raise RuntimeError(
                f"{ENV_CONFIG}={env_config} 指向的配置文件不存在"
            )
        return _load_yaml(p)

    # 按默认查找顺序
    for path in _client_config_paths():
        if path.exists():
            return _load_yaml(path)
    return ClientConfig()


def _load_yaml(path: Path) -> ClientConfig:
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        # 配置文件存在但读不动 / 解析失败,raise 让用户立即知道
        raise RuntimeError(f"读取配置文件 {path} 失败: {e}") from e

    if not isinstance(raw, dict):
        raise RuntimeError(
            f"配置文件 {path} 根节点必须是 mapping, 实际类型: {type(raw).__name__}"
        )

    coord_raw = raw.get("coordinator") or {}
    coord = CoordinatorAddr(
        host=coord_raw.get("host"),
        port=int(coord_raw.get("port", 7777)),
    )

    return ClientConfig(
        token_file=raw.get("token_file"),
        coordinator=coord,
        workspace_dir=raw.get("workspace_dir"),
        source_path=str(path),
    )


# ===========================================================
# Token 加载 (整合所有来源)
# ===========================================================

def load_token(
    token_file: str | None = None,
    config: ClientConfig | None = None,
) -> str | None:
    """加载 token。优先级:
       1. 显式 token_file 参数
       2. COOP_TOKEN_FILE 环境变量
       3. config.token_file (从 ~/.coop/client.yaml 读)
       4. 默认路径 (~/.coop/token, /etc/coop/token)
    """
    # 1. 命令行参数
    if token_file:
        p = Path(token_file).expanduser()
        if p.exists():
            return _read_file(p)
        return None

    # 2. 环境变量
    env_path = os.environ.get(ENV_TOKEN_FILE)
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return _read_file(p)
        return None

    # 3. 客户端配置文件
    if config is None:
        config = load_client_config()
    if config.token_file:
        p = Path(config.token_file).expanduser()
        if p.exists():
            return _read_file(p)
        return None

    # 4. 默认路径(向后兼容,允许有的部署直接放 ~/.coop/token)
    for p in DEFAULT_TOKEN_PATHS:
        if p.exists():
            tok = _read_file(p)
            if tok:
                return tok
    return None


def _read_file(p: Path) -> str | None:
    try:
        text = p.read_text(encoding="utf-8").strip()
        return text if text else None
    except OSError:
        return None


# ===========================================================
# Coop Server 地址解析
# ===========================================================

def resolve_coordinator(
    host: str | None = None,
    port: int | None = None,
    config: ClientConfig | None = None,
) -> tuple[str | None, int]:
    """解析Coop Server 地址。

    优先级:
        1. 命令行参数
        2. 环境变量(COOP_HOST, COOP_PORT)
        3. 配置文件 coordinator.host / port

    Returns:
        (host_or_None, port). host 为 None 表示需要回退到 mDNS 发现。
    """
    if config is None:
        config = load_client_config()

    # host
    final_host: str | None = None
    if host:
        final_host = host
    elif os.environ.get(ENV_HOST):
        final_host = os.environ.get(ENV_HOST)
    elif config.coordinator.host:
        final_host = config.coordinator.host

    # port
    final_port: int = 7777
    if port:
        final_port = port
    elif os.environ.get(ENV_PORT):
        try:
            final_port = int(os.environ[ENV_PORT])
        except ValueError:
            pass
    elif config.coordinator.port:
        final_port = config.coordinator.port

    return final_host, final_port


# ===========================================================
# 诊断用的描述函数
# ===========================================================

def describe_config_lookup() -> str:
    """配置文件查找路径描述。"""
    lines = ["配置文件查找顺序:"]
    lines.append("  1. 命令行 --config 参数")

    env_config = os.environ.get(ENV_CONFIG)
    if env_config:
        exists = "存在" if Path(env_config).expanduser().exists() else "不存在"
        lines.append(f"  2. 环境变量 {ENV_CONFIG}={env_config} ({exists})")
    else:
        lines.append(f"  2. 环境变量 {ENV_CONFIG} (未设置)")

    for i, p in enumerate(_client_config_paths(), start=3):
        if p.exists():
            lines.append(f"  {i}. {p} ✓ (使用)")
            return "\n".join(lines)
        else:
            lines.append(f"  {i}. {p} (不存在)")
    return "\n".join(lines)


def describe_token_lookup(config: ClientConfig | None = None) -> str:
    """token 查找路径描述。"""
    if config is None:
        config = load_client_config()

    lines = ["token 查找顺序:"]
    lines.append("  1. 命令行 --token-file 参数")

    env = os.environ.get(ENV_TOKEN_FILE)
    if env:
        exists = "存在" if Path(env).expanduser().exists() else "不存在"
        lines.append(f"  2. 环境变量 {ENV_TOKEN_FILE}={env} ({exists})")
    else:
        lines.append(f"  2. 环境变量 {ENV_TOKEN_FILE} (未设置)")

    if config.token_file:
        p = Path(config.token_file).expanduser()
        exists = "存在" if p.exists() else "不存在"
        src = config.source_path or "(配置)"
        lines.append(
            f"  3. 配置文件 {src} 中的 token_file={config.token_file} ({exists})"
        )
    else:
        lines.append(f"  3. 配置文件 token_file (未指定)")

    for i, p in enumerate(DEFAULT_TOKEN_PATHS, start=4):
        exists = "存在" if p.exists() else "不存在"
        lines.append(f"  {i}. {p} ({exists})")
    return "\n".join(lines)


def describe_coordinator_lookup(config: ClientConfig | None = None) -> str:
    """Coop Server 地址查找路径描述。"""
    if config is None:
        config = load_client_config()
    lines = ["Coop Server 地址查找顺序:"]
    lines.append("  1. 命令行 --host/--port")

    env_host = os.environ.get(ENV_HOST)
    if env_host:
        env_port = os.environ.get(ENV_PORT, "7777")
        lines.append(f"  2. 环境变量 {ENV_HOST}={env_host} {ENV_PORT}={env_port}")
    else:
        lines.append(f"  2. 环境变量 {ENV_HOST} (未设置)")

    if config.coordinator.host:
        lines.append(
            f"  3. 配置文件: host={config.coordinator.host} "
            f"port={config.coordinator.port}"
        )
    else:
        lines.append(f"  3. 配置文件 coordinator.host (未指定)")

    lines.append("  4. mDNS 自动发现 (扫描局域网)")
    return "\n".join(lines)
