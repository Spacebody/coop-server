"""Coop Server 入口。

用法:
    python -m coop_server [--config /path/to/config.yaml]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .config import default_config_path, load_config
from .logging_setup import setup_logging
from .server import CoopServer

logger = logging.getLogger(__name__)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> asyncio.Event:
    """注册 SIGTERM/SIGINT 处理器,触发优雅关闭。"""
    stop_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        logger.info(f"收到 {signame},开始优雅关闭")
        stop_event.set()

    for signame in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(
                getattr(signal, signame), _on_signal, signame
            )
        except NotImplementedError:
            # Windows 不支持
            pass

    return stop_event


async def _main_async(config_path: Path) -> int:
    config = load_config(config_path)
    setup_logging(config.logging)

    logger.info("=" * 60)
    logger.info(f"Coop Server 启动")
    logger.info(f"配置文件: {config_path}")
    logger.info(f"监听: {config.server.host}:{config.server.port}")
    logger.info(f"传输: {config.server.transport}")
    logger.info(f"DB: {config.database.path}")
    logger.info("=" * 60)

    server = CoopServer(config)
    await server.setup()

    # 提示用户 token 在哪
    if config.auth.enabled:
        logger.info(
            f"鉴权 token 文件: {config.auth.token_file} "
            f"(将此文件分发给 worker 机器,例如通过共享网盘)"
        )

    try:
        await server.run()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    except Exception:
        logger.exception("Server 运行出错")
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="coop-server")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="配置文件路径,默认按 COOP_CONFIG/cwd/etc 顺序查找",
    )
    args = parser.parse_args()

    config_path = args.config or default_config_path()

    if not config_path.exists():
        print(f"错误: 配置文件 {config_path} 不存在", file=sys.stderr)
        print(
            "可通过 --config 指定路径,或设置 COOP_CONFIG 环境变量",
            file=sys.stderr,
        )
        return 1

    try:
        return asyncio.run(_main_async(config_path))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
