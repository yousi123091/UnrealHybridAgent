"""适配器：把某个 Agent 的私有状态翻译成 UAH 事件。

* ``uha``      —— UHA 原生适配器。完整语义（task/stage/step/activity/tool）。
* ``generic``  —— 通用适配器。外部 Agent 只要会说 ``{"status": "running"}`` 就能接入。

两者产出的都是同一套 ``uah.core.models.Event``，写进同一个 ``StateStore``。
"""

from __future__ import annotations

__all__ = ["uha", "generic"]
