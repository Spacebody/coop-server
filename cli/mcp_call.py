"""MCP 调用封装。

提供两种使用模式:

短会话 (call_tool 函数):
  每次调用都开新会话。适合一次性查询 (ping, list_workers 等)。
  优点: 简单,不需要管理生命周期。
  缺点: 每次都 TCP 握手 + initialize,密集调用浪费。

长会话 (MCPClient 类):
  一次 connect, 多次 call, 最后 close。适合 simulate-worker / coordinator
  这种密集调用场景。会话期间复用 TCP 连接和 MCP 协议状态。
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _extract_result(result: Any) -> dict[str, Any]:
    """从 MCP CallToolResult 里取出我们想要的 dict。"""
    if result.structuredContent is not None:
        return result.structuredContent  # type: ignore[return-value]
    # 退化到非结构化输出
    text_blocks = [b.text for b in result.content if hasattr(b, "text")]
    return {"_raw": "\n".join(text_blocks)}


# ===========================================================
# 短会话 (一次性查询用)
# ===========================================================

async def call_tool(
    url: str, token: str, tool_name: str, arguments: dict[str, Any],
    timeout_sec: float = 10,
) -> dict[str, Any]:
    """打开短会话调用一次工具,返回 structuredContent。

    适合: ping / list_workers / list_tasks / 单次工具调用。
    不适合密集调用,会有重复 initialize 开销。
    """
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return _extract_result(result)


# ===========================================================
# 长会话 (持续调用用)
# ===========================================================

class MCPClient:
    """持有 MCP 会话的客户端。一次 connect 后多次 call,最后 close。

    用法:
        client = MCPClient(url, token)
        await client.connect()
        try:
            r = await client.call("tool_name", {...})
            r2 = await client.call("another_tool", {...})
        finally:
            await client.close()

    或者作为 async context manager:
        async with MCPClient(url, token) as client:
            r = await client.call("tool_name", {...})

    线程安全: 否。一个实例同时只能有一个 call() 在运行。
    用于多协程场景请每个协程一个 MCPClient。
    """

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        """建立连接,完成 MCP initialize 握手。"""
        if self._session is not None:
            return  # 已连接

        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            headers = {"Authorization": f"Bearer {self.token}"}
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(self.url, headers=headers)
            )
            session = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
        except BaseException:
            # 建立过程中出问题,清理已 enter 的资源
            await stack.aclose()
            raise

        # 全部成功后才赋值 (保证 connect 失败后 self._session 仍是 None)
        self._stack = stack
        self._session = session

    async def call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """复用会话调用工具。"""
        if self._session is None:
            raise RuntimeError("MCPClient 未连接,请先 await client.connect()")
        result = await self._session.call_tool(tool_name, arguments)
        return _extract_result(result)

    async def close(self) -> None:
        """关闭会话。"""
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    @property
    def connected(self) -> bool:
        return self._session is not None

    # ===== async context manager =====

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
