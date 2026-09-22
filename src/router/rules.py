"""确定性规则表。

第一版**不引入任何模型**：每条规则都是 if-then，输出的 reason 直接进日志。
这样出问题时能一眼看出"是哪条规则选错了"，而不是面对一个不可解释的分数。

每条规则返回 `Candidate(method, reason, bias)`，`bias` 是给 CostModel 之外的
先验加权（规则强烈建议 → bias 大）。Router 会把所有候选按
`score - bias` 排序，取最优，并把其余作为 fallback 顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .intent import TaskIntent

# 执行方式常量（与 logs 里的 selected_method 一致）
UNREAL_MCP = "UNREAL_MCP"
UE_PYTHON = "UE_PYTHON"
UE_COMMANDLET = "UE_COMMANDLET"
KEYBOARD = "KEYBOARD"
MOUSE = "MOUSE"
VISION = "VISION"
HYBRID = "HYBRID"

ALL_METHODS = (UNREAL_MCP, UE_PYTHON, UE_COMMANDLET, KEYBOARD, MOUSE, VISION, HYBRID)

#: 结构化通道（能给出精确值、可程序化验证）
STRUCTURED = (UNREAL_MCP, UE_PYTHON)
#: GUI 通道（快但靠不住，需要视觉兜底）
GUI = (MOUSE, KEYBOARD)


@dataclass
class Candidate:
    method: str
    reason: str
    bias: float = 0.0
    rule: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"method": self.method, "reason": self.reason, "bias": self.bias, "rule": self.rule}


# --- 单条规则 -----------------------------------------------------------------


def rule_strong_shortcut(intent: TaskIntent) -> Candidate | None:
    """已知快捷键且单步可完成 → KEYBOARD。

    这是最便宜的一条路：1~2 步、无需截图、无需精确数值。
    （典型：保存 Level 用 Ctrl+S）
    """
    if intent.known_shortcut and intent.target_count <= 1 and not intent.needs_exact_values:
        return Candidate(
            KEYBOARD,
            f"已知快捷键 {intent.known_shortcut!r} 且单步可完成（1~2 步，无需截图）",
            bias=0.45,
            rule="strong_shortcut",
        )
    return None


def rule_ui_only(intent: TaskIntent) -> Candidate | None:
    """只能通过 GUI 完成（第三方插件面板、右键菜单、视口 gizmo）→ MOUSE。"""
    if intent.ui_only:
        return Candidate(
            MOUSE,
            "任务只涉及 UI 交互（面板/菜单/视口拖拽），结构化通道够不着",
            bias=0.40,
            rule="ui_only",
        )
    return None


def rule_vision_required(intent: TaskIntent) -> Candidate | None:
    """需要审美/空间判断 → VISION（并暗示应走 HYBRID 才有意义）。"""
    if intent.needs_vision:
        return Candidate(
            VISION,
            "任务包含主观判断（悬空/穿模/比例/光照/观感），必须先看画面",
            bias=0.50,
            rule="vision_required",
        )
    return None


def rule_hybrid_for_vision(intent: TaskIntent) -> Candidate | None:
    """既要看、又要精确改 → HYBRID（看归 VISION，改归结构化通道）。"""
    if intent.needs_vision and intent.needs_exact_values and not intent.read_only:
        return Candidate(
            HYBRID,
            "既要视觉判断又要精确修改：视觉负责定位，结构化通道负责改值",
            bias=0.55,
            rule="hybrid_for_vision",
        )
    return None


def rule_batch(intent: TaskIntent) -> Candidate | None:
    """批量（>=5）→ 结构化通道。GUI 逐个点必然又慢又容易错。"""
    if intent.is_batch:
        return Candidate(
            UNREAL_MCP,
            f"批量 {intent.target_count} 个对象：脚本/结构化通道一步完成，GUI 会线性放大",
            bias=0.50,
            rule="batch",
        )
    return None


def rule_exact_values(intent: TaskIntent) -> Candidate | None:
    """需要精确数值修改 → 结构化通道。"""
    if intent.needs_exact_values and not intent.ui_only and intent.skill in {"actor_move", "actor_inspect"}:
        return Candidate(
            UNREAL_MCP,
            "需要精确数值（坐标/旋转/缩放），只有结构化通道能保证",
            bias=0.35,
            rule="exact_values",
        )
    return None


def rule_known_actor(intent: TaskIntent) -> Candidate | None:
    """有明确 Actor 名 → 结构化通道可以一步定位，不必靠找。"""
    if intent.known_actor:
        return Candidate(
            UNREAL_MCP,
            f"已知 Actor 名称 {intent.known_actor!r}：结构化通道可直接寻址，无需 GUI 搜索",
            bias=0.30,
            rule="known_actor",
        )
    return None


def rule_read_only(intent: TaskIntent) -> Candidate | None:
    """只读查询 → 结构化通道最快且零风险。"""
    if intent.read_only and not intent.ui_only:
        return Candidate(
            UNREAL_MCP,
            "只读查询：结构化通道无副作用、零步骤开销",
            bias=0.30,
            rule="read_only",
        )
    return None


def rule_save_shortcut(intent: TaskIntent) -> Candidate | None:
    """保存类任务：Ctrl+S 曾是默认捷径，但本机 Phase 3 实测 KEYBOARD 首选失败率 100%。

    规则仍提供 KEYBOARD 候选，但 bias 降低；结构化保存另有更高 bias 规则。
    最终是否冷却由 MethodHealth/quality 在 Router 侧生效，不在业务代码 hard-code。
    """
    if intent.skill == "level_save" and not intent.context.get("prefer_api_save"):
        return Candidate(
            KEYBOARD,
            "保存 Level：Ctrl+S 仍作为 GUI 备选（健康度差时会被 Router 降权）",
            bias=0.12,
            rule="save_shortcut",
        )
    return None


def rule_structured_save(intent: TaskIntent) -> Candidate | None:
    """结构化保存优先：可编程验证（mtime/dirty），不依赖 GUI 键盘链路。"""
    if intent.skill == "level_save":
        return Candidate(
            UNREAL_MCP,
            "保存 Level：优先结构化保存 API，可用 mtime/dirty 独立验证",
            bias=0.48,
            rule="structured_save",
        )
    return None


def rule_batch_skill(intent: TaskIntent) -> Candidate | None:
    """batch_mutation skill 始终走结构化通道（不论本批数量是否达到 is_batch 阈值）。"""
    if intent.skill == "batch_mutation":
        return Candidate(
            UNREAL_MCP,
            "批量修改流水线：结构化后端一次探测 + 批量写入/回读",
            bias=0.55,
            rule="batch_skill",
        )
    return None


RULES: Sequence[Callable[[TaskIntent], Candidate | None]] = (
    rule_hybrid_for_vision,
    rule_vision_required,
    rule_batch_skill,
    rule_structured_save,
    rule_strong_shortcut,
    rule_save_shortcut,
    rule_batch,
    rule_ui_only,
    rule_exact_values,
    rule_known_actor,
    rule_read_only,
)


def evaluate(intent: TaskIntent, *, available: Iterable[str] = ALL_METHODS) -> list[Candidate]:
    """跑完所有规则，去重后返回候选（保留 bias 最高的那条理由）。

    注意：这里**只返回有规则背书的候选**。把"可用但没规则推荐"的方法补进来
    是 `Router.decide` 的职责（`fallback_pool`）——这样规则表保持纯粹，
    而降级阶梯是框架保证的。
    """
    allowed = set(available)
    best: dict[str, Candidate] = {}
    for rule in RULES:
        try:
            cand = rule(intent)
        except Exception:  # 规则自身异常不应影响整体路由
            continue
        if cand is None or cand.method not in allowed:
            continue
        prev = best.get(cand.method)
        if prev is None or cand.bias > prev.bias:
            best[cand.method] = cand

    return sorted(best.values(), key=lambda c: -c.bias)


__all__ = [
    "Candidate",
    "evaluate",
    "RULES",
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
