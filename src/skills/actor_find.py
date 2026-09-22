"""Skill: 查找 Actor。"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.errors import NotSupported, ToolCallFailed
from ..router.intent import TaskIntent
from ..validation.result import CheckResult
from ..validation.verifiers import Verifier, check_actor_found
from . import gui_helpers
from .base import Skill, SkillContext, SkillTrace


class ActorFindSkill(Skill):
    """按名字找到 Actor，并给出它的完整 transform。

    降级阶梯：
        UNREAL_MCP / UE_PYTHON / UE_COMMANDLET  结构化精确查找
        MOUSE                                   在 World Outliner 搜索框里搜（需标定 ROI）
    """

    name = "actor_find"
    description = "按标签/名字查找 Level 中的 Actor"
    keywords = ("查找", "找到", "寻找", "find", "定位", "搜索")
    preferred_methods = ("UNREAL_MCP", "UE_PYTHON")
    supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "MOUSE"})

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        return True  # 只读 query

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        return "actor_read"

    def intent(self, params: Mapping[str, Any], **overrides: Any) -> TaskIntent:
        name = str(params.get("actor") or params.get("name") or "")
        kw: dict[str, Any] = {
            "skill": self.name,
            "description": f"查找 Actor {name!r}",
            "target_count": 1,
            "needs_exact_values": True,
            "known_actor": name or None,
            "read_only": True,
        }
        kw.update(overrides)
        return TaskIntent(**kw)

    # -- 执行 ----------------------------------------------------------------

    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> dict[str, Any]:
        name = str(params.get("actor") or params.get("name") or "")
        if not name:
            raise NotSupported("actor_find 需要 actor/name 参数")

        if method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            backend = ctx.structured(method)
            ref = backend.find_actor(name)
            if ref is None:
                raise ToolCallFailed(f"未找到 Actor {name!r}")
            payload = ref.as_dict()
            trace.after["transform"] = payload
            trace.note(f"结构化查找命中：{payload.get('label') or payload.get('name')}")
            return {"ok": True, "actor": payload, "_transport": f"structured:{method}"}

        if method == "MOUSE":
            # GUI 只能"把候选项筛出来"，无法给出精确 transform —— 如实标注
            res = gui_helpers.outliner_search(ctx, name)
            trace.note("GUI 查找：仅在 World Outliner 里做了过滤，未取得数值")
            return {"ok": True, "gui_only": True, "actor": {"label": name}, **res}

        raise NotSupported(f"actor_find 不支持 {method}")

    # -- 验证 ----------------------------------------------------------------

    def verify(
        self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace
    ) -> Any:
        v = Verifier()
        if method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            name = str(params.get("actor") or params.get("name") or "")
            v.read("ref", lambda: ctx.structured(method).get_actor_transform(name))
            v.check(lambda vals: check_actor_found(vals.get("ref"), label=name))
        else:
            v.add(CheckResult(
                "actor_find.gui_evidence", passed=False, skipped=True,
                reason="GUI 查找只做了列表过滤，没有可用于验证的精确读取通道",
            ))
        return v.run()


__all__ = ["ActorFindSkill"]
