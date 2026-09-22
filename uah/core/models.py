"""UAH 状态模型 —— **全系统唯一的 Agent 状态定义**。

需求 §六（Single Source of Truth）落在这个文件上：

* ``Status`` 只在这里定义一次。UHA 那边**不许**再定义一个 ``AgentStatus``；
  UAH Desktop 那边也**不许**再定义一个。新增一个状态（例如 ``BLOCKED``）
  只需要改本文件 + ``uah/ui/components/card.py`` 的颜色表，两个宿主零改动。
* UHA 已有的 ``TaskPhase``（IDLE/ANALYZING/STRUCTURED_EXECUTION/...）**不是**重复定义：
  它是**执行阶段**，是 UHA 的内部词汇；``Status`` 是**对外存在状态**。
  两者之间的关系由 ``uah/adapters/uha/adapter.py`` 里那张唯一的映射表定义。
  把两者混为一谈，就是把 UHA 的实现细节泄进协议。

为什么状态只有这些（需求 §四 / §五）：

    语义层级必须分开，否则 HUD 只能显示一个孤零零的 "Working"：

        Agent 状态   RUNNING                    ← Status
        Task        Phase 4B / actor_move       ← TaskState.name / .phase
        Stage       环境感知 / 结构化执行        ← TaskState.stage
        Step        3 / 7                       ← TaskState.step / .total_steps
        Activity    Analyzing UE5 viewport      ← Activity.summary
        Tool        Computer Use                ← Activity.tool

    除 ``Status`` 外**所有字段都允许为空**：外部 Agent 可能只说得出一句
    "RUNNING, running tests"。UAH 不逼它编造 stage / step。
"""

from __future__ import annotations

import enum
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .protocol import PROTOCOL_VERSION

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


class Status(str, enum.Enum):
    """Agent 对外存在状态。**全系统唯一定义处。**

    ``RETRYING`` 只在协议里保留（外部 Agent 可能发它），UHA 原生适配器**从不发出**：
    UHA 的降级重试是毫秒级且密集的，把它提升成 presence 状态只会让 HUD 闪。
    重试信息降级进 ``Activity.detail``。
    """

    IDLE = "IDLE"                          # 存在，但没在干活
    STARTING = "STARTING"                  # 刚起来，还在初始化
    RUNNING = "RUNNING"                    # 正在干活
    WAITING_INPUT = "WAITING_INPUT"        # 在等人给信息
    WAITING_APPROVAL = "WAITING_APPROVAL"  # 在等人批准
    PAUSED = "PAUSED"                      # 被人暂停
    ERROR = "ERROR"                        # 出错停下
    DONE = "DONE"                          # 干完了
    CANCELLED = "CANCELLED"                # 被取消 / 中止
    RETRYING = "RETRYING"                  # 协议保留；UHA 原生适配器不发出
    BLOCKED = "BLOCKED"                    # 被卡住，必须人工介入才能继续
    UNKNOWN = "UNKNOWN"                    # 解析不出来。UI 照样显示，只是显示成"未知"

    # -- 分组 ---------------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """正在消耗资源、还没结束。"""
        return self in _ACTIVE

    @property
    def needs_human(self) -> bool:
        """必须由人来解开，否则永远卡在这。"""
        return self in _NEEDS_HUMAN

    @property
    def is_alert(self) -> bool:
        """需求 §十三 的提醒集合：从 RUNNING 变成这些之一时提醒**一次**。"""
        return self in _ALERT


_TERMINAL = frozenset({Status.DONE, Status.ERROR, Status.CANCELLED})
_ACTIVE = frozenset({Status.STARTING, Status.RUNNING, Status.RETRYING})
_NEEDS_HUMAN = frozenset({Status.WAITING_INPUT, Status.WAITING_APPROVAL, Status.BLOCKED})
_ALERT = frozenset({
    Status.DONE,
    Status.ERROR,
    Status.WAITING_INPUT,
    Status.WAITING_APPROVAL,
    Status.BLOCKED,
})

#: 解析别名。**只做加法**：以后外部 Agent 用了个新说法，往这里加一行就行，
#: 不要改上面那个枚举——改枚举 = 改协议。
_STATUS_ALIASES: dict[str, Status] = {}


def _register_alias(status: Status, *names: str) -> None:
    _STATUS_ALIASES[status.value.lower()] = status
    for n in names:
        _STATUS_ALIASES[n.lower()] = status


_register_alias(Status.IDLE, "idle", "ready", "standby", "waiting")
_register_alias(Status.STARTING, "starting", "start", "boot", "booting", "init", "initializing")
_register_alias(Status.RUNNING, "running", "run", "working", "busy", "active", "in_progress", "executing")
_register_alias(Status.WAITING_INPUT, "waiting_input", "waitinginput", "input_required",
                "needs_input", "awaiting_input", "waiting_for_input")
_register_alias(Status.WAITING_APPROVAL, "waiting_approval", "waitingapproval", "approval_required",
                "needs_approval", "awaiting_approval", "confirm", "needs_confirmation")
_register_alias(Status.PAUSED, "paused", "pause", "suspended", "on_hold")
_register_alias(Status.ERROR, "error", "failed", "fail", "failure", "errored", "crash", "crashed")
_register_alias(Status.DONE, "done", "complete", "completed", "finish", "finished", "success",
                "succeeded", "ok", "passed")
_register_alias(Status.CANCELLED, "cancelled", "canceled", "cancel", "aborted", "abort", "stopped",
                "terminated", "killed")
_register_alias(Status.RETRYING, "retrying", "retry", "recovering")
_register_alias(Status.BLOCKED, "blocked", "stuck", "stalled", "deadlocked", "waiting_on_dependency")
_register_alias(Status.UNKNOWN, "unknown", "?", "", "none", "null")


def parse_status(raw: object, *, default: Status = Status.UNKNOWN) -> Status:
    """把任意输入解析成 ``Status``。**永不抛异常**（需求 §十九）。

    ``None`` → ``default``；认不出来的字符串 → ``Status.UNKNOWN``（不是报错、不是崩溃）。
    认不出来这件事本身是有信息量的，UI 会把它显示成"未知状态"而不是假装懂了。
    """
    if isinstance(raw, Status):
        return raw
    if raw is None:
        return default
    if isinstance(raw, enum.Enum):
        raw = raw.value
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return _STATUS_ALIASES.get(key, Status.UNKNOWN)


# ---------------------------------------------------------------------------
# 小工具：宽容取字段（外部来的 JSON 什么都可能是）
# ---------------------------------------------------------------------------


def as_str(raw: object, *, max_len: int = 512) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        s = f"{raw}"
    elif isinstance(raw, str):
        s = raw
    else:
        return None
    s = s.strip()
    if not s:
        return None
    return s[:max_len]


def as_int(raw: object) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def as_float(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _synth_event_id(raw: Mapping[str, Any], agent_id: str, etype: str) -> str:
    """给没带 ``event_id`` 的载荷合成一个稳定 id。

    稳定是关键：内容相同的重复载荷必须落到同一个 id 上，否则"重复发送"这一条
    （需求 §十九）就防不住了。
    """
    try:
        blob = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 掺杂了不可序列化的对象
        blob = repr(raw)
    digest = hashlib.sha1(f"{agent_id}|{etype}|{blob}".encode("utf-8", errors="replace")).hexdigest()
    return f"synth:{digest[:16]}"


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    """按顺序取第一个**有值**的键。外部 Agent 的字段名不统一，这里一次兜住。"""
    for k in keys:
        if k in mapping:
            v = mapping[k]
            if v is not None and v != "":
                return v
    return None


# ---------------------------------------------------------------------------
# 快照的组成部件
# ---------------------------------------------------------------------------


@dataclass
class AgentRef:
    """是哪个 Agent。``id`` 是稳定标识（同一 Agent 重启后应保持一致）。"""

    id: str = "unknown"
    name: str = "unknown"
    type: str = "generic"

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.type}

    @classmethod
    def from_wire(cls, raw: object) -> "AgentRef":
        if isinstance(raw, str):
            return cls(id=raw, name=raw)
        if not isinstance(raw, Mapping):
            return cls()
        aid = as_str(_first(raw, "id", "agent_id", "agentId")) or ""
        name = as_str(_first(raw, "name", "agent", "label")) or ""
        atype = as_str(_first(raw, "type", "kind", "category")) or "generic"
        if not aid:
            aid = name or "unknown"
        if not name:
            name = aid
        return cls(id=aid, name=name, type=atype)


@dataclass
class ProjectRef:
    """在哪干活。允许整体为空。"""

    name: str | None = None
    path: str | None = None

    def to_wire(self) -> dict[str, Any] | None:
        if self.name is None and self.path is None:
            return None
        return {"name": self.name, "path": self.path}

    @classmethod
    def from_wire(cls, raw: object) -> "ProjectRef":
        if isinstance(raw, str):
            return cls(name=raw)
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            name=as_str(_first(raw, "name", "project", "title")),
            path=as_str(_first(raw, "path", "root", "dir", "file")),
        )

    @property
    def is_empty(self) -> bool:
        return self.name is None and self.path is None


@dataclass
class TaskState:
    """任务 / 阶段 / 步骤 三层语义。**每个字段都可以为空。**

    ``phase`` 与 ``stage`` 的区别（这是需求 §五 的核心）：
      * ``phase``  —— 粗粒度的大阶段，例："Phase 4B"
      * ``stage``  —— 当前正在做的那一类事，例："环境感知"
      * ``step``   —— 细粒度进度，``step``/``total_steps``，例：3 / 7
    """

    id: str | None = None
    name: str | None = None
    phase: str | None = None
    stage: str | None = None
    step: int | None = None
    total_steps: int | None = None

    def to_wire(self) -> dict[str, Any] | None:
        if self.is_empty:
            return None
        return {
            "id": self.id,
            "name": self.name,
            "phase": self.phase,
            "stage": self.stage,
            "step": self.step,
            "total_steps": self.total_steps,
        }

    @classmethod
    def from_wire(cls, raw: object) -> "TaskState":
        if isinstance(raw, str):
            return cls(name=as_str(raw))
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            id=as_str(_first(raw, "id", "task_id")),
            name=as_str(_first(raw, "name", "task", "label", "title")),
            phase=as_str(_first(raw, "phase", "milestone", "epoch")),
            stage=as_str(_first(raw, "stage", "step_name", "section", "subtask")),
            step=as_int(_first(raw, "step", "index", "current_step", "step_index")),
            total_steps=as_int(_first(raw, "total_steps", "total", "steps", "of")),
        )

    @property
    def is_empty(self) -> bool:
        return (
            self.id is None and self.name is None and self.phase is None
            and self.stage is None and self.step is None and self.total_steps is None
        )

    @property
    def progress_text(self) -> str | None:
        """``"3 / 7"``；只有 step 就 ``"3"``；都没有就 ``None``。"""
        if self.step is None:
            return None
        if self.total_steps is None:
            return str(self.step)
        return f"{self.step} / {self.total_steps}"

    @property
    def progress_ratio(self) -> float | None:
        if self.step is None or not self.total_steps or self.total_steps <= 0:
            return None
        return max(0.0, min(1.0, self.step / float(self.total_steps)))

    def merged(self, patch: "TaskState") -> "TaskState":
        """部分更新：只覆盖 patch 里**非 None** 的字段。

        这是"事件只描述变化"的兑现点。没有它，Agent 每发一次 activity 更新
        就得把整个 task 重发一遍，漏一个字段就被清空。
        """
        return TaskState(
            id=patch.id if patch.id is not None else self.id,
            name=patch.name if patch.name is not None else self.name,
            phase=patch.phase if patch.phase is not None else self.phase,
            stage=patch.stage if patch.stage is not None else self.stage,
            step=patch.step if patch.step is not None else self.step,
            total_steps=patch.total_steps if patch.total_steps is not None else self.total_steps,
        )


@dataclass
class Activity:
    """此刻具体在干什么。``tool`` 就是执行通道（UHA 里对应 ``method``）。"""

    summary: str | None = None
    detail: str | None = None
    tool: str | None = None

    def to_wire(self) -> dict[str, Any] | None:
        if self.is_empty:
            return None
        return {"summary": self.summary, "detail": self.detail, "tool": self.tool}

    @classmethod
    def from_wire(cls, raw: object) -> "Activity":
        if isinstance(raw, str):
            return cls(summary=as_str(raw))
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            summary=as_str(_first(raw, "summary", "activity", "message", "text", "title")),
            detail=as_str(_first(raw, "detail", "details", "note", "description", "log")),
            tool=as_str(_first(raw, "tool", "method", "channel", "backend")),
        )

    @property
    def is_empty(self) -> bool:
        return self.summary is None and self.detail is None and self.tool is None

    def merged(self, patch: "Activity") -> "Activity":
        return Activity(
            summary=patch.summary if patch.summary is not None else self.summary,
            detail=patch.detail if patch.detail is not None else self.detail,
            tool=patch.tool if patch.tool is not None else self.tool,
        )

    @property
    def one_line(self) -> str:
        """卡片上那一行：优先 summary，其次 detail。"""
        return self.summary or self.detail or ""


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """一条 UAH 事件。Agent 之间、Agent 与 Hub 之间的唯一通信单位。

    ``event_id`` 用于去重（需求 §十九：同一个 event 重复发送要被吸收掉）；
    ``seq`` 用于同一 Agent 内的乱序保护（``seq`` 更小的迟到事件不会把状态改回去）。
    """

    event_id: str
    type: str
    agent: AgentRef
    timestamp: float
    status: Status | None = None
    seq: int | None = None
    project: ProjectRef | None = None
    task: TaskState | None = None
    activity: Activity | None = None
    protocol: str = PROTOCOL_VERSION
    payload: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "protocol": self.protocol,
            "event_id": self.event_id,
            "type": self.type,
            "timestamp": self.timestamp,
            "agent": self.agent.to_wire(),
        }
        if self.seq is not None:
            out["seq"] = self.seq
        out["status"] = self.status.value if self.status is not None else None
        proj = self.project.to_wire() if self.project is not None else None
        if proj is not None:
            out["project"] = proj
        task = self.task.to_wire() if self.task is not None else None
        if task is not None:
            out["task"] = task
        act = self.activity.to_wire() if self.activity is not None else None
        if act is not None:
            out["activity"] = act
        if self.payload:
            out["payload"] = self.payload
        return out

    @classmethod
    def from_wire(cls, raw: object) -> "Event":
        """从任意东西里尽力构造一个 Event。

        **不抛异常**（除了完全不是对象的输入，那种情况由调用方处理）。
        缺 ``event_id`` / ``type`` 就补一个合成值——一条字段不全的事件，
        也好过让 HUD 因为一条烂数据整个不显示。
        """
        now = time.time()
        if not isinstance(raw, Mapping):
            raise ProtocolMismatch(f"event body is not a JSON object: {type(raw).__name__}")

        agent = AgentRef.from_wire(raw.get("agent"))
        etype = as_str(raw.get("type") or raw.get("event") or raw.get("kind")) or "status.changed"
        eid = as_str(raw.get("event_id") or raw.get("id") or raw.get("eventId"))
        if not eid:
            # 合成一个**内容确定性**的 id：载荷一模一样 => id 一样 => 被去重。
            # （不能拿时间戳合成 —— 那样重发同一条事件会得到新 id，去重直接失效。）
            eid = _synth_event_id(raw, agent.id, etype)
        ts = as_float(raw.get("timestamp")) or now
        # 允许毫秒时间戳（外部程序很爱发 13 位）
        if ts > 1e12:
            ts = ts / 1000.0

        # 注意：status 缺省 ≠ status 未知。缺省表示"这条事件没打算改状态"，
        # 未知表示"Agent 说了个我不认识的状态"。两者对状态机是不同的动作。
        status_raw = raw.get("status")
        status = parse_status(status_raw, default=None) if status_raw is not None else None

        payload = raw.get("payload")
        payload_d = dict(payload) if isinstance(payload, Mapping) else {}

        return cls(
            event_id=eid,
            type=etype,
            agent=agent,
            timestamp=ts,
            status=status,
            seq=as_int(raw.get("seq")),
            project=ProjectRef.from_wire(raw["project"]) if "project" in raw else None,
            task=TaskState.from_wire(raw["task"]) if "task" in raw else None,
            activity=Activity.from_wire(raw["activity"]) if "activity" in raw else None,
            protocol=as_str(raw.get("protocol")) or PROTOCOL_VERSION,
            payload=payload_d,
        )


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


@dataclass
class AgentSnapshot:
    """某个 Agent 的当前完整状态。**这就是 UI 唯一被允许读的东西。**

    两个宿主（内嵌 / 桌面）拿到的都是这个对象，或它的 ``to_wire()`` 结果。
    需求 §二十四-B（"内嵌 UI 与 Standalone HUD 使用相同状态模型"）靠这一点成立。
    """

    agent: AgentRef
    project: ProjectRef = field(default_factory=ProjectRef)
    status: Status = Status.UNKNOWN
    task: TaskState = field(default_factory=TaskState)
    activity: Activity = field(default_factory=Activity)

    #: 由 Hub 维护
    seq: int = 0
    first_seen_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_event_id: str | None = None
    last_event_type: str | None = None

    #: Agent 自报的时间戳（用于估算传输延迟）
    reported_at: float | None = None

    #: 心跳过期（Hub 计算，见 StateStore.snapshot）
    stale: bool = False
    stale_for_s: float = 0.0

    #: Agent 明确发过 ``agent.stopped``。此时立刻视为离线，但仍保留最后已知状态——
    #: "它跑在半路就没了"比"它不见了"信息量大得多。
    stopped: bool = False

    #: 序号回退被判定为"新实例"的次数。重启后 HUD 据此说明"这是重启后的第 N 次"。
    restarts: int = 0

    #: 协议兼容性。False 时 UI 照常显示，只是多一行提示——不崩。
    protocol: str = PROTOCOL_VERSION
    protocol_ok: bool = True

    #: 上一次状态变化（提醒去重要用）
    prev_status: Status | None = None
    status_changed_at: float | None = None

    #: 原始 note / 错误文本
    note: str | None = None

    # -- 派生 ---------------------------------------------------------------

    @property
    def elapsed_ms(self) -> int:
        """从"第一次看到这个 Agent"到现在的毫秒数。"""
        return int(max(0.0, self.updated_at - self.first_seen_at) * 1000)

    @property
    def idle_ms(self) -> int:
        """多久没动静了。"""
        return int(max(0.0, time.time() - self.updated_at) * 1000)

    @property
    def is_live(self) -> bool:
        return not self.stale and self.status is not Status.UNKNOWN

    def to_wire(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_ok": self.protocol_ok,
            "agent": self.agent.to_wire(),
            "project": self.project.to_wire(),
            "status": self.status.value,
            "task": self.task.to_wire(),
            "activity": self.activity.to_wire(),
            "seq": self.seq,
            "first_seen_at": self.first_seen_at,
            "updated_at": self.updated_at,
            "elapsed_ms": self.elapsed_ms,
            "idle_ms": self.idle_ms,
            "stale": self.stale,
            "stale_for_s": round(self.stale_for_s, 3),
            "stopped": self.stopped,
            "restarts": self.restarts,
            "last_event_id": self.last_event_id,
            "last_event_type": self.last_event_type,
            "prev_status": self.prev_status.value if self.prev_status is not None else None,
            "status_changed_at": self.status_changed_at,
            "note": self.note,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "AgentSnapshot":
        snap = cls(agent=AgentRef.from_wire(raw.get("agent")))
        snap.project = ProjectRef.from_wire(raw.get("project"))
        snap.status = parse_status(raw.get("status"))
        snap.task = TaskState.from_wire(raw.get("task"))
        snap.activity = Activity.from_wire(raw.get("activity"))
        snap.seq = as_int(raw.get("seq")) or 0
        snap.first_seen_at = as_float(raw.get("first_seen_at")) or time.time()
        snap.updated_at = as_float(raw.get("updated_at")) or time.time()
        snap.last_event_id = as_str(raw.get("last_event_id"))
        snap.last_event_type = as_str(raw.get("last_event_type"))
        snap.stale = bool(raw.get("stale"))
        snap.stale_for_s = as_float(raw.get("stale_for_s")) or 0.0
        snap.stopped = bool(raw.get("stopped"))
        snap.restarts = as_int(raw.get("restarts")) or 0
        snap.protocol = as_str(raw.get("protocol")) or PROTOCOL_VERSION
        snap.protocol_ok = bool(raw.get("protocol_ok", True))
        prev = raw.get("prev_status")
        snap.prev_status = parse_status(prev) if prev is not None else None
        snap.status_changed_at = as_float(raw.get("status_changed_at"))
        snap.note = as_str(raw.get("note"))
        return snap

    def summary_line(self) -> str:
        """一行纯文本摘要。文本 HUD、日志、测试断言都用它，保证三处一致。"""
        parts = [f"{self.agent.name}", self.status.value]
        if self.task.phase:
            parts.append(f"phase={self.task.phase}")
        if self.task.stage:
            parts.append(f"stage={self.task.stage}")
        prog = self.task.progress_text
        if prog:
            parts.append(f"step={prog}")
        act = self.activity.one_line
        if act:
            parts.append(f"activity={act}")
        if self.activity.tool:
            parts.append(f"tool={self.activity.tool}")
        if self.stale:
            parts.append(f"STALE({self.stale_for_s:.0f}s)")
        return " | ".join(parts)


__all__ = [
    "Status",
    "parse_status",
    "as_str",
    "as_int",
    "as_float",
    "AgentRef",
    "ProjectRef",
    "TaskState",
    "Activity",
    "Event",
    "AgentSnapshot",
]
