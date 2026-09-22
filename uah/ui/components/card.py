"""卡片渲染 —— **纯函数**，两个宿主唯一的视觉真源。

``AgentSnapshot`` 进来，``CardView`` 出去。没有 tkinter、没有网络、没有状态。

需求 §一 要求的"不复制 UHA 的 UI 后独立维护"，落地就在这里：
内嵌宿主和桌面宿主都调用 ``render_card()``，它们**不拥有**任何状态标签、
颜色映射或布局决定。要改"WAITING_APPROVAL 长什么样"，
只改这个文件一个地方。

需求 §二十二 的验收项 C（"不存在两份独立 AgentStatus/TaskStatus 定义"）
在这个文件上体现为：颜色表以 ``Status`` 为键，而 ``Status`` 定义在
``uah/core/models.py``。**这里不定义任何状态枚举。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ...core.models import AgentSnapshot, Status

# ---------------------------------------------------------------------------
# 状态 → 视觉
# ---------------------------------------------------------------------------

#: 状态色。深色 HUD 面板上用，对比度都 ≥ 4.5:1。
STATUS_COLORS: dict[Status, str] = {
    Status.IDLE: "#8b949e",
    Status.STARTING: "#58a6ff",
    Status.RUNNING: "#3fb950",
    Status.WAITING_INPUT: "#d29922",
    Status.WAITING_APPROVAL: "#f0883e",
    Status.PAUSED: "#8b949e",
    Status.ERROR: "#f85149",
    Status.DONE: "#3fb950",
    Status.CANCELLED: "#6e7681",
    Status.RETRYING: "#58a6ff",
    Status.BLOCKED: "#f0883e",
    Status.UNKNOWN: "#6e7681",
}

#: 状态圆点/勾叉字形
STATUS_GLYPHS: dict[Status, str] = {
    Status.IDLE: "○",
    Status.STARTING: "◌",
    Status.RUNNING: "●",
    Status.WAITING_INPUT: "⚠",
    Status.WAITING_APPROVAL: "⚠",
    Status.PAUSED: "❙❙",
    Status.ERROR: "✗",
    Status.DONE: "✓",
    Status.CANCELLED: "⊘",
    Status.RETRYING: "↻",
    Status.BLOCKED: "⛔",
    Status.UNKNOWN: "?",
}

#: 状态人类文字
STATUS_LABELS: dict[Status, str] = {
    Status.IDLE: "IDLE",
    Status.STARTING: "STARTING",
    Status.RUNNING: "RUNNING",
    Status.WAITING_INPUT: "WAITING INPUT",
    Status.WAITING_APPROVAL: "WAITING APPROVAL",
    Status.PAUSED: "PAUSED",
    Status.ERROR: "ERROR",
    Status.DONE: "DONE",
    Status.CANCELLED: "CANCELLED",
    Status.RETRYING: "RETRYING",
    Status.BLOCKED: "BLOCKED",
    Status.UNKNOWN: "UNKNOWN",
}

#: 卡片边框色（未告警时的默认）
NEUTRAL_BORDER = "#30363d"
#: 面板底色
PANEL_BG = "#161b22"
CARD_BG = "#0d1117"
TITLE_FG = "#e6edf3"
LABEL_FG = "#8b949e"
VALUE_FG = "#c9d1d9"
MUTED_FG = "#6e7681"


# ---------------------------------------------------------------------------
# 视图模型
# ---------------------------------------------------------------------------


@dataclass
class CardRow:
    """卡片上的一行 "标签 + 值"。``value`` 为空的行**不渲染**（需求 §五：字段允许为空）。"""

    label: str
    value: str
    color: str = VALUE_FG
    strong: bool = False


@dataclass
class CardView:
    """一张卡片的全部渲染信息。两个宿主都照着它画。"""

    agent_id: str
    title: str
    subtitle: str
    status: Status
    badge: str
    badge_color: str
    border_color: str
    rows: list[CardRow] = field(default_factory=list)
    footer: str = ""
    note: str | None = None
    ratio: float | None = None
    stale: bool = False
    stopped: bool = False
    protocol_ok: bool = True

    #: 用于提醒去重与"卡片内容是否变了"的比较
    fingerprint: str = ""

    @property
    def is_alert(self) -> bool:
        return self.status.is_alert

    def to_lines(self) -> list[str]:
        """文本框 HUD 用的纯文本形态。与图形宿主共用同一份 rows。"""
        out = [f"{self.badge}  {self.title}"]
        if self.subtitle:
            out.append(f"    {self.subtitle}")
        for row in self.rows:
            out.append(f"    {row.label:<9}{row.value}")
        if self.footer:
            out.append(f"    {self.footer}")
        if self.note:
            out.append(f"    ! {self.note}")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "status": self.status.value,
            "badge": self.badge,
            "badge_color": self.badge_color,
            "border_color": self.border_color,
            "rows": [{"label": r.label, "value": r.value, "color": r.color} for r in self.rows],
            "footer": self.footer,
            "note": self.note,
            "ratio": self.ratio,
            "stale": self.stale,
            "stopped": self.stopped,
            "protocol_ok": self.protocol_ok,
            "fingerprint": self.fingerprint,
        }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def format_elapsed(seconds: float) -> str:
    """``3725`` → ``"1:02:05"``；``95`` → ``"1:35"``。"""
    total = int(max(0.0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _short_path(path: str | None, *, keep: int = 2) -> str | None:
    if not path:
        return None
    norm = path.replace("\\", "/").rstrip("/")
    parts = [p for p in norm.split("/") if p]
    if len(parts) <= keep:
        return "/".join(parts)
    return ".../" + "/".join(parts[-keep:])


def render_card(snapshot: AgentSnapshot, *, now: float | None = None) -> CardView:
    """``AgentSnapshot`` → ``CardView``。**这是唯一的渲染逻辑。**"""
    import time as _time

    ts = _time.time() if now is None else now
    status = snapshot.status
    color = STATUS_COLORS.get(status, STATUS_COLORS[Status.UNKNOWN])
    glyph = STATUS_GLYPHS.get(status, "?")
    label = STATUS_LABELS.get(status, status.value)

    # 标题行：谁，以及它现在什么状态
    title = snapshot.agent.name or snapshot.agent.id or "unknown"
    badge = f"{glyph} {label}"

    # 副标题：项目名优先，其次项目路径末两段，再其次 agent 类型
    subtitle = (
        snapshot.project.name
        or _short_path(snapshot.project.path)
        or snapshot.agent.type
        or ""
    )

    rows: list[CardRow] = []

    task = snapshot.task
    if task.phase or task.name:
        name_bits = [b for b in (task.phase, task.name) if b]
        rows.append(CardRow("Task", " · ".join(name_bits), strong=True))
    if task.stage:
        rows.append(CardRow("Stage", task.stage))
    progress = task.progress_text
    if progress:
        rows.append(CardRow("Step", progress, color=color))

    act = snapshot.activity
    if act.one_line:
        rows.append(CardRow("Activity", act.one_line, color=TITLE_FG))
    if act.detail:
        rows.append(CardRow("Detail", act.detail))
    if act.tool:
        rows.append(CardRow("Tool", act.tool))

    # 页脚：elapsed + 离线/已退出/协议不兼容等硬事实
    flags: list[str] = []
    if snapshot.stopped:
        flags.append("已退出")
    elif snapshot.stale:
        flags.append(f"离线 {snapshot.stale_for_s:.0f}s")
    if not snapshot.protocol_ok:
        flags.append("协议不兼容")
    # elapsed 的口径：还在跑 => 走到现在（看着它涨）；已结束 => 停在结束时（不再涨）。
    # 两种情况都用同一个函数取值，免得两个宿主各算各的。
    if status.is_terminal:
        elapsed_s = snapshot.elapsed_ms / 1000.0
    else:
        elapsed_s = max(0.0, ts - snapshot.first_seen_at)
    footer = f"elapsed {format_elapsed(elapsed_s)}"
    if flags:
        footer += "  ·  " + " · ".join(flags)

    border = NEUTRAL_BORDER
    if status.is_alert or snapshot.stale or not snapshot.protocol_ok:
        border = color

    # 指纹里**不放** elapsed：秒数每秒都在变，放进去会让每张卡片每秒重建一次。
    # 内容是内容，时钟是时钟 —— 宿主分开处理（见 ui/components/tk_card.py）。
    fingerprint = "|".join([
        snapshot.agent.id,
        status.value,
        str(snapshot.seq),
        str(snapshot.status_changed_at),
        str(len(rows)),
        progress or "",
        act.one_line or "",
        "1" if snapshot.stale else "0",
        "1" if snapshot.stopped else "0",
        "1" if snapshot.protocol_ok else "0",
        snapshot.note or "",
    ])

    return CardView(
        agent_id=snapshot.agent.id,
        title=title,
        subtitle=subtitle,
        status=status,
        badge=badge,
        badge_color=color,
        border_color=border,
        rows=rows,
        footer=footer,
        note=snapshot.note,
        ratio=task.progress_ratio,
        stale=snapshot.stale,
        stopped=snapshot.stopped,
        protocol_ok=snapshot.protocol_ok,
        fingerprint=fingerprint,
    )


def render_cards(
    snapshots: Iterable[AgentSnapshot], *, now: float | None = None
) -> list[CardView]:
    return [render_card(s, now=now) for s in snapshots]


def sort_cards(cards: Sequence[CardView]) -> list[CardView]:
    """需要人看的排前面。与 ``StateStore.snapshots`` 的排序意图一致。"""
    rank = {
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
    return sorted(cards, key=lambda c: rank.get(c.status, 99))


__all__ = [
    "STATUS_COLORS",
    "STATUS_GLYPHS",
    "STATUS_LABELS",
    "NEUTRAL_BORDER",
    "PANEL_BG",
    "CARD_BG",
    "TITLE_FG",
    "LABEL_FG",
    "VALUE_FG",
    "MUTED_FG",
    "CardRow",
    "CardView",
    "format_elapsed",
    "render_card",
    "render_cards",
    "sort_cards",
]
