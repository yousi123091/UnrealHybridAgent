"""Generic Adapter —— 最简接入面。

需求 §十 要的就是这个：外部程序能发最少量信息，UAH 就能显示。

外部只需要说得出这类东西：

    {"agent": "ExampleAgent", "status": "running", "task": "Running tests", "activity": "pytest"}

几处刻意的宽容（都是"让接入方少写代码"）：

* ``agent`` 可以是字符串（就当成名字），也可以是对象。
* ``task`` / ``activity`` 可以是字符串（当成 name / summary），也可以是对象。
* 缺 ``protocol`` / ``event_id`` / ``timestamp`` / ``type`` 就由这里补齐。
* ``status`` 认不出来时**不报错**：交给状态机记成 ``UNKNOWN`` 并显示出来。

但有一条**不宽容**：绝不替外部编造它没说的 stage / step / tool。
"我不知道"显示成空，比显示一个编出来的值好。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ...core.models import Status, parse_status
from ...core.protocol import (
    PROTOCOL_VERSION,
    EV_ACTIVITY_CHANGED,
    EV_AGENT_STARTED,
    EV_AGENT_STOPPED,
    EV_APPROVAL_REQUIRED,
    EV_STATUS_CHANGED,
    EV_TASK_COMPLETED,
    EV_TASK_FAILED,
    EV_USER_INPUT_REQUIRED,
)
from ...core.transport import HubClient, new_event_id
from ..publisher import HttpPublisher, Publisher

#: 状态 → 最贴切的事件类型。外部程序不想指定 ``type`` 时用这个推导。
STATUS_DEFAULT_EVENT: dict[Status, str] = {
    Status.STARTING: EV_AGENT_STARTED,
    Status.IDLE: EV_STATUS_CHANGED,
    Status.RUNNING: EV_STATUS_CHANGED,
    Status.WAITING_INPUT: EV_USER_INPUT_REQUIRED,
    Status.WAITING_APPROVAL: EV_APPROVAL_REQUIRED,
    Status.PAUSED: EV_STATUS_CHANGED,
    Status.ERROR: EV_TASK_FAILED,
    Status.DONE: EV_TASK_COMPLETED,
    Status.CANCELLED: EV_AGENT_STOPPED,
    Status.RETRYING: EV_ACTIVITY_CHANGED,
    Status.BLOCKED: EV_STATUS_CHANGED,
    Status.UNKNOWN: EV_STATUS_CHANGED,
}


def normalize_event(
    raw: Mapping[str, Any] | None = None,
    *,
    agent: Any = None,
    status: Any = None,
    task: Any = None,
    activity: Any = None,
    detail: Any = None,
    tool: Any = None,
    project: Any = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    """把"随便什么形状"的输入整形成一条规范 UAH 事件（dict）。

    既支持 ``normalize_event({"agent": "X", "status": "running"})``，
    也支持 ``normalize_event(agent="X", status="running")``。
    """
    src: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}

    def _pick(kw: Any, *keys: str) -> Any:
        if kw is not None:
            return kw
        for k in keys:
            if k in src and src[k] is not None:
                return src[k]
        return None

    agent_v = _pick(agent, "agent", "agent_id", "name")
    status_v = _pick(status, "status", "state")
    task_v = _pick(task, "task", "task_name")
    activity_v = _pick(activity, "activity", "message", "summary")
    detail_v = _pick(detail, "detail", "details", "note")
    tool_v = _pick(tool, "tool", "method")
    project_v = _pick(project, "project")

    parsed = parse_status(status_v, default=Status.UNKNOWN) if status_v is not None else None
    etype = (
        str(event_type)
        if event_type
        else str(src.get("type") or src.get("event") or "")
        or (STATUS_DEFAULT_EVENT.get(parsed, EV_STATUS_CHANGED) if parsed is not None else EV_STATUS_CHANGED)
    )

    event: dict[str, Any] = {
        "protocol": str(src.get("protocol") or PROTOCOL_VERSION),
        "event_id": str(src.get("event_id") or src.get("id") or new_event_id()),
        "type": etype,
        "timestamp": src.get("timestamp") or time.time(),
        "agent": agent_v if not isinstance(agent_v, (str, type(None))) else {"name": agent_v or "unknown"},
    }
    if parsed is not None:
        event["status"] = parsed.value

    # task：字符串 -> name；对象直接传下去（TaskState.from_wire 会宽容解析）
    if task_v is not None:
        event["task"] = task_v if not isinstance(task_v, str) else {"name": task_v}
    if activity_v is not None or detail_v is not None or tool_v is not None:
        act: dict[str, Any] = {}
        if activity_v is not None:
            if isinstance(activity_v, Mapping):
                act.update({k: v for k, v in activity_v.items() if k in ("summary", "detail", "tool")})
            else:
                act["summary"] = activity_v
        if detail_v is not None:
            act["detail"] = detail_v
        if tool_v is not None:
            act["tool"] = tool_v
        if act:
            event["activity"] = act
    if project_v is not None:
        event["project"] = project_v if not isinstance(project_v, str) else {"name": project_v}

    # 把外部多带的字段原样留在 payload 里，别丢
    known = {
        "protocol", "event_id", "id", "type", "event", "timestamp", "agent", "agent_id",
        "name", "status", "state", "task", "task_name", "activity", "message", "summary",
        "detail", "details", "note", "tool", "method", "project",
    }
    extra = {k: v for k, v in src.items() if k not in known}
    if extra:
        event["payload"] = extra
    return event


class GenericBridge:
    """外部 Agent 的接入桥。

        bridge = GenericBridge("http://127.0.0.1:8789")
        bridge.emit(agent="ExampleAgent", status="running", task="Running tests", activity="pytest")
        bridge.emit(agent="ExampleAgent", status="done")
    """

    def __init__(self, target: str | Publisher = "http://127.0.0.1:8789") -> None:
        if isinstance(target, str):
            self.publisher: Publisher = HttpPublisher(target)
            self.url = target
        else:
            self.publisher = target
            self.url = getattr(target, "url", "")
        self._sent = 0

    def emit(
        self,
        raw: Mapping[str, Any] | None = None,
        *,
        agent: Any = None,
        status: Any = None,
        task: Any = None,
        activity: Any = None,
        detail: Any = None,
        tool: Any = None,
        project: Any = None,
        event_type: str | None = None,
    ) -> bool:
        event = normalize_event(
            raw, agent=agent, status=status, task=task, activity=activity,
            detail=detail, tool=tool, project=project, event_type=event_type,
        )
        ok = self.publisher.publish(event)
        if ok:
            self._sent += 1
        return ok

    def stats(self) -> dict[str, Any]:
        return {"url": self.url, "sent": self._sent, **self.publisher.delivered()}


def emit_event(
    url: str = "http://127.0.0.1:8789",
    *,
    agent: Any = None,
    status: Any = None,
    task: Any = None,
    activity: Any = None,
    detail: Any = None,
    tool: Any = None,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    """一行接入：发一条事件并返回 Hub 的应答（测试与脚本用）。"""
    event = normalize_event(
        agent=agent, status=status, task=task, activity=activity, detail=detail, tool=tool
    )
    return HubClient(url, timeout_s=timeout_s).post_event(event)


__all__ = ["GenericBridge", "normalize_event", "emit_event", "STATUS_DEFAULT_EVENT"]
