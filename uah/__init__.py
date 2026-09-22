"""UAH —— Universal Agent HUD。

一个通用的 Agent Presence / Status 层：把"Agent 现在正在做什么、做到哪个阶段、
是否在等人、是否需要审批、是否出错、是否完成"以**统一协议**暴露给 UI。

分层（自上而下只有一个方向）：

    Agent Core
       │  （Adapter：把某个 Agent 的私有状态翻译成 UAH 事件）
       ▼
    uah.core.protocol  +  uah.core.models      ← 协议与状态定义（全系统唯一定义处）
       │
    uah.core.state.StateStore                  ← 唯一状态机（事件 → 快照）
       │
    uah.core.transport                         ← 本机传输（127.0.0.1 HTTP + SSE）
       │
    ├─ uah.hosts.embedded    （Agent 进程内）
    └─ uah.hosts.desktop     （独立 Windows 进程）

设计约束（写在这里，免得以后忘）：

* **只用标准库**。不引入 websocket / fastapi / redis / 数据库 / MQ。
* **状态定义只有一份**：``uah.core.models.Status``。宿主、适配器、UHA 都不许再定义一份。
* **协议版本与实现版本分离**：``protocol`` 是 ``uah/1``；core / desktop 各自独立版本号。
* **UI 是订阅者，不是 Agent 主逻辑的一部分**。Agent 挂了，UI 只看到 EOF，不跟着挂。
"""

from __future__ import annotations

from .core.protocol import (  # noqa: F401
    PROTOCOL_VERSION,
    PROTOCOL_MAJOR,
    UAH_CORE_VERSION,
    ProtocolMismatch,
    protocol_compatible,
)

__all__ = [
    "PROTOCOL_VERSION",
    "PROTOCOL_MAJOR",
    "UAH_CORE_VERSION",
    "ProtocolMismatch",
    "protocol_compatible",
]
