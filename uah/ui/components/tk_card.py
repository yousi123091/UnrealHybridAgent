"""把 ``CardView`` 画出来（tkinter）。

这是**唯一** import tkinter 的组件。两个宿主（内嵌 / 桌面）都用它，
所以"内嵌 UI 与桌面 HUD 长得不一样"这件事在结构上就不可能发生。

``TkCard`` 的更新策略（性能要求 §十八 的落点）：

* **内容**变了（``CardView.fingerprint``）→ 重建卡片主体；
* **时钟**变了（elapsed 每秒都在涨）→ 只改一个 Label 的文本。

绝不做"每秒重建整张卡片"这种事 —— 那会在空闲时也吃掉 CPU 并造成闪烁。
"""

from __future__ import annotations

try:  # pragma: no cover - 环境相关
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore
    ttk = None  # type: ignore

from .card import (
    CARD_BG,
    LABEL_FG,
    MUTED_FG,
    PANEL_BG,
    TITLE_FG,
    VALUE_FG,
    CardView,
)

CARD_WIDTH = 300
FONT = "Segoe UI"


def tk_available() -> bool:
    return tk is not None


class TkCard:
    """一张 Agent 卡片。``update(view)`` 就地刷新。"""

    def __init__(self, parent: "tk.Misc", view: CardView) -> None:
        if tk is None:  # pragma: no cover
            raise RuntimeError("tkinter 不可用")
        self.view = view
        self._rendered_fingerprint: str | None = None
        self._rows_signature: tuple = ()
        self._rows_host: "tk.Frame | None" = None
        self._note_label: "tk.Label | None" = None

        self.frame = tk.Frame(parent, bg=CARD_BG, highlightbackground=view.border_color,
                              highlightthickness=1, padx=8, pady=6)

        header = tk.Frame(self.frame, bg=CARD_BG)
        header.pack(fill="x")
        self._title = tk.Label(header, text=view.title, bg=CARD_BG, fg=TITLE_FG,
                               font=(FONT, 10, "bold"), anchor="w")
        self._title.pack(side="left")
        self._badge = tk.Label(header, text=view.badge, bg=CARD_BG, fg=view.badge_color,
                               font=(FONT, 9, "bold"), anchor="e")
        self._badge.pack(side="right")

        self._subtitle = tk.Label(self.frame, text=view.subtitle, bg=CARD_BG, fg=MUTED_FG,
                                  font=(FONT, 8), anchor="w", wraplength=CARD_WIDTH - 20,
                                  justify="left")
        self._subtitle.pack(fill="x")

        self._rows_host = tk.Frame(self.frame, bg=CARD_BG)

        self._progress = None
        if ttk is not None:
            self._progress = ttk.Progressbar(self.frame, orient="horizontal",
                                             length=CARD_WIDTH - 20, mode="determinate",
                                             maximum=100)

        self._footer = tk.Label(self.frame, text=view.footer, bg=CARD_BG, fg=MUTED_FG,
                                font=(FONT, 8), anchor="w")
        self._footer.pack(fill="x", pady=(4, 0))

        self.update(view, force=True)

    # -- 刷新 ---------------------------------------------------------------

    def update(self, view: CardView, *, force: bool = False) -> None:
        self.view = view
        if force or view.fingerprint != self._rendered_fingerprint:
            self._rendered_fingerprint = view.fingerprint
            self._render_content(view)
        # 时钟每帧都刷，但只改一个字符串 —— 不重建控件
        self._footer.configure(
            text=view.footer,
            fg=view.badge_color if (view.stale or view.stopped) else MUTED_FG,
        )

    def _render_content(self, view: CardView) -> None:
        self.frame.configure(highlightbackground=view.border_color)
        self._title.configure(text=view.title)
        self._badge.configure(text=view.badge, fg=view.badge_color)
        self._subtitle.configure(text=view.subtitle)

        rows_signature = tuple((r.label, r.value, r.color, r.strong) for r in view.rows)
        if rows_signature != self._rows_signature:
            self._rows_signature = rows_signature
            for child in self._rows_host.winfo_children():
                child.destroy()
            for row in view.rows:
                line = tk.Frame(self._rows_host, bg=CARD_BG)
                line.pack(fill="x")
                tk.Label(line, text=row.label, bg=CARD_BG, fg=LABEL_FG, width=8,
                         font=(FONT, 8), anchor="w").pack(side="left")
                tk.Label(line, text=row.value, bg=CARD_BG, fg=row.color,
                         font=(FONT, 9, "bold" if row.strong else "normal"), anchor="w",
                         wraplength=CARD_WIDTH - 80, justify="left").pack(side="left", fill="x")
            # rows 宿主紧跟副标题之后
            self._rows_host.pack(fill="x", pady=(4, 0), after=self._subtitle)

        if self._progress is not None:
            if view.ratio is None:
                self._progress.pack_forget()
            else:
                self._progress["value"] = max(0.0, min(1.0, view.ratio)) * 100.0
                self._progress.pack(fill="x", pady=(4, 0), before=self._footer)

        if view.note:
            if self._note_label is None:
                self._note_label = tk.Label(self.frame, text=view.note, bg=CARD_BG,
                                            fg=view.badge_color, font=(FONT, 8), anchor="w",
                                            wraplength=CARD_WIDTH - 20, justify="left")
                self._note_label.pack(fill="x", pady=(2, 0))
            else:
                self._note_label.configure(text=view.note)
        elif self._note_label is not None:
            self._note_label.destroy()
            self._note_label = None

    # -- 提醒高亮 -----------------------------------------------------------

    def flash(self, color: str) -> None:
        """把边框闪一下（``DONE`` / ``ERROR`` / ``WAITING_*`` 变化时）。"""
        try:
            self.frame.configure(highlightbackground=color, highlightthickness=2)
        except Exception:  # noqa: BLE001
            pass

    def unflash(self) -> None:
        try:
            self.frame.configure(highlightbackground=self.view.border_color, highlightthickness=1)
        except Exception:  # noqa: BLE001
            pass

    def destroy(self) -> None:
        try:
            self.frame.destroy()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["tk_available", "TkCard", "PANEL_BG", "CARD_BG", "VALUE_FG", "CARD_WIDTH", "FONT"]
