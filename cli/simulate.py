"""通信框架自动化验证工具。

不依赖 Claude,纯脚本通过 MCP 协议跟 server 交互,验证通信链路。

提供三个能力:
  - SimulatedWorker: 一个自动化 worker 客户端
  - SimulatedCoordinator: 一个自动化协调者客户端
  - SmokeTest: 端到端烟雾测试,跑完整流程

设计原则:
  - 不依赖任何 LLM,纯协议交互
  - 模拟真实 Claude 会做的事 (注册、心跳、接活、提交)
  - 时序日志清晰,出问题能定位到具体步骤
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from .mcp_call import MCPClient, call_tool


logger = logging.getLogger(__name__)


# ===========================================================
# 时序日志 (打印到终端,带时间戳和颜色)
# ===========================================================

def log_step(role: str, msg: str, color: str = "") -> None:
    """打印一行时序日志,role 用于区分 worker / coordinator。"""
    ts = time.strftime("%H:%M:%S")
    role_padded = role.ljust(12)
    if color and sys.stdout.isatty():
        print(f"\033[{color}m{ts} [{role_padded}] {msg}\033[0m")
    else:
        print(f"{ts} [{role_padded}] {msg}")


def log_worker(msg: str) -> None:
    log_step("worker", msg, "36")  # cyan


def log_coord(msg: str) -> None:
    log_step("coordinator", msg, "33")  # yellow


def log_test(msg: str) -> None:
    log_step("test", msg, "32")  # green


def log_error(msg: str) -> None:
    log_step("ERROR", msg, "31;1")  # bold red


# ===========================================================
# SimulatedWorker
# ===========================================================

@dataclass
class WorkerStats:
    tasks_received: int = 0
    tasks_submitted: int = 0
    clarifications_requested: int = 0
    blocks_reported: int = 0


class SimulatedWorker:
    """模拟 worker 客户端。

    自动循环:wait_for_task → 假装干活 → submit_work → wait_for_cleanup → ack。
    """

    def __init__(
        self,
        url: str,
        token: str,
        worker_id: str,
        hostname: str | None = None,
        # 行为模式
        auto_submit: bool = True,
        # 可注入的"特殊行为",用于测试不同分支
        force_blocked_reason: str | None = None,    # 收到任务后立即 blocked
        force_clarification: str | None = None,     # 收到任务后先 clarification
        simulate_work_seconds: float = 0.5,         # 假装干活的耗时
    ) -> None:
        self.url = url
        self.token = token
        self.worker_id = worker_id
        self.hostname = hostname or socket.gethostname()
        self.auto_submit = auto_submit
        self.force_blocked_reason = force_blocked_reason
        self.force_clarification = force_clarification
        self.simulate_work_seconds = simulate_work_seconds
        self.stats = WorkerStats()
        self._stop = False
        # work_loop 和 heartbeat_loop 各自的会话
        # MCPClient 不能并发调用,所以两个循环用不同 client
        self._work_client = MCPClient(url, token)
        self._hb_client = MCPClient(url, token)

    async def _call(
        self, client: MCPClient, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """通过指定 client 调用工具。失败时返回 {}。"""
        if self._stop:
            return {}
        try:
            result = await client.call(tool, args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 若已在 stop 状态,异常多半来自 cancel 中的连接断开,不是真实错
            if not self._stop:
                log_error(f"调用 {tool} 异常: {type(e).__name__}: {e}")
            return {}
        return result if result is not None else {}

    def stop(self) -> None:
        self._stop = True

    async def heartbeat_loop(self, interval_sec: float = 30) -> None:
        """后台心跳协程。client 在本 task 内 connect+close,避免 anyio 跨 task 错误。"""
        await self._hb_client.connect()
        try:
            while not self._stop:
                await self._call(self._hb_client, "heartbeat", {
                    "worker_id": self.worker_id
                })
                try:
                    await asyncio.sleep(interval_sec)
                except asyncio.CancelledError:
                    return
        finally:
            await self._hb_client.close()

    async def work_loop(self) -> None:
        """主接活循环。client 在本 task 内 connect+close+register。"""
        await self._work_client.connect()
        try:
            # 在 work_loop 这个 task 内做 register,保证 client 全程同一 task
            log_worker(
                f"register_worker(worker_id={self.worker_id}, "
                f"hostname={self.hostname})"
            )
            r = await self._call(self._work_client, "register_worker", {
                "worker_id": self.worker_id,
                "hostname": self.hostname,
            })
            if not r.get("ok"):
                raise RuntimeError(f"register 失败: {r}")
            log_worker(f"  → 注册成功")

            await self._work_loop_inner()
        finally:
            # 退出前主动调 deregister,让 server 清掉这条 worker 记录
            # (失败也无所谓,server 端有心跳超时兜底)
            try:
                await self._work_client.call("deregister_worker", {
                    "worker_id": self.worker_id,
                })
                log_worker(f"  → 已 deregister")
            except Exception:
                pass  # deregister 是 best-effort
            await self._work_client.close()

    async def _work_loop_inner(self) -> None:
        """主接活循环内部实现 (不管 client 生命周期)。"""
        while not self._stop:
            log_worker(f"wait_for_task (timeout=10s)")
            r = await self._call(self._work_client, "wait_for_task", {
                "worker_id": self.worker_id,
                "timeout_sec": 10,
            })
            if self._stop:
                return
            if not r.get("ok"):
                log_error(f"wait_for_task 失败: {r}")
                await asyncio.sleep(1)
                continue

            if r.get("status") == "no_task":
                log_worker(f"  → no_task,继续等")
                continue

            task = r["task"]
            self.stats.tasks_received += 1
            task_id = task["task_id"]
            log_worker(f"  → 收到任务 {task_id}: {task['description'][:60]}...")

            # 决定怎么处理
            if self.force_blocked_reason:
                await self._do_blocked(task_id, self.force_blocked_reason)
            elif self.force_clarification:
                await self._do_clarification_then_submit(task, self.force_clarification)
            elif self.auto_submit:
                await self._do_submit(task)
            else:
                log_worker(f"  → auto_submit=False,不主动提交,任务挂起")

    async def _do_submit(self, task: dict[str, Any]) -> None:
        task_id = task["task_id"]
        await asyncio.sleep(self.simulate_work_seconds)

        # 编造 submit 参数
        project = "myapp"
        branch = f"feature/{task_id.lower()}"
        commit_sha = f"abc{task_id.replace('-', '').lower()}"[:12]
        log_worker(
            f"submit_work(task={task_id}, project={project}, "
            f"branch={branch}, sha={commit_sha})"
        )
        r = await self._call(self._work_client, "submit_work", {
            "worker_id": self.worker_id,
            "task_id": task_id,
            "project": project,
            "branch": branch,
            "commit_sha": commit_sha,
            "summary": f"完成 {task_id} 模拟工作",
        })
        if not r.get("ok"):
            log_error(f"submit_work 失败: {r}")
            return
        log_worker(f"  → 已提交")
        self.stats.tasks_submitted += 1

        # 等清理指令
        log_worker(f"wait_for_cleanup_request (timeout=30s)")
        r = await self._call(self._work_client, "wait_for_cleanup_request", {
            "worker_id": self.worker_id,
            "task_id": task_id,
            "timeout_sec": 30,
        })
        if r.get("status") == "cleanup_requested":
            log_worker(f"  → 收到清理指令")
            r2 = await self._call(self._work_client, "acknowledge_cleanup", {
                "worker_id": self.worker_id,
                "task_id": task_id,
            })
            if r2.get("ok"):
                log_worker(f"  → 清理已 ack")
            elif not self._stop:
                log_error(f"ack_cleanup 失败: {r2}")
        elif r.get("status") == "already_closed":
            log_worker(f"  → 任务已关闭 (协调者跳过了清理)")
        else:
            log_worker(f"  → 清理指令未到 ({r.get('status')}),继续接下一单")

    async def _do_blocked(self, task_id: str, reason: str) -> None:
        log_worker(f"report_blocked(task={task_id}, reason={reason})")
        r = await self._call(self._work_client, "report_blocked", {
            "worker_id": self.worker_id,
            "task_id": task_id,
            "reason": reason,
        })
        if r.get("ok"):
            log_worker(f"  → 已上报 blocked")
            self.stats.blocks_reported += 1
        else:
            log_error(f"report_blocked 失败: {r}")

    async def _do_clarification_then_submit(
        self, task: dict[str, Any], question: str
    ) -> None:
        task_id = task["task_id"]
        log_worker(f"request_clarification(task={task_id}, q={question})")
        r = await self._call(self._work_client, "request_clarification", {
            "worker_id": self.worker_id,
            "task_id": task_id,
            "question": question,
        })
        if not r.get("ok"):
            log_error(f"request_clarification 失败: {r}")
            return
        log_worker(f"  → 已提问,等答复")
        self.stats.clarifications_requested += 1

        log_worker(f"wait_for_clarification (timeout=30s)")
        r = await self._call(self._work_client, "wait_for_clarification", {
            "task_id": task_id,
            "timeout_sec": 30,
        })
        if r.get("status") == "answered":
            log_worker(f"  → 收到答复: {r['answer']}")
            await self._do_submit(task)
        else:
            log_worker(f"  → 等答复超时 ({r.get('status')})")

    async def run(self, with_heartbeat: bool = True) -> None:
        """启动 worker。会一直循环直到 stop()。

        每个 loop 自己管理对应 MCPClient 的 connect/close,避免 anyio
        要求 client context manager 必须在同一 task 内进入和退出的限制。
        """
        tasks = [asyncio.create_task(self.work_loop(), name="work-loop")]
        if with_heartbeat:
            tasks.append(asyncio.create_task(
                self.heartbeat_loop(), name="heartbeat-loop"
            ))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._stop = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ===========================================================
# SimulatedCoordinator
# ===========================================================

@dataclass
class CoordinatorStats:
    tasks_published: int = 0
    events_received: int = 0
    clarifications_answered: int = 0
    cleanups_requested: int = 0


class SimulatedCoordinator:
    """模拟协调者客户端。

    可以派任务,监听事件,自动答复 clarification,自动请求清理。
    """

    def __init__(
        self,
        url: str,
        token: str,
        # 行为
        auto_answer_clarification: str | None = "JWT",  # 自动答复内容
        auto_request_cleanup: bool = True,              # 收到 work_submitted 后自动 cleanup
    ) -> None:
        self.url = url
        self.token = token
        self.auto_answer = auto_answer_clarification
        self.auto_cleanup = auto_request_cleanup
        self.stats = CoordinatorStats()
        self._stop = False
        # 收到的事件历史 (供测试查询)
        self.events: list[dict[str, Any]] = []
        # 协调者用短会话即可——事件循环 5s 一次, publish_task 也是稀疏调用
        # worker 那边才是密集调用需要长会话

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """调用 MCP 工具。失败 / 关闭中时返回 {} 而非 None,避免下游 .get() 崩溃。"""
        if self._stop:
            return {}
        try:
            result = await call_tool(self.url, self.token, tool, args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 不管 _stop 状态都打印,排查问题时才能看到真凶
            log_error(f"调用 {tool} 异常: {type(e).__name__}: {e}")
            return {}
        return result if result is not None else {}

    def stop(self) -> None:
        self._stop = True

    async def connect(self) -> None:
        """协调者用短会话,无需 connect。保留方法只为对称。"""
        pass

    async def close(self) -> None:
        """协调者用短会话,无需 close。"""
        self._stop = True

    async def list_workers(self) -> list[dict[str, Any]]:
        r = await self._call("list_workers", {})
        if not r.get("ok"):
            raise RuntimeError(f"list_workers 失败: {r}")
        return r["workers"]

    async def publish_task(
        self,
        task_id: str,
        assignee: str,
        description: str,
        priority: str = "normal",
    ) -> dict[str, Any]:
        log_coord(f"publish_task(id={task_id}, to={assignee}, priority={priority})")
        r = await self._call("publish_task", {
            "task_id": task_id,
            "assignee": assignee,
            "description": description,
            "priority": priority,
        })
        if not r.get("ok"):
            log_error(f"publish_task 失败: {r}")
            raise RuntimeError(f"publish 失败: {r}")
        log_coord(f"  → 派发成功")
        self.stats.tasks_published += 1
        return r

    async def event_loop(self) -> None:
        """循环接收事件,自动处理。"""
        while not self._stop:
            r = await self._call("wait_for_event", {"timeout_sec": 5})
            if self._stop:
                return
            if not r.get("ok"):
                log_error(f"wait_for_event 失败: {r}")
                await asyncio.sleep(1)
                continue
            if r.get("status") != "event":
                continue

            ev = r["event"]
            self.events.append(ev)
            self.stats.events_received += 1
            ev_type = ev["type"]
            task_id = ev.get("task_id")
            log_coord(f"event: type={ev_type} task={task_id}")

            # 自动行为
            if ev_type == "clarification_requested" and self.auto_answer:
                log_coord(
                    f"  → 自动答复 clarification: '{self.auto_answer}'"
                )
                r2 = await self._call("respond_clarification", {
                    "task_id": task_id,
                    "answer": self.auto_answer,
                })
                if r2.get("ok"):
                    self.stats.clarifications_answered += 1
                else:
                    log_error(f"respond_clarification 失败: {r2}")

            elif ev_type == "work_submitted" and self.auto_cleanup:
                log_coord(f"  → 自动 request_cleanup")
                r2 = await self._call("request_cleanup", {"task_id": task_id})
                if r2.get("ok"):
                    self.stats.cleanups_requested += 1
                else:
                    log_error(f"request_cleanup 失败: {r2}")

    async def wait_for_event_type(
        self, event_type: str, timeout_sec: float = 30
    ) -> dict[str, Any] | None:
        """等待特定类型事件出现。仅看历史已收到的,不主动调 wait_for_event。"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            for ev in self.events:
                if ev["type"] == event_type:
                    return ev
            await asyncio.sleep(0.2)
        return None


# ===========================================================
# SmokeTest - 一键端到端
# ===========================================================

async def run_smoke_test(
    url: str, token: str, timeout_sec: float = 30
) -> bool:
    """跑完整端到端测试:派单 → 接活 → 提交 → 清理。

    Returns:
        True 全过, False 有失败
    """
    log_test("=" * 60)
    log_test(" Coop 通信链路烟雾测试")
    log_test("=" * 60)

    worker_id = f"smoke-test-worker-{int(time.time())}"
    task_id = f"SMOKE-{int(time.time())}"

    worker = SimulatedWorker(
        url=url,
        token=token,
        worker_id=worker_id,
        simulate_work_seconds=0.2,
    )
    coord = SimulatedCoordinator(url=url, token=token)

    # 先 connect 协调者 client (worker 在自己的 run() 里管 connect)
    await coord.connect()

    worker_task = asyncio.create_task(worker.run(), name="smoke-worker")
    coord_task = asyncio.create_task(coord.event_loop(), name="smoke-coord")

    success = True
    try:
        # 1. 等 worker 注册成功 + 协调者收到 worker_registered 事件
        log_test("步骤 1: 等 worker 注册并被协调者感知")
        ev = await coord.wait_for_event_type(
            "worker_registered", timeout_sec=10
        )
        if ev is None:
            log_error("超时未收到 worker_registered 事件")
            return False
        log_test(f"  ✓ 协调者收到 worker {ev.get('worker_id')} 注册事件")

        # 2. 协调者派任务
        log_test("步骤 2: 协调者派任务")
        await coord.publish_task(
            task_id=task_id,
            assignee=worker_id,
            description=f"测试任务 {task_id} - 在 myapp 实现 X",
        )

        # 3. 等 work_submitted 事件
        log_test("步骤 3: 等 worker 提交完成 (含派单→接活→干活→提交)")
        ev = await coord.wait_for_event_type(
            "work_submitted", timeout_sec=timeout_sec
        )
        if ev is None:
            log_error(f"超时 {timeout_sec}s 未收到 work_submitted 事件")
            return False
        log_test(f"  ✓ 协调者收到提交事件:")
        log_test(f"    project={ev.get('project')}")
        log_test(f"    branch={ev.get('branch')}")
        log_test(f"    commit_sha={ev.get('commit_sha')}")

        # 4. 等 cleanup_done (协调者会自动 request_cleanup, worker 会自动 ack)
        log_test("步骤 4: 等清理完成 (协调者自动请求 → worker 自动 ack)")
        ev = await coord.wait_for_event_type(
            "cleanup_done", timeout_sec=15
        )
        if ev is None:
            log_error("超时未收到 cleanup_done 事件")
            return False
        log_test(f"  ✓ 协调者收到清理完成事件")

        # 5. 总结
        log_test("")
        log_test("步骤 5: 统计数据")
        log_test(f"  worker  - 接收: {worker.stats.tasks_received}, 提交: {worker.stats.tasks_submitted}")
        log_test(f"  coord   - 派发: {coord.stats.tasks_published}, 收事件: {coord.stats.events_received}")
        log_test(f"  coord   - 自动 cleanup: {coord.stats.cleanups_requested}")

        if worker.stats.tasks_submitted == 0:
            log_error("Worker 没成功提交任何任务")
            success = False

    finally:
        worker.stop()
        coord.stop()
        await asyncio.sleep(0.5)
        worker_task.cancel()
        coord_task.cancel()
        # 等 cancel 完成,然后 close 协调者 client
        await asyncio.gather(worker_task, coord_task, return_exceptions=True)
        await coord.close()

    log_test("=" * 60)
    if success:
        log_test(" 烟雾测试通过 ✓")
    else:
        log_test(" 烟雾测试失败 ✗")
    log_test("=" * 60)

    return success


async def run_clarification_test(url: str, token: str) -> bool:
    """专门测试 clarification 双向流程。"""
    log_test("=" * 60)
    log_test(" Clarification 双向通信测试")
    log_test("=" * 60)

    worker_id = f"clar-test-{int(time.time())}"
    task_id = f"CLAR-{int(time.time())}"

    worker = SimulatedWorker(
        url=url, token=token, worker_id=worker_id,
        force_clarification="用 JWT 还是 OAuth?",
        simulate_work_seconds=0.2,
    )
    coord = SimulatedCoordinator(
        url=url, token=token,
        auto_answer_clarification="用 JWT",
    )

    await coord.connect()
    worker_task = asyncio.create_task(worker.run(), name="clar-worker")
    coord_task = asyncio.create_task(coord.event_loop(), name="clar-coord")

    success = True
    try:
        # 等 worker 注册被协调者感知 (而不是死等 sleep)
        ev = await coord.wait_for_event_type(
            "worker_registered", timeout_sec=10
        )
        if ev is None:
            log_error("超时未收到 worker_registered (worker 注册过程异常)")
            return False

        await coord.publish_task(task_id, worker_id, "测试 clarification 流程")

        # 等 worker 发 clarification
        ev = await coord.wait_for_event_type(
            "clarification_requested", timeout_sec=10
        )
        if ev is None:
            log_error("超时未收到 clarification_requested")
            return False
        log_test(f"  ✓ 协调者收到提问: {ev.get('question')}")

        # 等 worker 收到答复并继续提交
        ev = await coord.wait_for_event_type(
            "work_submitted", timeout_sec=15
        )
        if ev is None:
            log_error("Clarification 答复后,worker 没继续提交")
            return False
        log_test(f"  ✓ Worker 收到答复后完成任务并提交")

    finally:
        worker.stop()
        coord.stop()
        await asyncio.sleep(0.5)
        worker_task.cancel()
        coord_task.cancel()
        await asyncio.gather(worker_task, coord_task, return_exceptions=True)
        await coord.close()

    log_test("=" * 60)
    if success:
        log_test(" Clarification 测试通过 ✓")
    return success


async def run_blocked_test(url: str, token: str) -> bool:
    """专门测试 blocked 反馈式派单。"""
    log_test("=" * 60)
    log_test(" Blocked 反馈式派单测试")
    log_test("=" * 60)

    worker_id = f"block-test-{int(time.time())}"
    task_id = f"BLOCK-{int(time.time())}"

    worker = SimulatedWorker(
        url=url, token=token, worker_id=worker_id,
        force_blocked_reason="本机未配置工程 mobile-app",
    )
    coord = SimulatedCoordinator(
        url=url, token=token, auto_request_cleanup=False,
    )

    await coord.connect()
    worker_task = asyncio.create_task(worker.run(), name="block-worker")
    coord_task = asyncio.create_task(coord.event_loop(), name="block-coord")

    success = True
    try:
        # 等 worker 注册被协调者感知 (而不是死等 sleep)
        ev = await coord.wait_for_event_type(
            "worker_registered", timeout_sec=10
        )
        if ev is None:
            log_error("超时未收到 worker_registered (worker 注册过程异常)")
            return False

        await coord.publish_task(task_id, worker_id, "在 mobile-app 做 X")

        ev = await coord.wait_for_event_type(
            "worker_blocked", timeout_sec=10
        )
        if ev is None:
            log_error("超时未收到 worker_blocked")
            return False
        log_test(f"  ✓ 协调者收到 blocked 事件: reason={ev.get('reason')}")

    finally:
        worker.stop()
        coord.stop()
        await asyncio.sleep(0.5)
        worker_task.cancel()
        coord_task.cancel()
        await asyncio.gather(worker_task, coord_task, return_exceptions=True)
        await coord.close()

    log_test("=" * 60)
    if success:
        log_test(" Blocked 测试通过 ✓")
    return success


# ===========================================================
# 稳定性压测
# ===========================================================

import contextlib
import io
import os
from datetime import datetime
from pathlib import Path


# 测试名 → 函数的映射
_STRESS_TESTS = {
    "smoke": run_smoke_test,
    "clarification": run_clarification_test,
    "blocked": run_blocked_test,
}


@contextlib.contextmanager
def _capture_output():
    """临时把 stdout/stderr 重定向到内存,返回缓冲区。"""
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stdout, sys.stderr = old_out, old_err


async def _run_one(name: str, url: str, token: str) -> tuple[bool, str]:
    """跑一次指定的测试,返回 (是否通过, 完整输出文本)。"""
    fn = _STRESS_TESTS[name]
    with _capture_output() as buf:
        try:
            # smoke_test 多一个 timeout 参数,统一用默认值
            if name == "smoke":
                ok = await fn(url, token, timeout_sec=30)
            else:
                ok = await fn(url, token)
        except Exception as e:
            buf.write(f"\n!!! 测试函数本身抛异常: {type(e).__name__}: {e}\n")
            import traceback
            buf.write(traceback.format_exc())
            ok = False
    return ok, buf.getvalue()


async def run_stress_test(
    url: str,
    token: str,
    tests: list[str],
    iterations: int,
    log_dir: str,
) -> int:
    """循环跑指定测试,失败时把完整输出写到日志文件。

    Args:
        tests: 测试名列表 (smoke / clarification / blocked)
        iterations: 跑多少轮 (每轮跑列表里所有测试)
        log_dir: 失败日志写入目录,自动创建

    Returns:
        总失败次数
    """
    log_dir_path = Path(log_dir).expanduser()
    log_dir_path.mkdir(parents=True, exist_ok=True)

    is_infinite = iterations >= 10**8
    total_attempts = iterations * len(tests)
    pass_count = 0
    fail_count = 0
    fail_log_paths: list[Path] = []
    start_ts = time.time()

    print(f"压测开始")
    print(f"  测试: {tests}")
    if is_infinite:
        print(f"  轮数: ∞ (Ctrl+C 停止)")
    else:
        print(f"  轮数: {iterations} (共 {total_attempts} 次)")
    print(f"  失败日志: {log_dir_path}")
    print()

    try:
        for round_i in range(1, iterations + 1):
            for test_name in tests:
                attempt_idx = (round_i - 1) * len(tests) + tests.index(test_name) + 1
                ok, output = await _run_one(test_name, url, token)
                # 进度显示
                if is_infinite:
                    progress = f"[{attempt_idx}]"
                else:
                    progress = f"[{attempt_idx}/{total_attempts}]"
                if ok:
                    pass_count += 1
                    print(
                        f"  {progress} "
                        f"{test_name:<15} ✓ "
                        f"(累计 通过 {pass_count} 失败 {fail_count})"
                    )
                else:
                    fail_count += 1
                    # 写失败日志
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    log_file = log_dir_path / f"{ts}-{test_name}-round{round_i}.log"
                    log_file.write_text(output, encoding="utf-8")
                    fail_log_paths.append(log_file)
                    print(
                        f"  {progress} "
                        f"{test_name:<15} ✗ "
                        f"(累计 通过 {pass_count} 失败 {fail_count}) "
                        f"→ 日志: {log_file}"
                    )
    except KeyboardInterrupt:
        print()
        print("收到 Ctrl+C,停止压测")

    elapsed = time.time() - start_ts
    print()
    print("=" * 60)
    print(" 压测总结")
    print("=" * 60)
    print(f"  耗时:     {elapsed:.1f} 秒")
    print(f"  总尝试:   {pass_count + fail_count}")
    print(f"  通过:     {pass_count}")
    print(f"  失败:     {fail_count}")
    if pass_count + fail_count > 0:
        rate = pass_count / (pass_count + fail_count) * 100
        print(f"  通过率:   {rate:.1f}%")
    print()
    if fail_log_paths:
        print(f"失败日志 ({len(fail_log_paths)} 个) 在:")
        for p in fail_log_paths[:10]:
            print(f"  {p}")
        if len(fail_log_paths) > 10:
            print(f"  ... 还有 {len(fail_log_paths) - 10} 个")

    return fail_count



# ===========================================================
# 长期运行的模拟协调者 (供 coop simulate-coordinator 使用)
# ===========================================================

import shlex


def _parse_command(line: str) -> tuple[str, list[str]] | None:
    """解析输入行,返回 (cmd, args) 或 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        parts = shlex.split(line)
    except ValueError as e:
        log_error(f"解析命令失败: {e}")
        return None
    if not parts:
        return None
    return parts[0], parts[1:]


_HELP = """
可用命令:
  publish <worker_id> <task_id> <description...>
                            派任务给指定 worker
  publish-auto <worker_id>  派一个自动生成 task_id 的测试任务
  workers                   显示在线 worker 列表
  workers --all             显示所有 worker (含 OFFLINE)
  tasks                     显示任务列表
  tasks --status <s>        按状态过滤 (pending/in_progress/submitted/closed/...)
  events                    显示已收到的事件历史
  cleanup <task_id>         手动请求清理(submit 后任务用,通常自动)
  cancel <task_id>          取消任务
  prune                     清理 OFFLINE worker 记录
  help                      显示此帮助
  quit / exit               退出

注意: publish 命令的 description 不需要引号, 整段会被合并。
      如果 description 含特殊字符可以加引号: publish bob T-1 "hello world"
"""


async def run_simulate_coordinator(
    url: str,
    token: str,
    auto_request_cleanup: bool = True,
    auto_answer: str | None = None,
) -> None:
    """启动模拟协调者主循环。

    职责:
        - 后台 task 长期监听 wait_for_event,实时打印
        - auto_request_cleanup=True 时自动响应 work_submitted -> request_cleanup
        - auto_answer 不为 None 时自动响应 clarification_requested
        - 主循环接收 stdin 命令, 提供 publish/workers/tasks 等子命令
    """
    coord = SimulatedCoordinator(
        url=url,
        token=token,
        auto_answer_clarification=auto_answer,
        auto_request_cleanup=auto_request_cleanup,
    )

    # 后台事件循环
    event_task = asyncio.create_task(
        coord.event_loop(), name="coord-events"
    )

    # 用一个并行的 task 读 stdin (避免阻塞事件循环)
    loop = asyncio.get_event_loop()

    def _read_input(prompt: str) -> str | None:
        """同步读 stdin 一行,EOF 时返回 None。"""
        try:
            return input(prompt)
        except EOFError:
            return None

    try:
        while not coord._stop:
            try:
                line = await loop.run_in_executor(None, _read_input, "协调者> ")
            except asyncio.CancelledError:
                break
            if line is None:
                # EOF (Ctrl+D 或管道结束)
                print()
                print("(stdin 已关闭, 退出)")
                break

            parsed = _parse_command(line)
            if parsed is None:
                continue
            cmd, cmd_args = parsed

            if cmd in ("quit", "exit"):
                print("退出中...")
                break

            elif cmd == "help":
                print(_HELP)

            elif cmd == "publish":
                if len(cmd_args) < 3:
                    log_error("用法: publish <worker_id> <task_id> <description...>")
                    continue
                worker_id, task_id, *desc_parts = cmd_args
                desc = " ".join(desc_parts)
                try:
                    await coord.publish_task(task_id, worker_id, desc)
                except Exception as e:
                    log_error(f"派任务失败: {type(e).__name__}: {e}")

            elif cmd == "publish-auto":
                if not cmd_args:
                    log_error("用法: publish-auto <worker_id>")
                    continue
                worker_id = cmd_args[0]
                task_id = f"T-{int(time.time())}"
                desc = f"自动生成任务 {task_id} (测试用)"
                try:
                    await coord.publish_task(task_id, worker_id, desc)
                except Exception as e:
                    log_error(f"派任务失败: {type(e).__name__}: {e}")

            elif cmd == "workers":
                online_only = "--all" not in cmd_args
                r = await coord._call("list_workers", {
                    "online_only": online_only
                })
                if not r.get("ok"):
                    log_error(f"list_workers 失败: {r}")
                    continue
                workers = r.get("workers", [])
                if not workers:
                    print("  (无 worker)" if online_only else "  (无任何 worker 记录)")
                else:
                    print(
                        f"  {'Worker ID':<20} {'Hostname':<25} "
                        f"{'Status':<10} {'Current Task':<15}"
                    )
                    print("  " + "-" * 80)
                    for w in workers:
                        print(
                            f"  {w['worker_id']:<20} {w['hostname']:<25} "
                            f"{w['status']:<10} "
                            f"{w.get('current_task_id') or '-':<15}"
                        )

            elif cmd == "tasks":
                params: dict[str, Any] = {}
                if "--status" in cmd_args:
                    idx = cmd_args.index("--status")
                    if idx + 1 >= len(cmd_args):
                        log_error("--status 需要参数")
                        continue
                    params["status"] = cmd_args[idx + 1]
                r = await coord._call("list_tasks", params)
                if not r.get("ok"):
                    log_error(f"list_tasks 失败: {r}")
                    continue
                tasks = r.get("tasks", [])
                if not tasks:
                    print("  (无任务)")
                else:
                    print(
                        f"  {'Task ID':<15} {'Assignee':<15} "
                        f"{'Status':<12} {'Priority':<8}"
                    )
                    print("  " + "-" * 60)
                    for t in tasks:
                        print(
                            f"  {t['task_id']:<15} {t['assignee']:<15} "
                            f"{t['status']:<12} {t['priority']:<8}"
                        )

            elif cmd == "events":
                if not coord.events:
                    print("  (无事件历史)")
                else:
                    for ev in coord.events[-20:]:
                        print(
                            f"  type={ev['type']} task={ev.get('task_id')} "
                            f"worker={ev.get('worker_id')}"
                        )
                    if len(coord.events) > 20:
                        print(f"  (只显示最近 20 条,共 {len(coord.events)} 条)")

            elif cmd == "cleanup":
                if not cmd_args:
                    log_error("用法: cleanup <task_id>")
                    continue
                task_id = cmd_args[0]
                r = await coord._call("request_cleanup", {"task_id": task_id})
                if r.get("ok"):
                    log_coord(f"  → cleanup 已请求")
                else:
                    log_error(f"cleanup 失败: {r}")

            elif cmd == "cancel":
                if not cmd_args:
                    log_error("用法: cancel <task_id>")
                    continue
                task_id = cmd_args[0]
                r = await coord._call("cancel_task", {
                    "task_id": task_id,
                    "reason": "manual cancel from simulate-coordinator",
                })
                if r.get("ok"):
                    log_coord(f"  → 已取消 {task_id}")
                else:
                    log_error(f"取消失败: {r}")

            elif cmd == "prune":
                r = await coord._call("prune_offline_workers", {})
                if r.get("ok"):
                    n = r.get("deleted_count", 0)
                    log_coord(f"  → 清理了 {n} 个 OFFLINE worker")
                else:
                    log_error(f"prune 失败: {r}")

            else:
                log_error(f"未知命令: {cmd} (输入 'help' 看可用命令)")

    finally:
        # 停止事件循环
        coord.stop()
        await asyncio.sleep(0.5)
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)

        # 总结
        print()
        print(f"统计:")
        print(f"  派发任务: {coord.stats.tasks_published}")
        print(f"  收到事件: {coord.stats.events_received}")
        print(f"  自动答 clarification: {coord.stats.clarifications_answered}")
        print(f"  自动 cleanup: {coord.stats.cleanups_requested}")
