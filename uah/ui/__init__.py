"""UAH UI 层：**共享**的渲染组件与提醒策略。

这一层被"内嵌宿主"和"独立桌面宿主"**共同使用**。需求 §一 禁止出现
"UHA/UI + UAH/UI 两份相似实现"，这里的做法是：

* ``components/card.py``  —— 纯函数渲染：``AgentSnapshot`` → ``CardView``。
  没有 tkinter、没有网络、没有状态。所以它是**可测的**，也是唯一的。
* ``components/tk_card.py`` —— 把 ``CardView`` 画成 tkinter 控件。只有它碰 GUI。
* ``notify.py``          —— 提醒策略（去重/静音/多通道），与 GUI 无关。

两个宿主都只是"把 CardView 画出来"而已，谁都不会自己决定状态该长什么样。
"""

from __future__ import annotations

__all__ = ["components", "notify"]
