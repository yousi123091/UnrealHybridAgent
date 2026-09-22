"""内嵌 HUD —— 与桌面 HUD 共用状态机与渲染，只是换个出口。

实现方式是**在发布链上挂一层**：

    adapter ──▶ EmbeddedHud ──▶ 真正的 Publisher ──▶ Hub ──▶ 桌面 HUD
                    │
                    ├─▶ StateStore.apply()      （同一套状态机）
                    └─▶ render_card()           （同一套渲染）
                            │
                            ├─▶ UHA 结构化日志（logs/runs/*.jsonl）
                            └─▶ 控制台单行（可选）

这样"内嵌 UI 用的状态"和"桌面 HUD 用的状态"在**代码层面就是同一个对象**，
不可能漂移 —— 因为它们读的是同一个 ``AgentSnapshot``。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ...core.models import AgentSnapshot, Status
from ...core.state import StateStore
from ...ui.components.card import CardView, render_card
from ...ui.notify import Notifier
from ...adapters.publisher import Publisher

#: 事件类型 → 提醒通道用的"状态名"，仅用于控制台文案
_VERB = {
    Status.STARTING: "启动中",
    Status.RUNNING: "执行中",
    Status.WAITING_INPUT: "等待输入",
    Status.WAITING_APPROVAL: "等待批准",
    Status.PAUSED: "已暂停",
    Status.ERROR: "出错",
    Status.DONE: "完成",
    Status.CANCELLED: "已取消",
    Status.BLOCKED: "被阻塞",
    Status.IDLE: "空闲",
}


class EmbeddedHud(Publisher):
    """把事件同时"渲染到 UHA 自己的通道"和"转发到 Hub"。"""

    name = "embedded"

    def __init__(
        self,
        inner: Publisher,
        *,
        store: StateStore | None = None,
        logger: Any | None = None,
        console: bool = False,
        notifier: Notifier | None = None,
    ) -> None:
        super().__init__()
        self.inner = inner
        # 同 transport.HubServer：`store or StateStore()` 会坑 —— 空 store 是 falsy。
        self.store = store if store is not None else StateStore()
        self.logger = logger
        self.console = bool(console)
        self.notifier = notifier
        self.views: dict[str, CardView] = {}
        self._last_fingerprint: dict[str, str] = {}
        self._rendered = 0

    # -- Publisher 接口 -----------------------------------------------------

    def _publish(self, payload: Mapping[str, Any]) -> None:
        # 1) 先喂给本地状态机（内嵌 HUD 的"状态真源"）
        outcome = self.store.apply(dict(payload))
        if outcome.accepted and not outcome.duplicate and not outcome.stale_seq and outcome.snapshot:
            self._render(outcome.snapshot)
        # 2) 再转发出去（桌面 HUD 与别的订阅者看的是同一批事件）
        self.inner.publish(payload)

    # -- 渲染出口 -----------------------------------------------------------

    def _render(self, snap: AgentSnapshot) -> None:
        view = render_card(snap)
        self.views[snap.agent.id] = view
        previous = self._last_fingerprint.get(snap.agent.id)
        if previous == view.fingerprint:
            return
        self._last_fingerprint[snap.agent.id] = view.fingerprint
        self._rendered += 1

        # 出口 A：UHA 的结构化日志（"内嵌 UI"的正式通道）
        if self.logger is not None:
            try:
                self.logger.event(
                    "uah_presence",
                    agent=view.title,
                    status=view.status.value,
                    task=[r.value for r in view.rows if r.label == "Task"],
                    stage=[r.value for r in view.rows if r.label == "Stage"],
                    step=[r.value for r in view.rows if r.label == "Step"],
                    activity=[r.value for r in view.rows if r.label == "Activity"],
                    tool=[r.value for r in view.rows if r.label == "Tool"],
                    elapsed=view.footer,
                )
            except Exception:  # noqa: BLE001
                pass

        # 出口 B：控制台单行（默认关，开了才对）
        if self.console:
            try:
                print(self.one_line(snap), flush=True)
            except Exception:  # noqa: BLE001
                pass

        # 出口 C：提醒（与桌面宿主完全同一套策略）
        if self.notifier is not None:
            try:
                self.notifier.consider(snap)
            except Exception:  # noqa: BLE001
                pass

    # -- 查询 ---------------------------------------------------------------

    @staticmethod
    def one_line(snap: AgentSnapshot) -> str:
        """单行文案。``[UAH] UHA RUNNING · Phase 4B · Analysis · Step 1/3 · 路由中``"""
        bits = [f"[UAH] {snap.agent.name} {snap.status.value}"]
        if snap.task.phase:
            bits.append(snap.task.phase)
        if snap.task.stage:
            bits.append(snap.task.stage)
        if snap.task.progress_text:
            bits.append(f"Step {snap.task.progress_text}")
        if snap.activity.one_line:
            bits.append(snap.activity.one_line)
        if snap.activity.tool:
            bits.append(f"via {snap.activity.tool}")
        return " · ".join(bits)

    def snapshot(self, agent_id: str) -> AgentSnapshot | None:
        return self.store.snapshot(agent_id)

    def text_report(self) -> str:
        """把当前所有卡片渲染成文本（测试 / ``uah status`` 用）。"""
        lines: list[str] = []
        now = time.time()
        for snap in self.store.snapshots():
            lines.extend(render_card(snap, now=now).to_lines())
            lines.append("")
        return "\n".join(lines).rstrip()

    def stats(self) -> dict[str, Any]:
        out = super().delivered()
        out.update({
            "inner": self.inner.delivered(),
            "rendered": self._rendered,
            "agents": len(self.views),
            "store": self.store.stats(),
        })
        return out


__all__ = ["EmbeddedHud"]
