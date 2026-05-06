"""数据模型 - dataclass 定义和状态枚举。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow_iso() -> str:
    """统一的时间戳格式 (UTC ISO 8601)。"""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    """解析 ISO 时间戳,容错处理。"""
    # Python 3.11+ fromisoformat 已支持完整 ISO 8601
    return datetime.fromisoformat(s)


class WorkerStatus(str, Enum):
    """worker 生命周期状态。"""
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    OFFLINE = "offline"


class TaskStatus(str, Enum):
    """任务生命周期状态。

    pending  -> 等待 worker 拉取
    in_progress -> worker 正在干
    submitted -> worker 已提交,等协调者 review
    cleaning -> 协调者已下发清理指令,等 worker 回执
    closed -> worker 已 acknowledge_cleanup,任务彻底结束
    abandoned -> worker 失联,任务搁置
    cancelled -> 协调者主动取消
    """
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    CLEANING = "cleaning"
    CLOSED = "closed"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


# 协调者关心的事件类型
class EventType(str, Enum):
    WORKER_REGISTERED = "worker_registered"
    WORKER_OFFLINE = "worker_offline"
    WORK_SUBMITTED = "work_submitted"
    WORKER_BLOCKED = "worker_blocked"
    PROGRESS = "progress"
    CLARIFICATION_REQUESTED = "clarification_requested"
    CLEANUP_DONE = "cleanup_done"
    TASK_ABANDONED = "task_abandoned"


@dataclass
class Worker:
    worker_id: str
    hostname: str
    status: WorkerStatus
    current_task_id: str | None
    registered_at: str  # ISO timestamp
    last_heartbeat: str  # ISO timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "hostname": self.hostname,
            "status": self.status.value,
            "current_task_id": self.current_task_id,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
        }


@dataclass
class Task:
    task_id: str
    assignee: str
    description: str
    priority: str  # high / normal / low
    parent_task_id: str | None
    depends_on: list[str]
    status: TaskStatus
    dispatched_from: str  # 派单方,通常是 'coordinator'
    dispatched_at: str
    # 提交后回填的字段
    submitted_summary: str | None = None
    submitted_artifact: dict[str, Any] | None = None  # 自由结构, server 不解析
    submitted_at: str | None = None
    # 取消/失败原因
    cancel_reason: str | None = None
    abandon_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "task_id": self.task_id,
            "assignee": self.assignee,
            "description": self.description,
            "priority": self.priority,
            "parent_task_id": self.parent_task_id,
            "depends_on": list(self.depends_on),
            "status": self.status.value,
            "from": self.dispatched_from,
            "dispatched_at": self.dispatched_at,
        }
        if self.submitted_at:
            d["submission"] = {
                "summary": self.submitted_summary,
                "artifact": self.submitted_artifact,
                "submitted_at": self.submitted_at,
            }
        if self.cancel_reason:
            d["cancel_reason"] = self.cancel_reason
        if self.abandon_reason:
            d["abandon_reason"] = self.abandon_reason
        return d


@dataclass
class Event:
    """协调者会读取的事件流条目。"""
    event_id: int  # 自增 ID,用作 cursor
    event_type: EventType
    task_id: str | None
    worker_id: str | None
    payload: dict[str, Any]  # 扁平化关键信息,序列化到 DB 时是 JSON
    created_at: str
    consumed: bool  # 协调者通过 wait_for_event 取走后置 true

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.event_type.value,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "created_at": self.created_at,
            **self.payload,  # 扁平化,方便协调者使用
        }


@dataclass
class Clarification:
    """worker 的提问与协调者的答复。"""
    task_id: str
    worker_id: str
    question: str
    answer: str | None
    created_at: str
    answered_at: str | None
