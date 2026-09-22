"""Router 包：把"任务意图"变成"用哪种执行方式"。

    intent.py    TaskIntent —— Router 的结构化输入
    rules.py     确定性规则表（每条规则给候选 + 人类可读理由）
    cost.py      成本模型（可解释的 5 分量加权）
    fallback.py  降级链 + 跨任务方法健康度
    router.py    Router 主体：探测可用性 -> 跑规则 -> 算成本 -> 出决定
"""

from .cost import CostBreakdown, CostModel, METHOD_PROFILE
from .fallback import DEFAULT_CHAIN, FATAL_CODES, Attempt, FallbackManager, FallbackState, MethodHealth
from .intent import TaskIntent
from .router import Decision, Router
from .rules import (
    ALL_METHODS,
    GUI,
    HYBRID,
    KEYBOARD,
    MOUSE,
    STRUCTURED,
    UE_COMMANDLET,
    UE_PYTHON,
    UNREAL_MCP,
    VISION,
    Candidate,
    evaluate,
)

__all__ = [
    "TaskIntent",
    "Router",
    "Decision",
    "Candidate",
    "evaluate",
    "CostModel",
    "CostBreakdown",
    "METHOD_PROFILE",
    "FallbackManager",
    "FallbackState",
    "MethodHealth",
    "Attempt",
    "DEFAULT_CHAIN",
    "FATAL_CODES",
    "ALL_METHODS",
    "STRUCTURED",
    "GUI",
    "UNREAL_MCP",
    "UE_PYTHON",
    "UE_COMMANDLET",
    "KEYBOARD",
    "MOUSE",
    "VISION",
    "HYBRID",
]
