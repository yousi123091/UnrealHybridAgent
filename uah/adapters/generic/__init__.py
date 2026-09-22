"""通用适配器：让任何外部程序都能接入 UAH。

不需要写 Python、不需要装库、不需要懂 UAH 的内部结构。一条 HTTP POST 即可：

    curl -X POST http://127.0.0.1:8789/event \\
         -d '{"agent":"ExampleAgent","status":"running","task":"Running tests","activity":"pytest"}'
"""

from __future__ import annotations

from .bridge import GenericBridge, emit_event  # noqa: F401

__all__ = ["GenericBridge", "emit_event"]
