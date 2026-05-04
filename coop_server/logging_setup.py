"""日志初始化。

格式化输出到 stderr 和滚动文件。生产环境也允许只输出到 stderr (例如 docker 日志驱动收集)。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingConfig

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg: LoggingConfig) -> None:
    """配置全局 root logger。多次调用是幂等的。"""
    root = logging.getLogger()
    # 清掉已有 handler 避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(cfg.level.upper())
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # stderr handler (docker 日志驱动会收集)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    # 文件 handler (滚动)
    if cfg.file:
        log_path = Path(cfg.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.file,
            maxBytes=cfg.rotate_max_bytes,
            backupCount=cfg.rotate_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # 抑制底层框架的过度日志
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
