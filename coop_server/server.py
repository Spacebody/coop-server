"""Server 装配:把 store / waiters / heartbeat / auth / mDNS / MCP 串起来。

启动顺序:
1. 加载配置,初始化日志
2. 加载/生成 token
3. 初始化 DB(创建表)
4. 创建 Waiters 单例
5. 创建并启动 HeartbeatMonitor
6. 创建 WorkerTools 和 CoordinatorTools, 注册到 FastMCP
7. 启动 mDNS 广播
8. 启动 HTTP server (streamable_http 或 sse)

停止顺序逆序,确保资源都清理。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import BearerAuthMiddleware, load_or_generate_token
from .config import Config
from .coordinator_tools import CoordinatorTools
from .db import init_db
from .discovery import MDNSAdvertiser
from .heartbeat import HeartbeatMonitor
from .waiters import Waiters
from .worker_tools import WorkerTools

logger = logging.getLogger(__name__)


class CoopServer:
    """完整 server 容器。"""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._token: str | None = None
        self._waiters: Waiters | None = None
        self._heartbeat: HeartbeatMonitor | None = None
        self._mdns: MDNSAdvertiser | None = None
        self._mcp: FastMCP | None = None
        self._worker_tools: WorkerTools | None = None
        self._coord_tools: CoordinatorTools | None = None

    async def _setup(self) -> None:
        """启动前的资源准备。"""
        cfg = self._config

        # token
        if cfg.auth.enabled:
            self._token = load_or_generate_token(
                cfg.auth.token_file, cfg.auth.token_length
            )
            logger.info(f"鉴权已启用 (token 文件: {cfg.auth.token_file})")
        else:
            logger.warning("鉴权已禁用 - 仅建议在隔离测试环境使用!")

        # DB
        await init_db(cfg.database.path)

        # waiters
        self._waiters = Waiters()

        # 工具集
        self._worker_tools = WorkerTools(cfg.database.path, self._waiters)
        self._coord_tools = CoordinatorTools(cfg.database.path, self._waiters)

        # 心跳
        self._heartbeat = HeartbeatMonitor(
            cfg.database.path, cfg.heartbeat, self._waiters
        )

        # mDNS
        self._mdns = MDNSAdvertiser(
            cfg.discovery,
            cfg.server.port,
            properties={
                "transport": cfg.server.transport,
                "auth": "bearer" if cfg.auth.enabled else "none",
                "version": "0.1.0",
            },
        )

        # MCP - 用 lifespan 控制后台任务的启停
        self._build_mcp()

    def _build_mcp(self) -> None:
        """构造 FastMCP 实例并注册所有工具。"""
        cfg = self._config

        @asynccontextmanager
        async def lifespan(_mcp: FastMCP):
            """MCP 服务运行期间的资源管理。"""
            assert self._heartbeat is not None
            assert self._mdns is not None
            try:
                await self._heartbeat.start()
                await self._mdns.start()
                logger.info("Coop Server 全部组件启动完成")
                yield {}
            finally:
                logger.info("Coop Server 开始优雅关闭")
                await self._mdns.stop()
                await self._heartbeat.stop()
                logger.info("Coop Server 已停止")

        mcp = FastMCP(
            name="coop",
            instructions="Claude Code 局域网协作系统 MCP server",
            host=cfg.server.host,
            port=cfg.server.port,
            lifespan=lifespan,
        )

        self._register_tools(mcp)
        self._register_routes(mcp)

        self._mcp = mcp

    def _register_tools(self, mcp: FastMCP) -> None:
        """注册所有 MCP 工具。"""
        wt = self._worker_tools
        ct = self._coord_tools
        assert wt is not None and ct is not None

        # ========== Worker 端 ==========

        @mcp.tool(description="worker 启动时声明上线。")
        async def register_worker(worker_id: str, hostname: str) -> dict[str, Any]:
            return await wt.register_worker(worker_id, hostname)

        @mcp.tool(description="worker 定期心跳,声明仍然存活。")
        async def heartbeat(worker_id: str) -> dict[str, Any]:
            return await wt.heartbeat(worker_id)

        @mcp.tool(
            description="worker 主动下线。从 server 删除 worker 记录,持有的进行中任务会被标记 abandoned。"
        )
        async def deregister_worker(worker_id: str) -> dict[str, Any]:
            return await wt.deregister_worker(worker_id)

        @mcp.tool(
            description="worker 长轮询等待派发的任务。返回 status=assigned 时拿到任务,no_task 表示超时无任务。"
        )
        async def wait_for_task(
            worker_id: str, timeout_sec: int = 60
        ) -> dict[str, Any]:
            return await wt.wait_for_task(worker_id, timeout_sec)

        @mcp.tool(
            description="worker 提交完成的任务。必须传 project/branch/commit_sha 让协调者能找到代码 review。"
        )
        async def submit_work(
            worker_id: str,
            task_id: str,
            project: str,
            branch: str,
            commit_sha: str,
            summary: str = "",
        ) -> dict[str, Any]:
            return await wt.submit_work(
                worker_id, task_id, project, branch, commit_sha, summary
            )

        @mcp.tool(description="worker 阶段性进度汇报。")
        async def report_progress(
            worker_id: str, task_id: str, note: str
        ) -> dict[str, Any]:
            return await wt.report_progress(worker_id, task_id, note)

        @mcp.tool(
            description="worker 卡住升级。常见场景:本机未配置该工程、环境问题、依赖缺失。"
        )
        async def report_blocked(
            worker_id: str, task_id: str, reason: str
        ) -> dict[str, Any]:
            return await wt.report_blocked(worker_id, task_id, reason)

        @mcp.tool(
            description="worker 任务不明确时向协调者提问。返回后必须立即调 wait_for_clarification 等答复。"
        )
        async def request_clarification(
            worker_id: str, task_id: str, question: str
        ) -> dict[str, Any]:
            return await wt.request_clarification(worker_id, task_id, question)

        @mcp.tool(description="worker 长轮询等协调者对 clarification 的答复。")
        async def wait_for_clarification(
            task_id: str, timeout_sec: int = 300
        ) -> dict[str, Any]:
            return await wt.wait_for_clarification(task_id, timeout_sec)

        @mcp.tool(
            description="worker 在提交后等待协调者下发清理指令。收到后应执行 git worktree remove 然后 acknowledge_cleanup。"
        )
        async def wait_for_cleanup_request(
            worker_id: str, task_id: str, timeout_sec: int = 600
        ) -> dict[str, Any]:
            return await wt.wait_for_cleanup_request(
                worker_id, task_id, timeout_sec
            )

        @mcp.tool(description="worker 完成 worktree 清理后回执。")
        async def acknowledge_cleanup(
            worker_id: str, task_id: str
        ) -> dict[str, Any]:
            return await wt.acknowledge_cleanup(worker_id, task_id)

        # ========== 协调者端 ==========

        @mcp.tool(
            description="查看 worker 列表。online_only=True (默认) 时只显示在线的, False 也包括 OFFLINE 已失联的。"
        )
        async def list_workers(online_only: bool = True) -> dict[str, Any]:
            return await ct.list_workers(online_only=online_only)

        @mcp.tool(
            description="清理所有已失联(OFFLINE)的 worker 记录。返回删除数量。"
        )
        async def prune_offline_workers() -> dict[str, Any]:
            return await ct.prune_offline_workers()

        @mcp.tool(description="查看任务列表,可按状态过滤。")
        async def list_tasks(status: str | None = None) -> dict[str, Any]:
            return await ct.list_tasks(status)

        @mcp.tool(
            description="协调者发布任务给指定 worker。description 用自然语言写,需包含工程名、分支名、目标、验收标准。"
        )
        async def publish_task(
            task_id: str,
            assignee: str,
            description: str,
            priority: str = "normal",
            parent_task_id: str | None = None,
            depends_on: list[str] | None = None,
        ) -> dict[str, Any]:
            return await ct.publish_task(
                task_id=task_id,
                assignee=assignee,
                description=description,
                priority=priority,
                parent_task_id=parent_task_id,
                depends_on=depends_on,
            )

        @mcp.tool(description="协调者取消任务。")
        async def cancel_task(task_id: str, reason: str = "") -> dict[str, Any]:
            return await ct.cancel_task(task_id, reason)

        @mcp.tool(description="协调者答复 worker 的 clarification 提问。")
        async def respond_clarification(
            task_id: str, answer: str
        ) -> dict[str, Any]:
            return await ct.respond_clarification(task_id, answer)

        @mcp.tool(
            description="协调者通知 worker 清理 worktree。任务必须是 SUBMITTED 状态。"
        )
        async def request_cleanup(task_id: str) -> dict[str, Any]:
            return await ct.request_cleanup(task_id)

        @mcp.tool(description="协调者长轮询接收 worker 事件。")
        async def wait_for_event(timeout_sec: int = 60) -> dict[str, Any]:
            return await ct.wait_for_event(timeout_sec)

    def _register_routes(self, mcp: FastMCP) -> None:
        """注册自定义路由(健康检查)。"""

        @mcp.custom_route("/health", methods=["GET"])
        async def health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok"})

    # =======================================================
    # 公共接口
    # =======================================================

    async def setup(self) -> None:
        """初始化所有资源,但不启动 HTTP server。"""
        await self._setup()

    def get_mcp(self) -> FastMCP:
        if self._mcp is None:
            raise RuntimeError("setup() 未调用")
        return self._mcp

    def get_token(self) -> str | None:
        return self._token

    def get_app(self):
        """返回 ASGI app,带鉴权中间件。

        用 streamable_http_app() 拿到 app,再包一层 BearerAuthMiddleware。
        """
        if self._mcp is None:
            raise RuntimeError("setup() 未调用")

        if self._config.server.transport == "streamable_http":
            app = self._mcp.streamable_http_app()
        else:
            app = self._mcp.sse_app()

        if self._config.auth.enabled and self._token:
            app.add_middleware(
                BearerAuthMiddleware,
                expected_token=self._token,
            )

        return app

    async def run(self) -> None:
        """blocking 启动 HTTP server。

        我们不直接调 mcp.run_streamable_http_async(), 因为那会用 mcp 自己
        构造的 app, 绕过我们的鉴权中间件。这里手动 wrap 一层。
        """
        import uvicorn

        if self._mcp is None:
            await self._setup()

        app = self.get_app()
        cfg = self._config.server

        # FastMCP 的 lifespan 已经绑在内部 starlette app 里, 包了中间件后
        # lifespan 仍然会被 uvicorn 触发(starlette 的 add_middleware 不破坏 lifespan)
        config = uvicorn.Config(
            app,
            host=cfg.host,
            port=cfg.port,
            log_level=self._config.logging.level.lower(),
            access_log=False,  # 我们自己的日志已经够用
        )
        server = uvicorn.Server(config)
        await server.serve()
