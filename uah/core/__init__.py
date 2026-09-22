"""UAH Core —— 协议、模型、状态机、事件总线、本机传输。

这里**不出现任何具体 Agent 的名字**。UHA 的接入方式与本仓库无关的第三方 Agent
完全一致：都是往 UAH 发事件。区别只是 UHA 走原生适配器（语义更丰富），
别人走通用适配器（只给最小字段）。
"""

from __future__ import annotations

__all__ = ["protocol", "models", "events", "state", "transport"]
