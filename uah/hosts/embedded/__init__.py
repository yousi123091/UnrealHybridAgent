"""内嵌宿主：跑在 Agent（UHA）进程里的 UAH 展示侧。

它**不自己开窗口**，原因是硬的：tkinter 的 ``mainloop`` 必须在主线程，
而 UHA 的主线程要留给 Executor。所以内嵌宿主复用的是：

* 同一个 ``StateStore``（同一套状态机）
* 同一个 ``render_card()``（同一套渲染）
* 同一个 ``Notifier``（同一套提醒策略）

只是把渲染结果送到**UHA 自己的展示通道**（结构化日志 + 可选控制台行），
而不是送到一个独立窗口。这正是需求 §一 要的"共享 HUD 核心组件、多个展示宿主"。
"""

from __future__ import annotations

from .bootstrap import UahBoot, uah_attach, uah_begin_boot, uah_shutdown  # noqa: F401
from .host import EmbeddedHud  # noqa: F401

__all__ = ["UahBoot", "uah_begin_boot", "uah_attach", "uah_shutdown", "EmbeddedHud"]
