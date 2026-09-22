"""Skill 基类。

一个 Skill = **一件语义明确的事**（移动 Actor / 保存关卡 / 视觉巡检），
它自己不含路由逻辑，只声明：

    * ``intent()``   —— 这件事有哪些特征（要不要精确值、要不要看画面、几个目标）
    * ``perform()``  —— 用指定方式**怎么做**
    * ``verify()``   —— 做完之后**怎么独立确认**

"选哪种方式"由 Router 决定，"失败怎么换"由 FallbackManager 决定，
"什么时候算完成"由 Skill 的 verify 决定。三者互不知道对方实现，
这是整个项目能持续演进的关键。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..adapters.base import UnrealBackend
from ..core.config import Config
from ..core.errors import NotSupported
from ..core.log import RunLogger
from ..router.intent import TaskIntent
from ..validation.result import VerifyReport
from ..vision.image import save_b64_png


@dataclass
class SkillContext:
    """Skill 运行时能用到的全部外部资源。"""

    bundle: Any                                   # runtime.BackendBundle
    config: Config
    logger: RunLogger
    artifacts_dir: Path
    dry_run: bool = False
    _artifact_index: list[Path] = field(default_factory=list)

    # -- 后端取用 ------------------------------------------------------------

    def structured(self, method: str) -> UnrealBackend:
        backend = self.bundle.unreal_for(method)
        if not backend.available():
            from ..core.errors import BackendUnavailable

            raise BackendUnavailable(
                f"{method} 后端不可用", details=backend.diagnostics()
            )
        return backend

    def structured_or_none(self, method: str) -> UnrealBackend | None:
        try:
            backend = self.bundle.unreal_for(method)
        except NotSupported:
            return None
        try:
            return backend if backend.available() else None
        except Exception:
            return None

    def gui(self) -> Any:
        cu = self.bundle.computer_use
        if cu is None:
            from ..core.errors import BackendUnavailable

            raise BackendUnavailable("Computer Use 未装配，GUI 方式不可用")
        cu.connect(required=True)
        return cu

    def gui_or_none(self) -> Any | None:
        cu = self.bundle.computer_use
        if cu is None:
            return None
        try:
            cu.connect(required=False)
            return cu
        except Exception:
            return None

    # -- 产物 ----------------------------------------------------------------

    def artifact_path(self, name: str) -> Path:
        path = Path(self.artifacts_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def save_screenshot(self, b64_png: str, name: str) -> Path:
        path = save_b64_png(b64_png, self.artifact_path(name))
        self._artifact_index.append(path)
        return path

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.artifact_path(name)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._artifact_index.append(path)
        return path

    @property
    def artifacts(self) -> list[Path]:
        return list(self._artifact_index)


@dataclass
class SkillTrace:
    """perform 与 verify 之间的状态传递。"""

    before: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def as_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "result": self.result,
            "after": self.after,
            "artifacts": self.artifacts,
            "notes": self.notes,
        }


@dataclass
class SkillResult:
    """一次 Skill 执行的完整结果。"""

    skill: str
    ok: bool
    method: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    verification: VerifyReport | None = None
    error: str | None = None
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "ok": self.ok,
            "method": self.method,
            "attempts": self.attempts,
            "data": self.data,
            "verification": self.verification.as_dict() if self.verification else None,
            "verification_summary": self.verification.summary() if self.verification else None,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
        }

    def summary(self) -> str:
        head = f"{self.skill}: {'成功' if self.ok else '失败'}"
        if self.method:
            head += f" · 方式={self.method}"
        if len(self.attempts) > 1:
            head += f" · 尝试 {len(self.attempts)} 次（含降级）"
        if self.verification:
            head += f" · {self.verification.summary()}"
        if self.error:
            head += f" · 错误={self.error}"
        return head


class Skill(ABC):
    """语义动作。"""

    name: str = "skill"
    description: str = ""
    #: 自然语言里出现这些词就大概率是这个 skill（planner 用）
    keywords: tuple[str, ...] = ()
    #: 这个 skill 倾向用哪些方法（仅作为 intent 的上下文提示，不构成硬约束）
    preferred_methods: tuple[str, ...] = ()
    #: 这个 skill **真正支持**的执行方式。None = 不过滤（交给 perform 自己报 NotSupported）。
    #: Router 只知道通道是否在线，不知道 skill 认不认某种 method；不声明的话，
    #: 降级链会把 HYBRID/KEYBOARD 排进只读 skill，白耗预算并制造误导性错误。
    supported_methods: frozenset[str] | None = None

    # -- 声明 ----------------------------------------------------------------

    @abstractmethod
    def intent(self, params: Mapping[str, Any], **overrides: Any) -> TaskIntent:
        """把参数翻译成 Router 能吃的意图。"""

    @abstractmethod
    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> dict[str, Any]:
        """用 ``method`` 指定的方式执行。抛异常 = 这次方式失败，交给降级。"""

    # -- 可选 ----------------------------------------------------------------

    def verify(
        self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace
    ) -> VerifyReport | None:
        """独立验证。返回 None 表示"这个 skill 不做验证"（会被如实记成未验证）。"""
        return None

    def preflight(self, ctx: SkillContext, params: Mapping[str, Any]) -> dict[str, Any]:
        """动手之前先采一份基线状态（用于验证时做前后对比）。"""
        return {}

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        """重复执行一次，结果是否与执行一次相同。

        **默认 False（保守）**：新增 mutation skill 不得依赖「默认 True」。
        只读 / 可重复保存 等 skill 必须**显式**返回 True。

        * 绝对 set_location → True
        * 相对 delta → False（F3：重试会重复生效）
        * 只读 query → True
        * save → True
        * spawn / duplicate → False
        """
        return False

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        """健康度 / Router 质量统计用的任务类别。子类可覆盖。"""
        from ..router.quality import skill_category

        params = dict(params or {})
        intent_ro = bool(getattr(self, "_last_intent_read_only", False))
        return skill_category(
            self.name,
            read_only=intent_ro or str(self.name) in ("actor_find", "actor_inspect", "visual_inspect"),
            ui_only=bool(params.get("ui_only")),
            needs_vision=bool(params.get("visual") or params.get("needs_vision")),
            is_batch=bool(params.get("batch") or (int(params.get("target_count") or 0) >= 5)),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
            "preferred_methods": list(self.preferred_methods),
            "supported_methods": sorted(self.supported_methods) if self.supported_methods else None,
        }


__all__ = ["Skill", "SkillContext", "SkillTrace", "SkillResult"]
