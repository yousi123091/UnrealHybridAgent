"""UAH Protocol v1 —— 版本、事件类型、兼容性判定。

协议是 UAH 的**契约**，UI 和状态模型都只是它的消费者。所以这个文件很无聊，
这是好事：协议一旦开始"聪明"，两端就会开始各自解释它。

版本策略（需求 §七）：

    UAH Protocol  1.x     ← 线上格式。主版本不兼容，次版本只加不删
    UAH Core      0.x     ← 本仓库核心实现
    UAH Desktop   0.x     ← 桌面宿主
    UHA           x.x     ← 被观察的 Agent（与本协议版本无关）

目标：`UHA 0.8` 与 `UAH Desktop 1.4` 只要都认 `uah/1`，就能通信。
所以**任何地方都不许**把 desktop 的版本号和 protocol 的版本号绑在一起比较。
"""

from __future__ import annotations

import re

#: 线上格式版本。改主版本 = 破坏性变更；改次版本 = 只新增字段。
PROTOCOL_VERSION = "uah/1"
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0

#: 本仓库各部件自己的版本，独立演进，**不与协议版本绑定**。
UAH_CORE_VERSION = "0.1.0"
UAH_DESKTOP_VERSION = "0.1.0"
UAH_EMBEDDED_VERSION = "0.1.0"

#: 本机默认端口。固定端口 + "先到先得"的 Hub 选举，用户不需要手动启动任何服务。
DEFAULT_HUB_HOST = "127.0.0.1"
DEFAULT_HUB_PORT = 8789

#: SSE 心跳间隔（秒）。客户端读超时设得比它大即可发现服务端已死。
HEARTBEAT_INTERVAL_S = 15.0


# --- 事件类型 -----------------------------------------------------------------
#
# 名字不神圣（需求 §九明说"不要求一定用这些名字"），但**这些字符串是线上格式**，
# 所以要改就得走协议次版本。用 `分类.动作` 的形状，方便订阅方按前缀过滤。

EV_AGENT_STARTED = "agent.started"
EV_AGENT_STOPPED = "agent.stopped"
EV_AGENT_HEARTBEAT = "agent.heartbeat"

EV_TASK_STARTED = "task.started"
EV_TASK_STAGE_CHANGED = "task.stage_changed"
EV_TASK_STEP_CHANGED = "task.step_changed"
EV_TASK_COMPLETED = "task.completed"
EV_TASK_FAILED = "task.failed"
EV_TASK_CANCELLED = "task.cancelled"

EV_ACTIVITY_CHANGED = "activity.changed"

EV_APPROVAL_REQUIRED = "approval.required"
EV_USER_INPUT_REQUIRED = "user_input.required"

#: 万能兜底事件。通用适配器只想说"我现在是 RUNNING"时用它，
#: 不必假装自己理解 task/approval 的全部语义。
EV_STATUS_CHANGED = "status.changed"

ALL_EVENT_TYPES = (
    EV_AGENT_STARTED,
    EV_AGENT_STOPPED,
    EV_AGENT_HEARTBEAT,
    EV_TASK_STARTED,
    EV_TASK_STAGE_CHANGED,
    EV_TASK_STEP_CHANGED,
    EV_TASK_COMPLETED,
    EV_TASK_FAILED,
    EV_TASK_CANCELLED,
    EV_ACTIVITY_CHANGED,
    EV_APPROVAL_REQUIRED,
    EV_USER_INPUT_REQUIRED,
    EV_STATUS_CHANGED,
)

#: 未知事件类型不是错误：Hub 照收不误（记进快照的 note），只是不触发特殊语义。
#: 这样以后加事件类型时，老版本 Hub 不会把新版本的 Agent 判成"非法"。
_UNKNOWN_EVENT_TYPE = "<unknown>"


class ProtocolMismatch(Exception):
    """协议主版本不兼容。

    只在**显式校验**时抛出（例如宿主启动时自检）。
    运行期的摄入路径**不抛**——需求 §十九 要求"旧版 protocol / 未知状态不能让 UI 崩"，
    所以 `StateStore` 收到不兼容事件时是"收下并标记"，不是"拒绝并崩溃"。
    """

    def __init__(self, got: str, expected: str = PROTOCOL_VERSION):
        super().__init__(f"protocol mismatch: got {got!r}, expected {expected!r}")
        self.got = got
        self.expected = expected


_VER_RE = re.compile(r"^\s*([A-Za-z_]+)\s*/\s*(\d+)(?:\.(\d+))?\s*$")


def parse_version(raw: object) -> tuple[str, int, int] | None:
    """``"uah/1"`` / ``"uah/1.2"`` → ``("uah", 1, 2)``；解析不了返回 ``None``。

    故意宽容：``"uah"``、``"UAH/1"``、``"uah/1.0.3"`` 都能解析出来。
    协议字符串上的小差异不值得让一个 HUD 罢工。
    """
    if raw is None:
        return None
    m = _VER_RE.match(str(raw))
    if not m:
        return None
    family = m.group(1).lower()
    major = int(m.group(2))
    minor = int(m.group(3) or 0)
    return family, major, minor


def protocol_compatible(raw: object, *, expected_major: int = PROTOCOL_MAJOR) -> bool:
    """只比较**主版本**。次版本差异不算不兼容（次版本只加不删）。"""
    parsed = parse_version(raw)
    if parsed is None:
        return False
    family, major, _minor = parsed
    return family == "uah" and major == expected_major


__all__ = [
    "PROTOCOL_VERSION",
    "PROTOCOL_MAJOR",
    "PROTOCOL_MINOR",
    "UAH_CORE_VERSION",
    "UAH_DESKTOP_VERSION",
    "UAH_EMBEDDED_VERSION",
    "DEFAULT_HUB_HOST",
    "DEFAULT_HUB_PORT",
    "HEARTBEAT_INTERVAL_S",
    "EV_AGENT_STARTED",
    "EV_AGENT_STOPPED",
    "EV_AGENT_HEARTBEAT",
    "EV_TASK_STARTED",
    "EV_TASK_STAGE_CHANGED",
    "EV_TASK_STEP_CHANGED",
    "EV_TASK_COMPLETED",
    "EV_TASK_FAILED",
    "EV_TASK_CANCELLED",
    "EV_ACTIVITY_CHANGED",
    "EV_APPROVAL_REQUIRED",
    "EV_USER_INPUT_REQUIRED",
    "EV_STATUS_CHANGED",
    "ALL_EVENT_TYPES",
    "ProtocolMismatch",
    "parse_version",
    "protocol_compatible",
]
