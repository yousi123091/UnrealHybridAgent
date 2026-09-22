"""Skill: 检查 Actor（只读）。"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.errors import NotSupported
from ..router.intent import TaskIntent
from ..validation.result import CheckResult
from ..validation.verifiers import Verifier, check_actor_found
from ..vision.judge import Observation, summarise
from ..vision.judge import judge_floating
from . import gui_helpers
from .base import Skill, SkillContext, SkillTrace


class ActorInspectSkill(Skill):
    """读取 Actor 的 transform / 包围盒，并可选做一次"浮空体检"。

    这是**零副作用**操作，所以结构化通道永远优先；GUI 只在结构化全挂时兜底，
    而且兜底结果会被明确标注为"没有数值"。
    """

    name = "actor_inspect"
    description = "读取 Actor 的变换/包围盒，或对整个 Level 做一次体检"
    keywords = ("查看", "检查", "读取", "inspect", "transform", "列出")
    preferred_methods = ("UNREAL_MCP", "UE_PYTHON")
    supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "MOUSE", "VISION"})

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        return True  # 只读 query

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        return "actor_read"

    def intent(self, params: Mapping[str, Any], **overrides: Any) -> TaskIntent:
        name = params.get("actor") or params.get("name")
        scan_all = bool(params.get("all"))
        kw: dict[str, Any] = {
            "skill": self.name,
            "description": f"读取 {name!r} 的变换" if name else "体检当前 Level",
            "target_count": int(params.get("limit") or (500 if scan_all else 1)),
            "needs_exact_values": True,
            "known_actor": str(name) if name else None,
            "read_only": True,
        }
        kw.update(overrides)
        return TaskIntent(**kw)

    # -- 执行 ----------------------------------------------------------------

    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> dict[str, Any]:
        name = params.get("actor") or params.get("name")

        if method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            backend = ctx.structured(method)
            if params.get("all"):
                rows = backend.get_actors(limit=int(params.get("limit") or 2000))
                trace.after["actors"] = [a.as_dict() for a in rows]
                trace.note(f"读到 {len(rows)} 个 Actor")
                out: dict[str, Any] = {"ok": True, "count": len(rows), "actors": [a.as_dict() for a in rows]}
                if params.get("check_floating", True):
                    out["floating"] = self._floating_from_rows(trace.after["actors"], params)
                    trace.after["floating"] = out["floating"]
                return out

            if not name:
                raise NotSupported("actor_inspect 需要 actor/name，或传 all=true 做全量体检")
            ref = backend.get_actor_transform(name)
            payload = ref.as_dict()
            trace.after["transform"] = payload
            trace.note(f"读到 {name!r}: {payload['location']}")
            return {"ok": True, "actor": payload}

        if method == "MOUSE":
            res = gui_helpers.outliner_search(ctx, str(name or ""))
            trace.note("GUI 巡检：只截了图，没有数值")
            return {"ok": True, "gui_only": True, **res}

        if method == "VISION":
            shot, path = gui_helpers.desktop_screenshot(ctx, "inspect_screen.png")
            return {"ok": True, "screenshot": str(path), "size": [shot.width, shot.height], "gui_only": True}

        raise NotSupported(f"actor_inspect 不支持 {method}")

    # -- 验证 ----------------------------------------------------------------

    def verify(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> Any:
        v = Verifier()
        if method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            if params.get("all"):
                got = trace.after.get("actors") or []
                v.add(CheckResult(
                    "inspect.actors_read", passed=bool(got),
                    expected="至少读到 1 个 Actor", actual=len(got),
                ))
                if params.get("check_floating", True):
                    fl = (trace.after.get("floating") or {}).get("floating_count")
                    v.add(CheckResult(
                        "inspect.floating_reported", passed=fl is not None,
                        expected="给出浮空结论", actual=fl,
                        detail=f"疑似浮空 {fl} 个" if fl is not None else "未计算",
                    ))
            else:
                name = str(params.get("actor") or params.get("name") or "")
                v.read("ref", lambda: ctx.structured(method).get_actor_transform(name))
                v.check(lambda vals: check_actor_found(vals.get("ref"), label=name))
        else:
            v.add(CheckResult(
                "inspect.evidence", passed=False, skipped=True,
                reason=f"{method} 只产出截图/列表过滤，没有可交叉验证的数值通道",
            ))
        return v.run()

    # -- 内部 ----------------------------------------------------------------

    @staticmethod
    def _floating_from_rows(rows: list[Mapping[str, Any]], params: Mapping[str, Any]) -> dict[str, Any]:
        obs = [Observation.from_payload(r) for r in rows]
        tol = float(params.get("ground_tolerance", 5.0))
        cands = judge_floating(obs, ground_tolerance=tol)
        return summarise(cands)


__all__ = ["ActorInspectSkill"]
