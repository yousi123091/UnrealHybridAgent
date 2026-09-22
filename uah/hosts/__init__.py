"""展示宿主。

``embedded`` —— 跑在 Agent 进程内的宿主（UHA 内嵌 HUD）。
``desktop``  —— 独立的 Windows HUD 进程，与 Agent 完全解耦。

两者都只是 ``uah.ui.components`` 的**使用者**：它们不定义状态、不定义颜色、
不定义布局。这就是需求 §一 要的"一套 HUD 核心组件，多个展示宿主"。
"""

from __future__ import annotations

__all__ = ["embedded", "desktop"]
