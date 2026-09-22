"""UHA（UnrealHybridAgent）原生适配器。

UHA 是第一个"深度原生支持 UAH"的 Agent：它能给出完整的
task / phase / stage / step / activity / tool 语义，
而不是像外部 Agent 那样只能说一句 "RUNNING"。
"""

from __future__ import annotations

from .adapter import UhaNativeAdapter, UHA_PHASE_TO_STATUS, UHA_PHASE_TO_STAGE  # noqa: F401

__all__ = ["UhaNativeAdapter", "UHA_PHASE_TO_STATUS", "UHA_PHASE_TO_STAGE"]
