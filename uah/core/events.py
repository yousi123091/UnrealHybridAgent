"""进程内事件总线。

用途只有一个：把"状态变了"这件事**推**给订阅者，而不是让订阅者去轮询
（需求 §十八：禁止高频轮询）。UHA 现有的 ``ControlOverlay`` 是 200ms 轮询
``controller.snapshot()``；UAH 的路径是 ``SessionController.on_change`` → 本总线 → 传输层。

设计上刻意**极简**：

* 同步投递，调用方线程里跑完。订阅者必须快（只做入队/序列化，不做 I/O 等待）。
* 订阅者抛异常**不影响**其它订阅者，也不影响发布方。一条坏订阅者不能让 Agent 挂掉。
* 不做优先级、不做重试、不做持久化——那些都是 Hub 的事，不是总线的事。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

Handler = Callable[[Any], None]


class EventBus:
    """线程安全的同步 pub/sub。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: list[tuple[int, Handler]] = []
        self._next_token = 1
        self._errors: list[tuple[str, int]] = []
        self._dropped = 0

    def subscribe(self, handler: Handler) -> int:
        """注册订阅者，返回 token（用于 ``unsubscribe``）。"""
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._subs.append((token, handler))
        return token

    def unsubscribe(self, token: int) -> bool:
        with self._lock:
            before = len(self._subs)
            self._subs = [(t, h) for (t, h) in self._subs if t != token]
            return len(self._subs) != before

    def publish(self, event: Any) -> int:
        """投递给所有订阅者，返回**成功**投递的数量。

        订阅者异常被吞掉并记账（``errors()``）。这是刻意的：
        UI 崩了不该让 Agent 崩，Agent 的某个日志订阅者抛了不该让别的订阅者收不到。
        """
        with self._lock:
            targets = list(self._subs)
        ok = 0
        for _token, handler in targets:
            try:
                handler(event)
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 订阅者故障必须隔离
                with self._lock:
                    self._errors.append((f"{type(exc).__name__}: {exc}", self._dropped + len(self._errors)))
        return ok

    # -- 观察 ---------------------------------------------------------------

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def errors(self) -> list[tuple[str, int]]:
        with self._lock:
            return list(self._errors)

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()
            self._errors.clear()
            self._dropped = 0


__all__ = ["EventBus", "Handler"]
