"""核心基础设施：配置、日志、错误。"""

from .config import CONFIG_DIR, DEFAULTS, ROOT, Config, load_config
from .errors import (
    ActorNotFound,
    AdapterError,
    BackendUnavailable,
    DesktopBusy,
    FallbackExhausted,
    NoViableMethod,
    NotSupported,
    PreconditionFailed,
    SafetyRefused,
    ToolCallFailed,
    TransportError,
    UHAError,
    VerificationFailed,
)
from .log import RunLogger

__all__ = [
    "Config",
    "load_config",
    "ROOT",
    "CONFIG_DIR",
    "DEFAULTS",
    "RunLogger",
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
