"""执行方式的成本模型。

按需求把 cost 拆成 5 个可解释的分量（值越小越好），并按配置权重加权：

    score = w_success*(1-成功率) + w_latency*延迟 + w_precision*精度缺口
          + w_cost*操作成本 + w_risk*风险

其中"操作成本"就是需求里那条：

    tool_cost = 预计步骤数 + 失败概率 + 是否需要截图 + 是否重复 + 是否精确操作

刻意做成**可解释**而不是黑盒：每个 Decision 都会把各分量原样写进日志，
这样"为什么选了 Mouse 而不是 Unreal MCP"是可答辩的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .intent import TaskIntent

#: 各执行方式的固有属性（第一版人工标定，后续用 logs/runs/*.jsonl 的实测数据修正）
METHOD_PROFILE: dict[str, dict[str, float]] = {
    # 结构化通道：慢一点但精确、可验证、不依赖界面状态
    "UNREAL_MCP": {"base_steps": 1.0, "latency": 0.35, "precision": 0.02, "risk": 0.15, "vision": 0.0},
    "UE_PYTHON": {"base_steps": 1.0, "latency": 0.45, "precision": 0.02, "risk": 0.20, "vision": 0.0},
    "UE_COMMANDLET": {"base_steps": 1.0, "latency": 0.95, "precision": 0.05, "risk": 0.45, "vision": 0.0},
    # GUI 通道：快，但精度差、依赖界面状态
    "KEYBOARD": {"base_steps": 1.5, "latency": 0.15, "precision": 0.55, "risk": 0.35, "vision": 0.1},
    "MOUSE": {"base_steps": 3.5, "latency": 0.25, "precision": 0.75, "risk": 0.55, "vision": 0.3},
    "VISION": {"base_steps": 2.0, "latency": 0.60, "precision": 0.40, "risk": 0.50, "vision": 1.0},
    "HYBRID": {"base_steps": 2.5, "latency": 0.55, "precision": 0.20, "risk": 0.40, "vision": 0.6},
}


@dataclass
class CostBreakdown:
    """一条可读的成本明细。"""

    method: str
    steps: float
    failure_prob: float
    needs_screenshot: bool
    repeated: bool
    precision_required: bool
    weights: Mapping[str, float] = field(default_factory=dict)
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def tool_cost(self) -> float:
        """需求里定义的那个"简单 cost"。"""
        return (
            self.steps
            + self.failure_prob
            + (1.0 if self.needs_screenshot else 0.0)
            + (1.0 if self.repeated else 0.0)
            + (1.0 if self.precision_required else 0.0)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "score": round(self.score, 4),
            "tool_cost": round(self.tool_cost, 4),
            "steps": self.steps,
            "failure_prob": round(self.failure_prob, 3),
            "needs_screenshot": self.needs_screenshot,
            "repeated": self.repeated,
            "precision_required": self.precision_required,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "notes": self.notes,
        }


class CostModel:
    """把 (意图, 方式, 历史统计) 映射成一个分数。"""

    def __init__(self, weights: Mapping[str, float] | None = None, *, profiles: Mapping[str, Mapping[str, float]] | None = None):
        self.weights = dict(
            weights
            or {"success_rate": 0.42, "latency": 0.18, "precision": 0.16, "cost": 0.14, "risk": 0.10}
        )
        self.profiles = {k: dict(v) for k, v in (profiles or METHOD_PROFILE).items()}

    # -- 统计注入 -------------------------------------------------------------

    @staticmethod
    def _success_rate(method: str, stats: Mapping[str, Mapping[str, int]] | None) -> float:
        """从历史统计里算成功率。没有历史时给一个中性先验。"""
        if not stats or method not in stats:
            return 0.6  # 中性先验：既不过度乐观也不惩罚新方法
        slot = stats[method]
        total = int(slot.get("ok", 0)) + int(slot.get("failed", 0))
        if total == 0:
            return 0.6
        return int(slot.get("ok", 0)) / total

    def estimate(
        self,
        intent: TaskIntent,
        method: str,
        *,
        stats: Mapping[str, Mapping[str, int]] | None = None,
        available: bool = True,
        attempts: int = 0,
        cooldown: bool = False,
        task_category: str | None = None,
    ) -> CostBreakdown:
        prof = self.profiles.get(method)
        if prof is None:
            return CostBreakdown(
                method=method,
                steps=99.0,
                failure_prob=1.0,
                needs_screenshot=False,
                repeated=False,
                precision_required=False,
                score=99.0,
                notes=[f"未知执行方式 {method}"],
            )

        steps = prof["base_steps"]
        notes: list[str] = []
        if task_category:
            notes.append(f"task_category={task_category}")

        # --- 步骤数修正：批量 / 只读 / 精确 --------------------------------------
        if intent.is_batch and method in {"MOUSE", "KEYBOARD", "VISION"}:
            # GUI 通道在批量下几乎线性放大
            steps += 0.55 * intent.target_count
            notes.append(f"批量 {intent.target_count} 个：GUI 通道步骤放大")
        if intent.target_count > 1 and method in {"UNREAL_MCP", "UE_PYTHON"}:
            steps += 0.15  # 结构化通道一个脚本搞定，只加一点点
            notes.append("批量：结构化通道仍为单步")
        if intent.read_only:
            steps = max(1.0, steps - 0.4)
            notes.append("只读任务：步骤略降")

        needs_screenshot = intent.needs_vision and method in {"VISION", "MOUSE", "HYBRID"}

        # --- 精度缺口：需要精确数值却走 GUI ------------------------------------
        precision_required = bool(intent.needs_exact_values)
        if precision_required and method in {"MOUSE", "KEYBOARD"}:
            notes.append("需要精确数值：GUI 通道无法保证")
        if not precision_required:
            notes.append("无需精确数值：GUI 通道可用")

        # --- 视觉需求：不看画面的方法要扣分 ------------------------------------
        if intent.needs_vision and prof["vision"] < 0.5:
            notes.append("需要视觉判断：该方法看不见画面")

        # --- 重复惩罚 -----------------------------------------------------------
        repeated = attempts > 0
        if repeated:
            notes.append(f"本任务已尝试 {attempts} 次")

        # --- 可行性一票否决 -----------------------------------------------------
        viable = available and not cooldown
        if not available:
            notes.append("后端不可用")
        if cooldown:
            notes.append("方法处于冷却期")

        # --- 组装分量 -----------------------------------------------------------
        success_rate = self._success_rate(method, stats)
        latency = prof["latency"]
        precision_gap = (prof["precision"] if precision_required else 0.0)
        if not precision_required:
            precision_gap *= 0.25  # 不需要精确时，精度差不是大问题
        risk = prof["risk"] + (0.25 if intent.ui_only and method in {"UNREAL_MCP", "UE_PYTHON"} else 0.0)
        if intent.ui_only and method in {"UNREAL_MCP", "UE_PYTHON"}:
            notes.append("UI-only 任务：结构化通道可能根本够不着")

        op_cost = min(1.0, (steps + (1.0 if repeated else 0.0) * 0.5) / 6.0)

        w = self.weights
        components = {
            "success": w["success_rate"] * (1.0 - success_rate),
            "latency": w["latency"] * latency,
            "precision": w["precision"] * precision_gap,
            "cost": w["cost"] * op_cost,
            "risk": w["risk"] * min(1.0, risk),
        }
        score = sum(components.values())
        if not viable:
            score += 10.0  # 不可行的排在最后，但仍保留在候选里以便解释

        return CostBreakdown(
            method=method,
            steps=round(steps, 2),
            failure_prob=1.0 - success_rate,
            needs_screenshot=needs_screenshot,
            repeated=repeated,
            precision_required=precision_required,
            weights=w,
            components=components,
            score=score,
            notes=notes,
        )


__all__ = ["CostModel", "CostBreakdown", "METHOD_PROFILE"]
