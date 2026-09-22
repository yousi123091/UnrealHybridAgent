"""发布通道：适配器把事件送去哪。

三个实现，都是同一个接口 ``publish(payload: dict) -> None``：

* ``LocalPublisher``  —— 与 Hub 同进程时直连，省一次 HTTP 往返。
* ``HttpPublisher``   —— 跨进程，POST ``/event``。
* ``NullPublisher``   —— 测试/降级用，只记账不外发（HUD 不在也不影响 Agent）。

**关键约束**：``publish`` 必须**不抛异常、不阻塞**。
Agent 的主逻辑（Executor）会走这条路径，一条 HUD 侧的故障绝不能把 Agent 拖死。
所以 HttpPublisher 内部吞掉所有异常并计数；需要真正确认送达时用 ``delivered()``。
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

from ..core.transport import HubClient


class Publisher:
    """发布通道协议。子类只需实现 ``_publish``。"""

    name = "publisher"

    def __init__(self) -> None:
        self._sent = 0
        self._failed = 0
        self._last_error: str | None = None

    def publish(self, payload: Mapping[str, Any]) -> bool:
        """发送。返回是否"成功送达"；**永不抛异常**。"""
        try:
            self._publish(payload)
            self._sent += 1
            return True
        except Exception as exc:  # noqa: BLE001 - 发布失败不能影响 Agent
            self._failed += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            return False

    def _publish(self, payload: Mapping[str, Any]) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def delivered(self) -> dict[str, Any]:
        return {"publisher": self.name, "sent": self._sent, "failed": self._failed,
                "last_error": self._last_error}


class NullPublisher(Publisher):
    """不发。用于测试与"Hub 不可达但我不想让 Agent 报错"的降级。"""

    name = "null"

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _publish(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append(dict(payload))

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            out = list(self.events)
            self.events.clear()
        return out


class LocalPublisher(Publisher):
    """直连同进程的 Hub（UHA 自己就是 Hub 时用这条）。"""

    name = "local"

    def __init__(self, server: Any) -> None:
        super().__init__()
        self.server = server

    def _publish(self, payload: Mapping[str, Any]) -> None:
        self.server.emit(dict(payload))

    def delivered(self) -> dict[str, Any]:
        out = super().delivered()
        try:
            out["hub"] = self.server.health()
        except Exception:  # noqa: BLE001
            pass
        return out


class HttpPublisher(Publisher):
    """跨进程：POST 到别的 Hub。"""

    name = "http"

    def __init__(self, url: str, *, timeout_s: float = 2.0) -> None:
        super().__init__()
        self.client = HubClient(url, timeout_s=timeout_s)

    def _publish(self, payload: Mapping[str, Any]) -> None:
        res = self.client.post_event(dict(payload))
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or "post failed"))
        if not res.get("accepted"):
            # 被状态机拒了（非法载荷）——不算传输失败，但值得记一笔
            self._last_error = str(res.get("reason"))


__all__ = ["Publisher", "NullPublisher", "LocalPublisher", "HttpPublisher"]
