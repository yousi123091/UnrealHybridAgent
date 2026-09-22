"""降级管理器。

需求里的降级链（§8）与硬性预算：

    UNREAL_MCP 失败  ->  UE_PYTHON
    UE_PYTHON  失败  ->  GUI (MOUSE / KEYBOARD)
    GUI 找不到对象    ->  screenshot + VISION
    VISION 无法定位   ->  World Outliner 搜索（精细 MOUSE + KEYBOARD）

    max_attempts_per_method = 2
    max_total_fallbacks     = 5

两个容易踩的坑，都在这层解决：

1. **同一种方案不要无限重试** —— 每个方法单独计数，超限即永久弃用（本次任务内）。
2. **失败次数要跨任务累计到"方法冷却"** —— 否则下一个任务还会傻乎乎地再试一遍
   刚失败两次的方法。冷却由 Router 持久化在 `.state/routing_stats.json`。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.errors import BackendUnavailable, FallbackExhausted, NotSupported
from . import rules

#: 默认降级链。键是"当前失败的方法"，值是"下一步试什么"，按顺序。
DEFAULT_CHAIN: dict[str, tuple[str, ...]] = {
    rules.UNREAL_MCP: (rules.UE_PYTHON, rules.MOUSE, rules.VISION),
    rules.UE_PYTHON: (rules.UNREAL_MCP, rules.MOUSE, rules.VISION),
    rules.UE_COMMANDLET: (rules.UE_PYTHON, rules.UNREAL_MCP, rules.MOUSE),
    rules.MOUSE: (rules.KEYBOARD, rules.UNREAL_MCP, rules.VISION),
    rules.KEYBOARD: (rules.MOUSE, rules.UNREAL_MCP, rules.VISION),
    rules.VISION: (rules.MOUSE, rules.UNREAL_MCP),
    rules.HYBRID: (rules.UNREAL_MCP, rules.UE_PYTHON, rules.MOUSE, rules.VISION),
}

#: 这些错误说明"这条路根本走不通"，应立刻换方法，不要重试。
FATAL_CODES = {
    "BACKEND_UNAVAILABLE",
    "NOT_SUPPORTED",
    "NO_VIABLE_METHOD",
    "SAFETY_REFUSED",
}


@dataclass
class Attempt:
    method: str
    ok: bool
    error_code: str | None = None
    detail: str = ""
    duration_ms: float | None = None


@dataclass
class FallbackState:
    """一个任务内的降级状态。

    ``attempts`` 是**总尝试次数**（含成功），只用于展示；
    ``failures`` 才是**判定弃用的依据**——否则一个连续成功的通道
    会在跑满两次之后被误判为"已耗尽"，后续步骤直接跳过它
    （真实踩过的坑：demo1 里 UNREAL_MCP 成功两步后，第三步保存被跳过）。
    """

    attempts: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    history: list[Attempt] = field(default_factory=list)
    exhausted: set[str] = field(default_factory=set)
    total_fallbacks: int = 0

    def attempts_for(self, method: str) -> int:
        return self.attempts.get(method, 0)

    def failures_for(self, method: str) -> int:
        return self.failures.get(method, 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": dict(self.attempts),
            "failures": dict(self.failures),
            "total_fallbacks": self.total_fallbacks,
            "exhausted": sorted(self.exhausted),
            "history": [
                {"method": a.method, "ok": a.ok, "error_code": a.error_code, "detail": a.detail[:200]}
                for a in self.history
            ],
        }


class FallbackManager:
    """管理一次任务内的重试与降级。"""

    def __init__(
        self,
        *,
        max_attempts_per_method: int = 2,
        max_total_fallbacks: int = 5,
        chain: Mapping[str, Iterable[str]] | None = None,
    ):
        self.max_attempts_per_method = int(max_attempts_per_method)
        self.max_total_fallbacks = int(max_total_fallbacks)
        self.chain = {k: tuple(v) for k, v in (chain or DEFAULT_CHAIN).items()}
        self.state = FallbackState()

    # -- 记录 -----------------------------------------------------------------

    def record(self, method: str, *, ok: bool, error_code: str | None = None, detail: str = "", duration_ms: float | None = None) -> Attempt:
        att = Attempt(method=method, ok=ok, error_code=error_code, detail=detail, duration_ms=duration_ms)
        self.state.attempts[method] = self.state.attempts_for(method) + 1
        self.state.history.append(att)
        if ok:
            # 成功不消耗重试预算：这个通道刚刚证明了自己是好的
            return att
        self.state.failures[method] = self.state.failures_for(method) + 1
        if error_code in FATAL_CODES:
            # 结构性失败：这条路不会再变好，直接判死
            self.state.exhausted.add(method)
        elif self.state.failures_for(method) >= self.max_attempts_per_method:
            self.state.exhausted.add(method)
        return att

    # -- 判定 -----------------------------------------------------------------

    def can_try(self, method: str) -> bool:
        return method not in self.state.exhausted and self.state.failures_for(method) < self.max_attempts_per_method

    def reset(self) -> None:
        """开始一个新任务：清空降级状态。

        每个任务（一次 skill 执行）都是独立预算，否则前一步的成功/失败
        会污染后一步的选路——这正是"第三步保存跳过 UNREAL_MCP"的根因之一。
        """
        self.state = FallbackState()

    def is_fatal(self, error_code: str | None) -> bool:
        return error_code in FATAL_CODES

    def budget_left(self) -> int:
        return max(0, self.max_total_fallbacks - self.state.total_fallbacks)

    # -- 下一步 ---------------------------------------------------------------

    def next_candidate(
        self,
        failed_method: str,
        *,
        available: Iterable[str],
        extra_order: Iterable[str] = (),
        error_code: str | None = None,
    ) -> str | None:
        """决定下一步用哪种方法。返回 None 表示降级预算用尽。"""
        if self.budget_left() <= 0:
            return None

        allowed = set(available)
        # 先走配置链，再走 Router 给的备用顺序
        order = list(self.chain.get(failed_method, ())) + [m for m in extra_order if m != failed_method]

        for method in order:
            if method == failed_method or method not in allowed:
                continue
            if not self.can_try(method):
                continue
            self.state.total_fallbacks += 1
            return method
        return None

    def note_fallback(self) -> None:
        """记一次降级（由调用方在真正切换方法时调用，便于预算控制）。"""
        self.state.total_fallbacks += 1

    def order_for(self, preferred: str, *, available: Iterable[str], extra_order: Iterable[str] = ()) -> list[str]:
        """给出完整尝试顺序（首选 + 降级链），用于日志和 CLI 展示。"""
        allowed = set(available)
        seq = [preferred] if preferred in allowed else []
        for m in list(self.chain.get(preferred, ())) + list(extra_order):
            if m in allowed and m not in seq:
                seq.append(m)
        return seq

    def has_budget(self) -> bool:
        return self.budget_left() > 0

    def raise_if_exhausted(self, *, available: Iterable[str]) -> None:
        """降级预算用尽、且没有任何方法还能试时抛出。"""
        if self.budget_left() > 0:
            return
        if any(self.can_try(m) for m in available):
            return
        raise FallbackExhausted("所有降级路线已用尽", details=self.state.as_dict())


# --- 方法健康度（跨任务持久化） ------------------------------------------------


class MethodHealth:
    """跨任务的方法健康度：连续失败计数 + 冷却。

    存 `.state/routing_stats.json`。键支持：

    * ``METHOD``                     —— 兼容旧全局账本
    * ``task_category::METHOD``      —— 按任务类型细分（actor_read::UNREAL_MCP）

    用途：
      * 某个方法（在某类任务上）连续失败 N 次 -> 冷却；
      * 避免「通道不会干 A」被记成「通道整体坏了」。

    ``UNEXPECTED`` / ``NOT_SUPPORTED`` 由 Executor 排除，不进本账本。
    """

    def __init__(self, path: str | Path, *, cooldown_s: float = 60.0, fail_threshold: int = 2):
        self.path = Path(path)
        self.cooldown_s = float(cooldown_s)
        self.fail_threshold = int(fail_threshold)
        self.data: dict[str, Any] = {"methods": {}, "updated": None}
        self.load()

    @staticmethod
    def key(method: str, task_category: str | None = None) -> str:
        cat = (task_category or "").strip()
        if not cat or cat in ("general", "unknown"):
            return str(method)
        return f"{cat}::{method}"

    @staticmethod
    def method_of(key: str) -> str:
        return str(key).split("::", 1)[-1]

    def load(self) -> None:
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        self.data.setdefault("methods", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated"] = time.time()
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _slot(self, key: str) -> dict[str, Any]:
        return self.data["methods"].setdefault(
            key,
            {
                "ok": 0, "failed": 0, "consecutive_failures": 0,
                "cooldown_until": 0.0, "last_error": None, "method": self.method_of(key),
            },
        )

    def record(
        self,
        method: str,
        *,
        ok: bool,
        error_code: str | None = None,
        task_category: str | None = None,
    ) -> None:
        key = self.key(method, task_category)
        slot = self._slot(key)
        if ok:
            slot["ok"] += 1
            slot["consecutive_failures"] = 0
            slot["last_error"] = None
            slot["cooldown_until"] = 0.0
        else:
            slot["failed"] += 1
            slot["consecutive_failures"] += 1
            slot["last_error"] = error_code
            if slot["consecutive_failures"] >= self.fail_threshold:
                slot["cooldown_until"] = time.time() + self.cooldown_s
        # 兼容：无类别时同时维护裸 method 槽位
        if task_category and self.key(method, task_category) != method:
            self.save()
            return
        self.save()

    def in_cooldown(self, method: str, task_category: str | None = None) -> bool:
        """任一相关键（精确类别 / 裸 method）仍在冷却则视为冷却中。"""
        keys = [self.key(method, task_category), str(method)]
        if task_category:
            keys.append(self.key(method, task_category))
        cooling = False
        for key in dict.fromkeys(keys):
            slot = self.data["methods"].get(key)
            if not slot:
                continue
            until = float(slot.get("cooldown_until") or 0.0)
            if until <= 0:
                continue
            if time.time() > until:
                slot["cooldown_until"] = 0.0
                slot["consecutive_failures"] = 0
                self.save()
                continue
            cooling = True
        return cooling

    def clear(self) -> None:
        self.data = {"methods": {}, "updated": None}
        self.save()

    def stats(self) -> dict[str, Mapping[str, Any]]:
        return {k: v for k, v in self.data["methods"].items()}

    def stats_for_category(self, task_category: str | None) -> dict[str, Mapping[str, Any]]:
        """供 CostModel 使用：method -> 该类别下的槽位（缺省回落到全局 method）。"""
        out: dict[str, Mapping[str, Any]] = {}
        for key, slot in self.data["methods"].items():
            method = self.method_of(key)
            cat = key.split("::", 1)[0] if "::" in key else None
            if task_category and cat and cat != task_category:
                continue
            if task_category and cat is None:
                out.setdefault(method, slot)
                continue
            if not task_category:
                out[method] = slot
            else:
                out[method] = slot
        return out

    def success_rates(self, task_category: str | None = None) -> dict[str, float]:
        out: dict[str, float] = {}
        for method, slot in self.stats_for_category(task_category).items():
            total = int(slot.get("ok", 0)) + int(slot.get("failed", 0))
            if total:
                out[method] = int(slot.get("ok", 0)) / total
        return out


__all__ = [
    "FallbackManager",
    "FallbackState",
    "Attempt",
    "MethodHealth",
    "DEFAULT_CHAIN",
    "FATAL_CODES",
]
