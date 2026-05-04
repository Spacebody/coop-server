"""Token 鉴权。

设计:
- 启动时检查 token_file。不存在则生成随机 32 字符 hex token,写入文件 (0600 权限)。
- worker 通过 Authorization: Bearer <token> header 携带 token。
- 用 starlette ASGI 中间件做校验,在所有 MCP 请求之前拦截。
- 健康检查路由 (/health) 不需要 token。
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# 不需要鉴权的路径前缀
PUBLIC_PATHS = ("/health",)


def load_or_generate_token(
    token_file: str,
    token_length: int = 32,
) -> str:
    """加载现有 token,或生成新 token 并写入文件。

    文件权限设为 0600 (仅 owner 可读写)。
    """
    path = Path(token_file)

    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"Token 文件 {token_file} 内容为空")
        if len(token) < 16:
            raise ValueError(
                f"Token 文件 {token_file} 中的 token 太短 (< 16 字符),不安全"
            )
        logger.info(f"已加载 token 文件: {token_file}")
        return token

    # 生成新 token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(token_length // 2)  # hex 字符数 = bytes * 2
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        # Windows 上可能失败,警告但不阻塞
        logger.warning(f"无法设置 token 文件权限 0600: {e}")
    logger.info(f"已生成新 token 写入: {token_file}")
    return token


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """简单的 Bearer token 校验中间件。"""

    def __init__(self, app: ASGIApp, expected_token: str) -> None:
        super().__init__(app)
        self._expected = expected_token

    async def dispatch(self, request: Request, call_next):
        # 公开路径不校验
        for prefix in PUBLIC_PATHS:
            if request.url.path.startswith(prefix):
                return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "缺少 Authorization Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        provided = auth_header[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, self._expected):
            logger.warning(
                f"鉴权失败: 来自 {request.client.host if request.client else '?'}"
            )
            return JSONResponse(
                {"error": "Token 无效"},
                status_code=403,
            )

        return await call_next(request)
