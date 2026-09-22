"""Router —— 第一版最重要的模块。

输入：`TaskIntent`（任务描述 + 上下文 + 特征）
输出：`Decision`（推荐执行方式 + 理由 + 成本明细 + 备用顺序）

决策步骤（每一步都可解释、可日志化）：

    1. 探测当前**真正可用**的执行方式（后端在不在、冷却期没到）
    2. 跑确定性规则，得到候选 + 人类可读理由      (rules.evaluate)
    3. 对每个候选算成本分                           (cost.CostModel)
    4. 按 `score - bias` 取最优；其余按序作为 fallback
    5. 把决策与完整计算过程写进日志（含被淘汰方案的原因）

第一版刻意**不引入模型**：规则跑稳定了，后面再谈学习权重。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..core.errors import NoViableMethod
from . import rules
from .capability_cache import get_capability_cache
from .cost import CostBreakdown, CostModel
from .fallback import MethodHealth
from .intent import TaskIntent


@dataclass
class Decision:
    """Router 的输出。"""

    method: str
    reason: str
    rule: str = ""
    score: float = 0.0
    cost: CostBreakdown | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    considered: list[dict[str, Any]] = field(default_factory=list)
    task_category: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def fallback_order(self) -> list[str]:
        return [a["method"] for a in self.alternatives]

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected_method": self.method,
            "reason": self.reason,
            "rule": self.rule,
            "score": round(self.score, 4),
            "cost": self.cost.as_dict() if self.cost else None,
            "alternatives": self.alternatives,
            "considered": self.considered,
            "task_category": self.task_category,
            "signals": self.signals,
        }

    def explain(self) -> str:
        """一行人类可读解释，直接进日志。"""
        return f"{self.method}  <- {self.reason}"


class Router:
    """确定性规则 Router。"""

    def __init__(
        self,
        *,
        weights: Mapping[str, float] | None = None,
        health: MethodHealth | None = None,
        cost_model: CostModel | None = None,
        capability_cache: Any | None = None,
        capability_ttl_s: float | None = None,
    ):
        self.cost_model = cost_model or CostModel(weights)
        self.health = health
        self.capability_cache = capability_cache
        if self.capability_cache is None and capability_ttl_s is not None:
            self.capability_cache = get_capability_cache(capability_ttl_s)

    # -- 可用性 ---------------------------------------------------------------

    def available_methods(
        self,
        *,
        backends: Mapping[str, Any] | None = None,
        computer_use_available: bool = False,
        vision_available: bool = False,
        use_cache: bool = False,
        force_probe: bool = False,
    ) -> dict[str, bool]:
        """探测当前哪些执行方式真的可用。

        `backends` 形如 `{"UNREAL_MCP": backend_obj, "UE_PYTHON": backend_obj, ...}`。
        Session capability cache 可避免每次 decide 都做重探测；失败会 invalidate。
        """
        def _probe() -> Mapping[str, Any]:
            avail: dict[str, bool] = {}
            for method in (rules.UNREAL_MCP, rules.UE_PYTHON, rules.UE_COMMANDLET):
                backend = (backends or {}).get(method)
                if backend is None:
                    avail[method] = False
                    continue
                try:
                    avail[method] = bool(backend.available())
                except Exception as exc:  # noqa: BLE001
                    avail[method] = False
                    if self.capability_cache is not None:
                        self.capability_cache.invalidate(reason=f"{method}:{exc}")
            avail[rules.MOUSE] = bool(computer_use_available)
            avail[rules.KEYBOARD] = bool(computer_use_available)
            avail[rules.VISION] = bool(vision_available or computer_use_available)
            avail[rules.HYBRID] = any(
                avail.get(m) for m in (rules.UNREAL_MCP, rules.UE_PYTHON, rules.UE_COMMANDLET)
            )
            return avail

        if use_cache and self.capability_cache is not None:
            snap = self.capability_cache.get(_probe, force=force_probe)
            return dict(snap.ready)
        return dict(_probe())

    def _cooldown_filter(self, avail: Mapping[str, bool]) -> dict[str, bool]:
        if self.health is None:
            return dict(avail)
        out = {}
        for method, ok in avail.items():
            out[method] = bool(ok) and not self.health.in_cooldown(method)
        return out

    # -- 决策 -----------------------------------------------------------------

    def decide(
        self,
        intent: TaskIntent,
        *,
        backends: Mapping[str, Any] | None = None,
        computer_use_available: bool = False,
        vision_available: bool = False,
        task_category: str | None = None,
        use_capability_cache: bool = True,
        force_probe: bool = False,
    ) -> Decision:
        """单 Router 多信号决策。

        信号（最终仍只由本方法输出一个 Decision）：
          A 规则判断 / B 成本与步骤 / C Method Health（可按任务类别）
          D 同类历史成功率 / E 是否要精确 / F 是否要视觉 / G 是否批量
          H 可用性快照 / I 操作风险
        """
        t_probe = time.perf_counter() if False else None
        raw_avail = self.available_methods(
            backends=backends,
            computer_use_available=computer_use_available,
            vision_available=vision_available,
            use_cache=use_capability_cache,
            force_probe=force_probe,
        )
        # 多信号 I：冷却过滤（支持 task_category 细分账本）
        if self.health is None:
            avail = dict(raw_avail)
        else:
            avail = {}
            for method, ok in raw_avail.items():
                cooled = self.health.in_cooldown(method, task_category)
                # 全局 method 键冷却同样生效
                cooled = cooled or self.health.in_cooldown(method, None)
                avail[method] = bool(ok) and not cooled

        allowed = [m for m, ok in avail.items() if ok]
        if not allowed:
            raise NoViableMethod(
                "没有任何可用的执行方式",
                details={"availability": raw_avail, "intent": intent.as_dict()},
            )

        candidates = rules.evaluate(intent, available=allowed)

        # --- 健康度驱动的 bias 调整（不在业务 hard-code 某通道好坏） ----------
        # task_category 细分账本优先；连续失败/低成功率的候选降权。
        if self.health is not None:
            cat_stats = self.health.stats_for_category(task_category) or {}
            global_stats = self.health.stats() or {}
            adjusted: list[rules.Candidate] = []
            for cand in candidates:
                bias = cand.bias
                slot = cat_stats.get(cand.method) or global_stats.get(cand.method) or {}
                fails = int(slot.get("consecutive_failures") or 0)
                oks = int(slot.get("ok") or 0)
                failed = int(slot.get("failed") or 0)
                total = oks + failed
                rate = (oks / total) if total else None
                if fails >= 2:
                    bias -= 0.25
                    cand.reason = f"{cand.reason}（健康度：连续失败 {fails}，降权）"
                elif rate is not None and total >= 2 and rate < 0.5:
                    bias -= 0.15
                    cand.reason = f"{cand.reason}（健康度：成功率 {rate:.0%}，降权）"
                adjusted.append(rules.Candidate(cand.method, cand.reason, bias=bias, rule=cand.rule))
            candidates = adjusted

        # --- 补全降级阶梯 -----------------------------------------------------
        covered = {c.method for c in candidates}
        missing = [m for m in allowed if m not in covered]
        if missing:
            if not candidates:
                for m in missing:
                    candidates.append(rules.Candidate(m, "无规则命中：按各通道固有成本排序", bias=0.0, rule="cost_only"))
            else:
                for m in missing:
                    candidates.append(
                        rules.Candidate(m, "规则未推荐：作为降级备选（首选失败后启用）", bias=-0.5, rule="fallback_pool")
                    )

        # 多信号 D：优先读任务类别账本，缺省回落全局
        stats = None
        if self.health is not None:
            stats = self.health.stats_for_category(task_category) or self.health.stats()
        attempts = int(intent.context.get("attempts", 0) or 0)

        considered: list[dict[str, Any]] = []
        scored: list[tuple[float, Any, CostBreakdown]] = []
        for cand in candidates:
            cost = self.cost_model.estimate(
                intent,
                cand.method,
                stats=stats,
                available=avail.get(cand.method, False),
                attempts=attempts,
                cooldown=self.health.in_cooldown(cand.method, task_category) if self.health else False,
                task_category=task_category,
            )
            # 多信号：规则 bias + 成本 score
            effective = cost.score - cand.bias
            scored.append((effective, cand, cost))
            considered.append(
                {
                    **cand.as_dict(),
                    "score": round(cost.score, 4),
                    "effective": round(effective, 4),
                    "cost": cost.as_dict(),
                    "signals": {
                        "rule": cand.rule,
                        "bias": cand.bias,
                        "task_category": task_category,
                        "needs_exact": intent.needs_exact_values,
                        "needs_vision": intent.needs_vision,
                        "is_batch": intent.is_batch,
                        "read_only": intent.read_only,
                        "risk": cost.components.get("risk"),
                        "success_gap": cost.components.get("success"),
                    },
                    "rejected_because": None,
                }
            )

        scored.sort(key=lambda t: t[0])
        best_eff, best_cand, best_cost = scored[0]

        for item in considered:
            if item["method"] == best_cand.method:
                item["selected"] = True
                continue
            item["selected"] = False
            item["rejected_because"] = _why_not(intent, item)

        alternatives = [
            {"method": c.method, "reason": c.reason, "score": round(cost.score - c.bias, 4)}
            for _, c, cost in scored[1:]
        ]

        return Decision(
            method=best_cand.method,
            reason=best_cand.reason,
            rule=best_cand.rule,
            score=round(best_eff, 4),
            cost=best_cost,
            alternatives=alternatives,
            considered=considered,
            task_category=task_category,
            signals={
                "availability": raw_avail,
                "task_category": task_category,
                "selected_signals": next(
                    (c.get("signals") for c in considered if c.get("selected")), {}
                ),
            },
        )

    # -- 反馈 -----------------------------------------------------------------

    def feedback(self, method: str, *, ok: bool, error_code: str | None = None) -> None:
        """执行结果回灌钩子。

        **健康度账本（MethodHealth）由 Executor 唯一写入**。
        这里刻意不再 `health.record`：Router 与 Executor 通常共享同一个
        MethodHealth，若两处都写，一次真实失败会让 `consecutive_failures`
        直接 +2——阈值为 2 时等于**失败一次就冷却**，会把好通道提前隔离
        （F5 同类问题的隐蔽变体）。此方法保留给未来的学习/探索扩展点。
        """
        return


def _why_not(intent: TaskIntent, item: Mapping[str, Any]) -> str:
    """给被淘汰的方案生成一句可读的否定理由。"""
    cost = item.get("cost") or {}
    notes = cost.get("notes") or []
    method = item.get("method")
    if notes:
        return "；".join(notes[:2])
    extra = cost.get("components") or {}
    return f"综合成本高于首选（score={item.get('effective')}，分量={ {k: round(v,3) for k,v in extra.items()} }）"


__all__ = ["Router", "Decision"]
