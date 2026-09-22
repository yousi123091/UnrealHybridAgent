"""Shared desktop transport and legacy Dashboard host.

The public HudApp defaults to Compact and retains this host as an explicit Debug mode.
Status, colors and rendering remain in uah.core and uah.ui.components.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from ...core.models import AgentSnapshot
from ...core.transport import DEFAULT_HUB_HOST, DEFAULT_HUB_PORT, HubClient, hub_url
from ...ui.components.card import (
    PANEL_BG,
    STATUS_COLORS,
    CardView,
    render_card,
)
from ...ui.notify import HudFlashSink, Notifier, Sink, SoundSink, ToastSink


class _NullSink(Sink):
    """提醒通道占位：明确"这个通道被关掉了"，而不是留个 None 到处判空。"""

    name = "off"
    available = False

    def send(self, note: Any) -> None:  # noqa: ARG002
        return None

DEFAULT_WIDTH = 320
DEFAULT_MAX_HEIGHT = 560
SCREEN_MARGIN = 16

HEADER_BG = "#0d1117"
BODY_BG = "#010409"
FOOTER_BG = "#0d1117"
TEXT_FG = "#e6edf3"
DIM_FG = "#8b949e"
BUTTON_BG = "#21262d"
BUTTON_FG = "#c9d1d9"


def default_state_path() -> Path:
    # uah/hosts/desktop/hud.py -> parents[3] == 仓库根
    return Path(__file__).resolve().parents[3] / ".state" / "uah" / "notify.json"


def _load_tk():
    try:
        import tkinter as tk
        from tkinter import ttk
        return tk, ttk
    except Exception:  # noqa: BLE001
        return None, None


class DashboardApp:
    """桌面上那个小窗口。"""

    def __init__(
        self,
        url: str | None = None,
        *,
        title: str = "UAH · Universal Agent HUD",
        width: int = DEFAULT_WIDTH,
        max_height: int = DEFAULT_MAX_HEIGHT,
        topmost: bool = True,
        corner: str = "top-right",
        notifier: Notifier | None = None,
        notify_sound: bool = True,
        notify_toast: bool = True,
        poll_ms: int = 250,
        clock_ms: int = 1000,
        embedded: bool = False,
    ) -> None:
        self.tk, self.ttk = _load_tk()
        if self.tk is None:
            raise RuntimeError("tkinter 不可用：请用自带 tkinter 的解释器运行桌面 HUD")
        self.url = url or hub_url(DEFAULT_HUB_HOST, DEFAULT_HUB_PORT)
        self.title = title
        self.width = int(width)
        self.max_height = int(max_height)
        self.corner = corner
        self.poll_ms = int(poll_ms)
        self.clock_ms = int(clock_ms)
        self.embedded = bool(embedded)

        # 子线程 → UI 的传输队列（tkinter 只能在主线程碰控件）
        self._inbox: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._snapshots: dict[str, AgentSnapshot] = {}
        self._cards: dict[str, Any] = {}
        self._order: list[str] = []
        self._changed: set[str] = set()

        self._stop = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._stream_status = "未连接"
        self._flash_until = 0.0

        self.notifier = notifier or Notifier(
            sinks=[SoundSink() if notify_sound else _NullSink(),
                   ToastSink(enabled=notify_toast)],
            state_path=default_state_path(),
        )
        self.notifier.sinks.append(HudFlashSink(self._on_notify))

        self.client = HubClient(self.url, timeout_s=3.0)
        self._root: Any = None
        self._body: Any = None
        self._empty_label: Any = None
        self._conn_label: Any = None
        self._mute_button: Any = None
        self._topmost = bool(topmost)
        #: 已排期的 after 回调 id。关窗口时必须**取消**，
        #: 否则 Tk 会在窗口销毁后仍去调用它们，弹一串
        #: "invalid command name ..._drain" —— 无害但看起来像崩了。
        self._after_ids: list[str] = []

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def build(self) -> Any:
        tk = self.tk
        root = tk.Tk()
        root.withdraw()
        import os
        if os.name == "nt": root.attributes("-disabled", True)
        self._root = root
        root.title(self.title)
        root.configure(bg=BODY_BG)
        root.attributes("-topmost", self._topmost)
        try:
            root.attributes("-alpha", 0.96)
        except Exception:  # noqa: BLE001
            pass
        self._place_window(root)
        root.protocol("WM_DELETE_WINDOW", self.close)

        # --- 头部 ---
        header = tk.Frame(root, bg=HEADER_BG, padx=8, pady=6)
        header.pack(fill="x")
        title_label = tk.Label(header, text="UAH", bg=HEADER_BG, fg=TEXT_FG,
                               font=("Segoe UI", 10, "bold"))
        title_label.pack(side="left")
        self._conn_label = tk.Label(header, text="● 未连接", bg=HEADER_BG, fg=DIM_FG,
                                    font=("Segoe UI", 8))
        self._conn_label.pack(side="left", padx=(6, 0))
        tk.Button(header, text="×", command=self.close, bg=BUTTON_BG, fg=BUTTON_FG,
                  font=("Segoe UI", 9), bd=0, padx=6, cursor="hand2").pack(side="right")
        self._mute_button = tk.Button(header, text=self._mute_text(), command=self.toggle_mute,
                                      bg=BUTTON_BG, fg=BUTTON_FG, font=("Segoe UI", 8),
                                      bd=0, padx=6, cursor="hand2")
        self._mute_button.pack(side="right", padx=(0, 4))
        tk.Button(header, text="置顶", command=self.toggle_topmost, bg=BUTTON_BG, fg=BUTTON_FG,
                  font=("Segoe UI", 8), bd=0, padx=6, cursor="hand2").pack(side="right", padx=(0, 4))

        for widget in (header, title_label):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        # --- 主体（可滚动） ---
        body_host = tk.Frame(root, bg=BODY_BG)
        body_host.pack(fill="both", expand=True)
        canvas = tk.Canvas(body_host, bg=BODY_BG, highlightthickness=0,
                           width=self.width, height=self.max_height)
        scrollbar = tk.Scrollbar(body_host, orient="vertical", command=canvas.yview)
        self._body = tk.Frame(canvas, bg=BODY_BG)
        window_id = canvas.create_window((0, 0), window=self._body, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _resize(_event: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        self._body.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        self._empty_label = tk.Label(
            self._body, text="还没有 Agent 接入\n\npython -m uah.tools.uah emit --agent Demo --status running",
            bg=BODY_BG, fg=DIM_FG, font=("Segoe UI", 8), justify="center", pady=24,
        )
        self._empty_label.pack(fill="x")

        footer = tk.Frame(root, bg=FOOTER_BG, padx=8, pady=4)
        footer.pack(fill="x")
        tk.Label(footer, text=self.url, bg=FOOTER_BG, fg=DIM_FG,
                 font=("Segoe UI", 7)).pack(side="left")

        # 位置与尺寸**放在最后**才设：geometry 会被之后 pack 的控件
        # 按"请求尺寸"改写，先设再装控件的话窗口会退回到 Tk 默认的 200x200。
        self._place_window(root)

        # --- 定时器 ---
        self._after_ids.append(root.after(self.poll_ms, self._drain))
        self._after_ids.append(root.after(self.clock_ms, self._tick))
        return root

    def _place_window(self, root: Any) -> None:
        root.update_idletasks()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        height = min(self.max_height + 80, max(200, screen_h - 160))
        if self.corner == "top-left":
            x, y = SCREEN_MARGIN, SCREEN_MARGIN
        elif self.corner == "bottom-right":
            x, y = screen_w - self.width - SCREEN_MARGIN, screen_h - height - 64
        elif self.corner == "bottom-left":
            x, y = SCREEN_MARGIN, screen_h - height - 64
        else:
            x, y = screen_w - self.width - SCREEN_MARGIN, SCREEN_MARGIN
        root.geometry(f"{self.width}x{height}+{x}+{y}")
        root.minsize(240, 120)

    # -- 窗口交互 -----------------------------------------------------------

    def _drag_start(self, event: Any) -> None:
        self._drag = (event.x_root - self._root.winfo_x(), event.y_root - self._root.winfo_y())

    def _drag_move(self, event: Any) -> None:
        dx, dy = getattr(self, "_drag", (0, 0))
        self._root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def toggle_topmost(self) -> None:
        self._topmost = not self._topmost
        try:
            self._root.attributes("-topmost", self._topmost)
        except Exception:  # noqa: BLE001
            pass

    def _mute_text(self) -> str:
        return "静音:关" if self.notifier.muted else "静音:开"

    def toggle_mute(self) -> None:
        self.notifier.toggle_mute()
        try:
            self._mute_button.configure(text=self._mute_text())
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._stop.set()
        # 主动断开 SSE：否则流线程要等一整个读超时（37s）才回来，
        # 关窗口会有明显延迟，看起来像卡死。
        self.client.close()
        if self._stream_thread and self._stream_thread is not threading.current_thread():
            self._stream_thread.join(timeout=2.0)
        self.notifier.set_mute(self.notifier.muted)  # 落盘一次
        root = self._root
        if root is not None:
            # 先取消排期的回调，再销毁窗口 —— 顺序反了会在 Tk 里留下一串
            # "invalid command name" 报错。
            for after_id in self._after_ids:
                try:
                    root.after_cancel(after_id)
                except Exception:  # noqa: BLE001
                    pass
            self._after_ids.clear()
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 数据接入（后台线程）
    # ------------------------------------------------------------------

    def start_stream(self) -> None:
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return
        self._stop.clear()

        def _on_snapshot(snap: AgentSnapshot) -> None:
            self._inbox.put(("snapshot", snap))

        def _on_removed(agent_id: str) -> None:
            self._inbox.put(("removed", agent_id))

        def _on_status(text: str) -> None:
            self._inbox.put(("status", text))

        def _run() -> None:
            self.client.stream(_on_snapshot, on_removed=_on_removed, on_status=_on_status,
                               stop=self._stop, read_timeout_s=3.0)

        self._stream_thread = threading.Thread(target=_run, name="uah-hud-stream", daemon=True)
        self._stream_thread.start()

    def _drain(self) -> None:
        """把子线程塞进队列的变化搬到 UI 上。**只在有变化时才动控件。**"""
        self._prune_timers()
        dirty = False
        processed = 0
        while processed < 500:
            try:
                kind, payload = self._inbox.get_nowait()
            except queue.Empty:
                break
            processed += 1
            if kind == "snapshot":
                self._snapshots[payload.agent.id] = payload
                self._changed.add(payload.agent.id)
                note = self.notifier.consider(payload)
                if note is not None:
                    dirty = True
            elif kind == "removed":
                self._changed.add(payload)
                self._snapshots.pop(payload, None)
                dirty = True
            elif kind == "status":
                self._stream_status = payload
                if payload == "connected":
                    # The following stream snapshot is authoritative after Hub restart.
                    self._snapshots.clear()
                dirty = True

        if dirty or self._changed:
            self._sync_cards()
        self._update_conn()
        if not self._stop.is_set():
            self._after_ids.append(self._root.after(self.poll_ms, self._drain))

    def _update_conn(self) -> None:
        text = "已连接" if self._stream_status == "connected" else self._stream_status
        healthy = text == "已连接"
        try:
            self._conn_label.configure(text=f"● {text}", fg="#3fb950" if healthy else "#d29922")
        except Exception:  # noqa: BLE001
            pass

    def _sync_cards(self) -> None:
        """重建/更新卡片。只有内容指纹变了才重建那张卡。"""
        tk = self.tk
        order = [s.agent.id for s in sorted(
            self._snapshots.values(),
            key=lambda s: (self._rank(s), -s.updated_at),
        )]

        if order != self._order:
            for card in self._cards.values():
                card.frame.pack_forget()
            for agent_id in order:
                card = self._cards.get(agent_id)
                if card is None:
                    snap = self._snapshots[agent_id]
                    from ...ui.components.tk_card import TkCard

                    card = TkCard(self._body, render_card(snap))
                    self._cards[agent_id] = card
                card.frame.pack(fill="x", padx=6, pady=(6, 0))
            self._order = order
            if self._empty_label is not None:
                if order:
                    self._empty_label.pack_forget()
                else:
                    self._empty_label.pack(fill="x")

        now = time.time()
        for agent_id in list(self._cards):
            snap = self._snapshots.get(agent_id)
            card = self._cards[agent_id]
            if snap is None:
                card.destroy()
                self._cards.pop(agent_id, None)
                continue
            card.update(render_card(snap, now=now))
        self._changed.clear()

    @staticmethod
    def _rank(snap: AgentSnapshot) -> int:
        # 与 ui.components.card.sort_cards 的口径一致
        return {
            "WAITING_APPROVAL": 0, "WAITING_INPUT": 1, "BLOCKED": 2, "ERROR": 3,
            "RUNNING": 4, "RETRYING": 5, "STARTING": 6, "PAUSED": 7,
            "IDLE": 8, "DONE": 9, "CANCELLED": 10, "UNKNOWN": 11,
        }.get(snap.status.value, 99)

    def _prune_timers(self):
        active = set(self._root.tk.call("after", "info"))
        self._after_ids[:] = [item for item in self._after_ids if item in active]

    def _tick(self) -> None:
        """每秒刷新时钟：只改 Label 文本，不重建控件。"""
        if self._stop.is_set():
            return
        now = time.time()
        for agent_id, card in list(self._cards.items()):
            snap = self._snapshots.get(agent_id)
            if snap is None:
                continue
            card.update(render_card(snap, now=now))
        if self._flash_until and now > self._flash_until:
            self._flash_until = 0.0
            for card in self._cards.values():
                card.unflash()
        self._after_ids.append(self._root.after(self.clock_ms, self._tick))

    # -- 提醒 ---------------------------------------------------------------

    def _on_notify(self, note: Any) -> None:
        """HudFlashSink 回调：卡片闪一下 + 窗口抬起来。"""
        card = self._cards.get(note.agent_id)
        if card is not None:
            color = STATUS_COLORS.get(note.status, "#f0883e")
            card.flash(color)
        self._flash_until = time.time() + 6.0
        try:
            self._root.deiconify()
            self._root.lift()
            if self._topmost:
                self._root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------

    def run(self) -> int:
        self.build()
        self.start_stream()
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
        return 0


# 关于内嵌宿主：tkinter 的 mainloop 必须在**主线程**，而 UHA 的主线程要给 Executor。
# 所以内嵌宿主**不**自己开窗口 —— 它复用同一个 Hub 订阅 + 同一套 ui.components，
# 把当前状态输出到 UHA 自己的界面通道（见 hosts/embedded/host.py）。
# 这条注释是刻意留的：它解释了为什么"共享组件"比"共享窗口"更正确。


from .compact import HudApp

__all__ = ["HudApp", "DashboardApp", "default_state_path", "DEFAULT_WIDTH", "DEFAULT_MAX_HEIGHT"]
