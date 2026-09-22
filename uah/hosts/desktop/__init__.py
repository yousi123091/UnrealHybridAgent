"""独立桌面 HUD（Windows）。

* ``hud.py``       —— tkinter 宿主。置顶小窗、多 Agent 卡片、提醒、静音、重连。
* ``text_hud.py``  —— 没有 tkinter 时的降级宿主（本机 ``.venv`` 就属于这种情况）。
* ``launch.py``    —— 自动挑一个带 tkinter 的解释器来跑图形宿主。
"""

from __future__ import annotations

__all__ = ["hud", "text_hud", "launch"]
