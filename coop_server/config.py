"""配置加载与校验。

配置文件用 YAML,字段在启动时校验,有缺失/错误立即报错退出。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ServerConfig:
    """HTTP server 监听配置。"""
    host: str = "0.0.0.0"
    port: int = 7777
    transport: str = "streamable_http"  # 'streamable_http' or 'sse'

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError(f"server.port 不合法: {self.port}")
        if self.transport not in ("streamable_http", "sse"):
            raise ValueError(
                f"server.transport 必须是 streamable_http 或 sse, "
                f"实际: {self.transport}"
            )


@dataclass(frozen=True)
class AuthConfig:
    """token 鉴权配置。"""
    enabled: bool = True
    token_file: str = "/data/token"
    token_length: int = 32

    def validate(self) -> None:
        if self.token_length < 16:
            raise ValueError("auth.token_length 不能小于 16")


@dataclass(frozen=True)
class DatabaseConfig:
    """SQLite 配置。"""
    path: str = "/data/coop.db"

    def validate(self) -> None:
        # 路径所在目录必须存在或可创建,运行时再做
        if not self.path:
            raise ValueError("database.path 不能为空")


@dataclass(frozen=True)
class HeartbeatConfig:
    """心跳超时配置。"""
    worker_interval_sec: int = 30   # worker 应当每 N 秒心跳一次
    timeout_sec: int = 90           # 超过 N 秒未心跳标记 offline
    check_interval_sec: int = 30    # 后台扫描频率

    def validate(self) -> None:
        if self.worker_interval_sec <= 0:
            raise ValueError("heartbeat.worker_interval_sec 必须 > 0")
        if self.timeout_sec <= self.worker_interval_sec:
            raise ValueError(
                f"heartbeat.timeout_sec ({self.timeout_sec}) "
                f"必须大于 worker_interval_sec ({self.worker_interval_sec})"
            )


@dataclass(frozen=True)
class DiscoveryConfig:
    """mDNS 服务发现配置。"""
    enabled: bool = True
    service_type: str = "_coop._tcp.local."
    service_name: str = "coop-server"
    advertise_host: str = ""  # 空则用 hostname 自动检测


@dataclass(frozen=True)
class LoggingConfig:
    """日志配置。"""
    level: str = "INFO"
    file: str = "/data/coop.log"
    rotate_max_bytes: int = 10 * 1024 * 1024  # 10MB
    rotate_backup_count: int = 5

    def validate(self) -> None:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.level.upper() not in valid_levels:
            raise ValueError(f"logging.level 必须是 {valid_levels}")


@dataclass(frozen=True)
class Config:
    """完整配置。"""
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> None:
        """递归校验所有子配置。"""
        self.server.validate()
        self.auth.validate()
        self.database.validate()
        self.heartbeat.validate()
        self.logging.validate()


def _build_section(cls, data: dict[str, Any] | None):
    """根据 dataclass 类和字典构造实例,只接受已知字段。"""
    if data is None:
        return cls()
    valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data.keys()) - valid_fields
    if unknown:
        raise ValueError(f"{cls.__name__} 含未知字段: {unknown}")
    return cls(**{k: v for k, v in data.items() if k in valid_fields})


def load_config(path: str | Path) -> Config:
    """从 YAML 文件加载配置。

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置内容不合法
        yaml.YAMLError: YAML 解析失败
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"配置文件根节点必须是 mapping, 实际类型: {type(raw)}")

    config = Config(
        server=_build_section(ServerConfig, raw.get("server")),
        auth=_build_section(AuthConfig, raw.get("auth")),
        database=_build_section(DatabaseConfig, raw.get("database")),
        heartbeat=_build_section(HeartbeatConfig, raw.get("heartbeat")),
        discovery=_build_section(DiscoveryConfig, raw.get("discovery")),
        logging=_build_section(LoggingConfig, raw.get("logging")),
    )
    config.validate()
    return config


def default_config_path() -> Path:
    """配置文件查找顺序: 环境变量 → 工作目录 → /etc。"""
    env = os.environ.get("COOP_CONFIG")
    if env:
        return Path(env)

    cwd = Path("config.yaml")
    if cwd.exists():
        return cwd

    return Path("/app/config.yaml")
