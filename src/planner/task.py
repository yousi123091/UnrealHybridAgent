"""计划的数据结构。

第一版计划是**线性的、显式的**，故意不做"动态重规划"：
    * 线性 = 出问题时一眼能看出是哪一步错了；
    * 显式 = 每一步写清哪个 skill、什么参数、能不能跳过。

真正需要动态决策的地方只有"这一步用哪种方式执行"，
那件事已经由 Router 负责了 —— 不要在两层同时做决策，会互相打架。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping


@dataclass
class PlanStep:
    """计划中的一步。"""

    skill: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    #: 可选步骤：失败不阻断整个计划（例如"顺手截个图"）
    optional: bool = False
    #: 依赖的前置步骤 id（仅用于展示与校验，第一版不并行）
    after: tuple[str, ...] = ()

    #: **建议**本步优先用哪种执行方式（UNREAL_MCP / HYBRID / ...）。
    #:
    #: 这是一个容易和"不要在两层同时做决策"打架的东西，所以说清边界：
    #:   * 它**只调整尝试顺序**，不删减降级链——首选失败后照样按 Router
    #:     给的顺序继续降级，鲁棒性不受影响；
    #:   * 该方式不在 Router 的可用候选里（后端不可用/冷却中）时，这条建议
    #:     会被**忽略并记日志**，不会硬闯。
    #:
    #: 为什么仍然需要它：当某一步的**目的就是演练某条通道**时（演示、
    #: 通道契约测试），"按成本挑最快"是错的目标函数。默认仍为 None，
    #: 也就是默认完全交给 Router。
    prefer_method: str | None = None

    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.skill

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "skill": self.skill,
            "params": dict(self.params),
            "description": self.description,
            "optional": self.optional,
            "after": list(self.after),
            "prefer_method": self.prefer_method,
        }


@dataclass
class Plan:
    """一个完整的执行计划。"""

    name: str
    steps: list[PlanStep] = field(default_factory=list)
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __iter__(self) -> Iterator[PlanStep]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def add(self, step: PlanStep) -> "Plan":
        self.steps.append(step)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [s.as_dict() for s in self.steps],
            "meta": dict(self.meta),
        }

    def describe(self) -> str:
        lines = [f"计划 {self.name}（{len(self.steps)} 步）{'：' + self.description if self.description else ''}"]
        for i, step in enumerate(self.steps, 1):
            flag = "（可选）" if step.optional else ""
            hint = f" [建议方式：{step.prefer_method}]" if step.prefer_method else ""
            lines.append(f"  {i}. {step.skill}{flag} — {step.description or step.params}{hint}")
        return "\n".join(lines)


__all__ = ["Plan", "PlanStep"]
