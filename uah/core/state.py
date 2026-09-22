"""StateStore —— UAH 的**唯一状态机**。

一切状态变化都必须经过 ``apply(event)``。UI 不许自己算状态，适配器不许自己存状态。

三条硬约束（需求 §六 / §九 / §十九）：

1. **单一真源**：状态只有这一个地方。内嵌宿主和桌面宿主读的是同一套 ``AgentSnapshot``。
2. **事件驱动**：Agent 主动发事件，UI 不反过来猜。没有轮询文件的路径。
3. **一条烂数据不能让 UI 崩**：非法 JSON / 未知状态 / 重复事件 / 旧版协议 / 乱序，
   全部在这里被吸收并**如实标记**，不上抛异常。

关于"重复事件"：按 ``event_id`` 去重（保留最近 ``dedupe_window`` 条 id）。
关于"乱序"：同一 Agent 内，``seq`` 比当前小的迟到事件**不改状态**，只更新心跳时间——
否则一条晚到的旧事件会把已经推进的 step 3 打回 step 2，而 UI 无法察觉。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import AgentRef, AgentSnapshot, Event, Status, parse_status
from .protocol import (
    EV_AGENT_STARTED,
    EV_AGENT_STOPPED,
    EV_APPROVAL_REQUIRED,
    EV_USER_INPUT_REQUIRED,
    PROTOCOL_VERSION,
    protocol_compatible,
)

#: 多久没收到任何事件就算"过期"。UHA 的任务之间可能长时间 IDLE，
#: 但适配器会发心跳，所以这个值只用于"Agent 进程是不是死了"的判断。
DEFAULT_STALE_AFTER_S = 20.0
#: 过期多久之后从列表里移除。给得比 stale 宽松，好让 UI 有机会显示"这个 Agent 掉线了"。
DEFAULT_EXPIRE_AFTER_S = 300.0
#: 去重窗口（保留多少条 event_id）。
DEFAULT_DEDUPE_WINDOW = 2048


@dataclass
class ApplyOutcome:
    """``apply()`` 的结果。**不抛异常**，一切用这个对象表达。"""

    accepted: bool
    reason: str = ""
    snapshot: AgentSnapshot | None = None
    prev_status: Status | None = None
    status_changed: bool = False
    duplicate: bool = False
    stale_seq: bool = False
    changed: tuple[str, ...] = ()
    protocol_ok: bool = True

    @property
    def is_new_agent(self) -> bool:
        return self.accepted and not self.duplicate and "status" in self.changed and self.prev_status is None

    def describe(self) -> str:
        if not self.accepted:
            return f"rejected: {self.reason}"
        if self.duplicate:
            return f"duplicate ignored ({self.reason})"
        if self.stale_seq:
            return f"stale seq ignored ({self.reason})"
        return "applied: " + ",".join(self.changed)


#: 卡片排序：需要人看的排前面。HUD 与 Hub 都用这一份，两边顺序不会不一致。
_STATUS_RANK: dict[Status, int] = {
    Status.WAITING_APPROVAL: 0,
    Status.WAITING_INPUT: 1,
    Status.BLOCKED: 2,
    Status.ERROR: 3,
    Status.RUNNING: 4,
    Status.RETRYING: 5,
    Status.STARTING: 6,
    Status.PAUSED: 7,
    Status.IDLE: 8,
    Status.DONE: 9,
    Status.CANCELLED: 10,
    Status.UNKNOWN: 11,
}


def _sort_snapshots(items: list[AgentSnapshot]) -> list[AgentSnapshot]:
    items.sort(key=lambda s: (_STATUS_RANK.get(s.status, 99), -s.updated_at))
    return items


class StateStore:
    """事件 → 快照。线程安全。"""

    def __init__(
        self,
        *,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        expire_after_s: float = DEFAULT_EXPIRE_AFTER_S,
        dedupe_window: int = DEFAULT_DEDUPE_WINDOW,
        clock: Any = time.time,
    ) -> None:
        self.stale_after_s = float(stale_after_s)
        self.expire_after_s = float(expire_after_s)
        self.dedupe_window = int(dedupe_window)
        self._clock = clock

        self._lock = threading.RLock()
        self._agents: dict[str, AgentSnapshot] = {}
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._rejected = 0
        self._duplicates = 0
        self._stale_seq = 0
        #: 每次成功 apply 计数 +1，用于 SSE 端做"有没有变化"的廉价比较
        self.revision = 0

    # -- 摄入 ---------------------------------------------------------------

    def apply(self, raw: object) -> ApplyOutcome:
        """摄入一条事件。接受 dict / Event / 任何东西。

        **这个方法永不抛异常。** 这是需求 §十九 的落点。
        """
        now = self._clock()

        # 1) 变成 Event
        try:
            event = raw if isinstance(raw, Event) else Event.from_wire(raw)
        except Exception as exc:  # noqa: BLE001 - 非法载荷必须被吸收
            with self._lock:
                self._rejected += 1
            return ApplyOutcome(accepted=False, reason=f"{type(exc).__name__}: {exc}")

        # 2) 协议兼容性：不兼容也收，但标记出来（UI 显示提示，不崩）
        ok_proto = protocol_compatible(event.protocol)

        with self._lock:
            agent_id = event.agent.id or "unknown"
            etype = event.type

            # 3) 去重
            if event.event_id in self._seen:
                self._seen.move_to_end(event.event_id)
                self._duplicates += 1
                return ApplyOutcome(
                    accepted=True, duplicate=True, reason=f"event_id={event.event_id}",
                    snapshot=self._agents.get(agent_id), protocol_ok=ok_proto,
                )

            # 4) 乱序保护
            #
            # 只挡"**又旧又迟到**"的事件。判定顺序（宁可放过，不可错杀）：
            #
            #   序号没变小                        → 不是迟到，放行
            #   event 类型是 agent.started        → Agent 明说"我起来了"，放行
            #   没有可比的时间戳                  → 放行
            #   时间戳严格更早                    → 真的迟到，挡下
            #   序号恰好回到 1（且时间戳不更早）  → 计数器从头开始，是新实例，放行
            #
            # 为什么必须放行"序号回到 1"：Agent 重启后序号是从头数的。
            # 如果只认 `agent.started`，那么一个没发 start 事件就说话的实例
            # （外部 Agent、被中途接管的进程、并发跑的两个实例）会被**整段压制**，
            # 而 HUD 会一直显示崩溃前那一刻的状态 —— 一个不会报错的哑故障。
            # 这正是本项目最忌讳的"假绿"形态，所以这里刻意宽松。
            prev = self._agents.get(agent_id)
            lower_seq = (
                prev is not None and event.seq is not None and prev.seq and event.seq < prev.seq
            )
            if lower_seq and prev is not None:
                if etype == EV_AGENT_STARTED:
                    restart_like = True
                elif not prev.reported_at:
                    restart_like = True
                elif event.timestamp < prev.reported_at - 1e-3:
                    restart_like = False          # 又旧又晚：真迟到
                else:
                    restart_like = event.seq == 1  # 新实例的计数器
            else:
                restart_like = False
            if lower_seq and not restart_like:
                self._stale_seq += 1
                self._remember(event.event_id)
                # 心跳时间仍然推进：这条事件"旧"，但证明 Agent 还活着
                prev.updated_at = now
                return ApplyOutcome(
                    accepted=True, stale_seq=True,
                    reason=f"seq={event.seq} < {prev.seq} 且时间戳更早", snapshot=prev,
                    protocol_ok=ok_proto,
                )

            is_new = prev is None
            snap = prev if prev is not None else AgentSnapshot(agent=event.agent, first_seen_at=now)
            changed: list[str] = []

            # 5) 身份 / 项目
            #
            # 关键细节：部分事件只带 ``{"agent": {"id": "uha"}}``。此时
            # ``AgentRef.name`` 会被**合成**成 id，如果直接覆盖，第二次事件就会把
            # 已知的漂亮名字 "UHA" 冲成 "uha"。所以只有"提供了比 id 更具体的名字"
            # 或"提供了非 generic 的类型"时才更新身份。
            if is_new:
                snap.agent = event.agent
                changed.append("agent")
            else:
                incoming = event.agent
                new_name = snap.agent.name
                if incoming.name and incoming.name != incoming.id and incoming.name != new_name:
                    new_name = incoming.name
                new_type = snap.agent.type
                if incoming.type and incoming.type not in ("generic", snap.agent.type):
                    new_type = incoming.type
                if (new_name, new_type) != (snap.agent.name, snap.agent.type):
                    snap.agent = AgentRef(id=snap.agent.id, name=new_name, type=new_type)
                    changed.append("agent")
            if event.project is not None and not event.project.is_empty:
                snap.project = event.project
                changed.append("project")

            # 6) 状态
            prev_status = snap.status if not is_new else None
            status_changed = False
            if event.status is not None:
                if is_new or event.status is not snap.status:
                    snap.prev_status = None if is_new else snap.status
                    snap.status = event.status
                    snap.status_changed_at = event.timestamp or now
                    status_changed = True
                    changed.append("status")

            # 7) task / activity：部分更新
            if event.task is not None and not event.task.is_empty:
                merged = snap.task.merged(event.task)
                if merged != snap.task:
                    snap.task = merged
                    changed.append("task")
            if event.activity is not None and not event.activity.is_empty:
                merged_a = snap.activity.merged(event.activity)
                if merged_a != snap.activity:
                    snap.activity = merged_a
                    changed.append("activity")

            # 8) 事件类型带来的语义（非状态变化类事件）
            if etype == EV_APPROVAL_REQUIRED and not status_changed and snap.status.is_active:
                snap.prev_status = snap.status
                snap.status = Status.WAITING_APPROVAL
                snap.status_changed_at = event.timestamp or now
                status_changed = True
                changed.append("status")
            elif etype == EV_USER_INPUT_REQUIRED and not status_changed and snap.status.is_active:
                snap.prev_status = snap.status
                snap.status = Status.WAITING_INPUT
                snap.status_changed_at = event.timestamp or now
                status_changed = True
                changed.append("status")
            elif etype == EV_AGENT_STOPPED:
                # 明确说了"我停了"：不动状态，只标记离线。
                # 保留最后已知状态比改成 CANCELLED 有用得多——
                # "RUNNING 但已离线" = 半路死掉；"DONE 且离线" = 正常收工。
                snap.stopped = True
                changed.append("stopped")

            if is_new and "status" not in changed:
                # 从没见过的 Agent 只发了 activity：给一个 STARTING，别显示成"未知"
                snap.status = Status.STARTING
                snap.status_changed_at = event.timestamp or now
                changed.append("status")

            # 9) 记帐
            if lower_seq:
                # 序号回退 = 这是重启后的新实例，号段从头开始：跟着它重置，
                # 否则后续的保护会一直拿一个过期的基准去比较。
                snap.seq = event.seq or 1
                if prev is not None and snap.restarts == prev.restarts:
                    snap.restarts = prev.restarts + 1
            else:
                snap.seq = max(snap.seq, event.seq or 0) or snap.seq + 1
            snap.updated_at = now
            snap.reported_at = event.timestamp
            snap.last_event_id = event.event_id
            snap.last_event_type = etype
            snap.protocol = event.protocol
            snap.protocol_ok = ok_proto
            snap.stale = False
            snap.stale_for_s = 0.0
            if etype != EV_AGENT_STOPPED:
                # 又收到事件了 => 它没停（可能是重启后复用了同一个 agent id）
                snap.stopped = False
            if not ok_proto:
                snap.note = f"协议 {event.protocol} 与 {PROTOCOL_VERSION} 主版本不兼容（只读显示）"
            elif event.payload.get("note"):
                snap.note = str(event.payload["note"])[:512]

            self._agents[agent_id] = snap
            self._remember(event.event_id)
            self.revision += 1

            return ApplyOutcome(
                accepted=True,
                prev_status=prev_status,
                status_changed=status_changed,
                changed=tuple(changed),
                snapshot=snap,
                protocol_ok=ok_proto,
                reason="ok",
            )

    def apply_many(self, raws: Iterable[object]) -> list[ApplyOutcome]:
        return [self.apply(r) for r in raws]

    def _remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        while len(self._seen) > self.dedupe_window:
            self._seen.popitem(last=False)

    # -- 读取 ---------------------------------------------------------------

    def _with_staleness(self, snap: AgentSnapshot, now: float) -> AgentSnapshot:
        age = max(0.0, now - snap.updated_at)
        snap.stale_for_s = age
        # 已结束的 Agent 不会"过期"—— 一个跑完的 DONE 卡片永远该显示 DONE。
        # 但它"离线"这件事仍然要说：Agent 发过 agent.stopped 就算离线。
        if snap.status.is_terminal:
            snap.stale = False
        else:
            snap.stale = snap.stopped or age > self.stale_after_s
        return snap

    def snapshot(self, agent_id: str) -> AgentSnapshot | None:
        with self._lock:
            snap = self._agents.get(agent_id)
            if snap is None:
                return None
            return self._with_staleness(snap, self._clock())

    def snapshots(self, *, include_expired: bool = False) -> list[AgentSnapshot]:
        """全部快照，按"需要注意的排前面"排序。

        排序规则刻意面向"我该看谁"：需要人介入的 > 出错的 > 正在跑的 > 其它的。
        HUD 直接照这个顺序画卡片，不需要自己再排一遍。

        注意：返回的是**存储里的对象本身**（不是拷贝）。只读使用没问题，
        但如果要把"快照内容"和"判断它有没有变"一起用，必须用
        ``wire_snapshots()`` —— 见那里的说明。
        """
        now = self._clock()
        with self._lock:
            items = [
                self._with_staleness(s, now)
                for s in self._agents.values()
                if include_expired or (now - s.updated_at) <= self.expire_after_s
            ]
        return _sort_snapshots(items)

    def wire_snapshots(self, *, include_expired: bool = False) -> list[dict[str, Any]]:
        """快照的线上形态（dict），**序列化与过期计算都在同一把锁内**。

        为什么必须是原子的一步：SSE 的增量逻辑要同时做两件事 ——
        把当前快照发给新订阅者，并记下它的"签名"用于下次比较。
        如果分两步（先读对象、再读签名），中间恰好有条事件落到同一个对象上，
        就会变成"发出去的是旧状态、记下的签名是新状态"，
        于是下一次比较认为**没变**，那一次更新被静默吞掉 ——
        HUD 会一直停在旧状态直到该 Agent 再发下一条事件。

        这是一个只在"订阅者接入的瞬间刚好有事件"时才会命中的竞态，
        而它看起来就像"HUD 偶尔卡住一次"。所以这里把它做成一趟原子操作。
        """
        with self._lock:
            now = self._clock()
            rows: list[dict[str, Any]] = []
            alive: list[AgentSnapshot] = []
            for s in self._agents.values():
                if not include_expired and (now - s.updated_at) > self.expire_after_s:
                    continue
                self._with_staleness(s, now)
                alive.append(s)
            for s in _sort_snapshots(alive):
                rows.append(s.to_wire())
            return rows

    @staticmethod
    def signature_of(row: Mapping[str, Any]) -> tuple:
        """增量比较用的签名。**只接受线上形态**，这样签名与内容是同一时刻的。"""
        return (row.get("updated_at"), row.get("status"), bool(row.get("stale")))

    def agent_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._agents)

    def __len__(self) -> int:
        with self._lock:
            return len(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        with self._lock:
            return agent_id in self._agents

    # -- 维护 ---------------------------------------------------------------

    def sweep(self) -> list[str]:
        """移除长时间没消息的 Agent，返回被移除的 id。"""
        now = self._clock()
        with self._lock:
            dead = [k for k, s in self._agents.items() if (now - s.updated_at) > self.expire_after_s]
            for k in dead:
                self._agents.pop(k, None)
            return dead

    def remove(self, agent_id: str) -> bool:
        with self._lock:
            return self._agents.pop(agent_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._agents.clear()
            self._seen.clear()
            self.revision += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_status: dict[str, int] = {}
            for s in self._agents.values():
                by_status[s.status.value] = by_status.get(s.status.value, 0) + 1
            return {
                "agents": len(self._agents),
                "by_status": by_status,
                "rejected_events": self._rejected,
                "duplicate_events": self._duplicates,
                "stale_seq_events": self._stale_seq,
                "dedupe_window": self.dedupe_window,
                "seen_event_ids": len(self._seen),
                "revision": self.revision,
                "stale_after_s": self.stale_after_s,
                "expire_after_s": self.expire_after_s,
            }


__all__ = ["StateStore", "ApplyOutcome", "DEFAULT_STALE_AFTER_S", "DEFAULT_EXPIRE_AFTER_S"]