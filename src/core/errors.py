"""错误类型。

设计原则：每一种失败都要能被 Router / FallbackManager 用来做决策，
所以错误必须携带稳定的 `code`，而不是靠匹配错误字符串。
"""

from __future__ import annotations

from typing import Any, Mapping


class UHAError(Exception):
    """所有 UnrealHybridAgent 错误的基类。"""

    code = "UHA_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# --- 适配器相关 ---------------------------------------------------------------


class AdapterError(UHAError):
    """适配器层通用错误。"""

    code = "ADAPTER_ERROR"


class BackendUnavailable(AdapterError):
    """后端根本不可用（进程没起、端口不通、插件缺失）。

    这类错误**不应该重试**，应该直接让 Router 换一种执行方式。
    """

    code = "BACKEND_UNAVAILABLE"


class ToolCallFailed(AdapterError):
    """后端可用，但这次调用失败了。可以重试或降级。"""

    code = "TOOL_CALL_FAILED"


class TransportError(AdapterError):
    """协议/传输层失败（握手失败、超时、SSE 解析失败）。"""

    code = "TRANSPORT_ERROR"


class DesktopBusy(AdapterError):
    """Computer Use 桌面锁被别人持有。"""

    code = "DESKTOP_BUSY"


class NotSupported(AdapterError):
    """该后端不支持这个动作（例如某个 UE MCP 没有 set_actor_scale）。"""

    code = "NOT_SUPPORTED"


# --- 语义层相关 ---------------------------------------------------------------


class ActorNotFound(UHAError):
    """目标 Actor 在当前 Level 中不存在。"""

    code = "ACTOR_NOT_FOUND"


class VerificationFailed(UHAError):
    """动作执行了（没报错），但**验证不通过**——这是最重要的一类错误。

    例如：set_actor_location 返回成功，但重新读取 transform 发现没变。
    """

    code = "VERIFICATION_FAILED"


class PreconditionFailed(UHAError):
    """前置条件不满足，不应该继续执行。"""

    code = "PRECONDITION_FAILED"


# --- 编排层相关 ---------------------------------------------------------------


class NoViableMethod(UHAError):
    """Router 找不到任何可用执行方式。"""

    code = "NO_VIABLE_METHOD"


class FallbackExhausted(UHAError):
    """所有降级路线都用完了。"""

    code = "FALLBACK_EXHAUSTED"


class SafetyRefused(UHAError):
    """被安全策略拦住（例如超过 max_actors_per_batch、要求只读）。"""

    code = "SAFETY_REFUSED"


__all__ = [
    "UHAError",
    "AdapterError",
    "BackendUnavailable",
    "ToolCallFailed",
    "TransportError",
    "DesktopBusy",
    "NotSupported",
    "ActorNotFound",
    "VerificationFailed",
    "PreconditionFailed",
    "NoViableMethod",
    "FallbackExhausted",
    "SafetyRefused",
]
