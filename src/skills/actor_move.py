"""Skill: 移动 Actor —— 本项目的"主力示范动作"。

为什么挑它当示范：
    * 它有**精确答案**（改完的坐标可以重新读回来比对）；
    * 它同时存在"结构化"与"GUI"两条路（拖动 gizmo / 改 Details 面板）；
    * 它能自然地引出视觉（"改完看起来对吗"）。

各方式的真实能力边界（写死在这里，避免"看起来能跑"的假象）：

    UNREAL_MCP / UE_PYTHON   任意轴向，绝对/相对都行，可精确验证
    HYBRID                   同上 + 前后各一张视口截图，用于人工/视觉复核
    MOUSE                    只能在 Details 面板改 **Z**（面板布局决定），无精确验证
    KEYBOARD                 不支持——移动没有通用快捷键，直接抛错让降级继续往下走
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.errors import NotSupported
from ..router.intent import TaskIntent
from ..validation.result import CheckResult
from ..validation.verifiers import (
    check_axis_delta,
    check_transform_equals,
    check_unchanged,
    check_visual_changed,
)
from ..vision.image import diff, decode_b64_png, load_rgb
from . import gui_helpers
from .base import Skill, SkillContext, SkillTrace

AXIS_INDEX = {"x": 0, "y": 1, "z": 2, "0": 0, "1": 1, "2": 2}


def _axis_index(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 2:
            return value
        raise NotSupported(f"轴向下标越界: {value}")
    key = str(value or "z").strip().lower()
    if key not in AXIS_INDEX:
        raise NotSupported(f"无法识别的轴向 {value!r}，只支持 x/y/z")
    return AXIS_INDEX[key]


class ActorMoveSkill(Skill):
    """移动 Actor（相对偏移或绝对定位）。"""

    name = "actor_move"
    description = "移动 Level 中的 Actor（相对偏移 / 绝对定位）"
    keywords = ("移动", "抬高", "提高", "降低", "调整位置", "位移", "move", "挪")
    preferred_methods = ("UNREAL_MCP", "UE_PYTHON")
    supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "HYBRID", "MOUSE"})

    # -- 意图 ----------------------------------------------------------------

    def intent(self, params: Mapping[str, Any], **overrides: Any) -> TaskIntent:
        name = params.get("actor") or params.get("name")
        kw: dict[str, Any] = {
            "skill": self.name,
            "description": self._describe(params),
            "target_count": 1,
            "needs_exact_values": True,
            "known_actor": str(name) if name else None,
            "read_only": False,
            "needs_vision": bool(params.get("visual") or params.get("needs_vision")),
        }
        kw.update(overrides)
        return TaskIntent(**kw)

    @staticmethod
    def _describe(params: Mapping[str, Any]) -> str:
        name = params.get("actor") or params.get("name") or "?"
        if params.get("location") is not None:
            return f"把 {name} 移动到 {list(params['location'])}"
        axis = str(params.get("axis") or "z").upper()
        delta = float(params.get("delta") or 0.0)
        return f"把 {name} 的 {axis} 轴{'提高' if delta >= 0 else '降低'} {abs(delta):g}"

    # -- 声明 ----------------------------------------------------------------

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        """绝对定位 / place_on_ground 幂等；**相对位移 / 未知写操作不幂等**。

        默认基类是 False；这里对**已知安全**的形态显式返回 True。
        """
        if params.get("location") is not None:
            return True
        if params.get("place_on_ground"):
            return True
        return False

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        return "actor_mutation"

    # -- 基线 ----------------------------------------------------------------

    def preflight(self, ctx: SkillContext, params: Mapping[str, Any]) -> dict[str, Any]:
        name = str(params.get("actor") or params.get("name") or "")
        before: dict[str, Any] = {}
        for method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            backend = ctx.structured_or_none(method)
            if backend is None:
                continue
            try:
                ref = backend.get_actor_transform(name)
                payload = ref.as_dict(include_extra=True)
                before = {"method": method, "transform": payload}
                break
            except Exception:
                continue
        return before

    # -- 执行 ----------------------------------------------------------------

    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> dict[str, Any]:
        name = str(params.get("actor") or params.get("name") or "")
        if not name:
            raise NotSupported("actor_move 需要 actor/name 参数")

        if method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "HYBRID"):
            return self._perform_structured(ctx, method, params, trace, name)

        if method == "MOUSE":
            return self._perform_mouse(ctx, params, trace, name)

        if method == "KEYBOARD":
            raise NotSupported("移动没有通用快捷键，KEYBOARD 方式对 actor_move 不适用")

        if method == "VISION":
            raise NotSupported("纯视觉无法产生精确位移，VISION 不适用于 actor_move")

        raise NotSupported(f"actor_move 不支持 {method}")

    def _resolve_precise_ground(
        self, backend: Any, name: str, params: Mapping[str, Any], before: Mapping[str, Any]
    ) -> tuple[list[float], dict[str, Any]]:
        """place_on_ground：用向下射线求支撑面，返回 (绝对 location, ground_plan)。"""
        from ..vision.ground_place import plan_ground_placement

        loc = list(before.get("location") or (0.0, 0.0, 0.0))
        extra = (
            before.get("world_bounds_extent")
            or before.get("bounds_extent")
            or before.get("extent")
        )
        extent = extra
        if not extent:
            raise NotSupported(
                f"place_on_ground 需要 {name} 的包围盒 extent（preflight/回读里未找到）；"
                "请改用显式 location，或先跑 actor_inspect 带上 extra"
            )
        ignore = list(params.get("ignore_labels") or params.get("exclude_labels") or [])
        line_trace = getattr(backend, "downward_support_hits", None)
        if not callable(line_trace):
            raise NotSupported(
                f"{getattr(backend, 'name', type(backend).__name__)} 不支持 line_trace，无法精确落地"
            )
        hits = line_trace(
            location=loc,
            extent=extent,
            actor_label=name,
            ignore_labels=ignore,
            drop=float(params.get("trace_drop", 5000.0)),
        )
        plan = plan_ground_placement(
            location=loc,
            extent=extent,
            hits=hits,
            exclude_actor=name,
            ignore_hit_labels=ignore,
            min_hits=int(params.get("min_hits", 1)),
            ground_tolerance=float(params.get("tolerance", 5.0)),
        )
        if not plan.get("actionable"):
            raise NotSupported(
                f"精确落地不可执行：{plan.get('reason')}（命中={plan.get('hit_count')}）"
            )
        return [float(v) for v in plan["location"]], plan

    def _perform_structured(
        self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace, name: str
    ) -> dict[str, Any]:
        backend = ctx.structured(method)

        # HYBRID：动手前后各留一张 UE 内部视口截图（若后端支持）
        shots: dict[str, str] = {}
        if method == "HYBRID" and params.get("visual", True):
            shots = self._capture(ctx, backend, trace, "before")

        before = trace.before.get("transform")
        if not before:
            before = backend.get_actor_transform(name).as_dict(include_extra=True)
            trace.before["transform"] = before

        ground_plan = None
        if params.get("place_on_ground"):
            target_list, ground_plan = self._resolve_precise_ground(backend, name, params, before)
            target = target_list
            trace.result["ground_plan"] = {
                k: ground_plan.get(k)
                for k in ("support_z", "gap", "delta_z", "hit_count", "location", "mode", "note")
            }
            trace.note(
                f"{name}: 精确落地 {_fmt(before.get('location') or [])} -> {_fmt(target)}"
                f"（支撑面 Z={ground_plan.get('support_z')}，中位命中 {ground_plan.get('hit_count')} 条）"
            )
        else:
            target = self._target_location(params, before)
            trace.note(f"{name}: {_fmt(before['location'])} -> {_fmt(target)}")

        res = backend.set_actor_location(name, target)
        trace.result["set_actor_location"] = _slim(res)

        # 写完立刻在同一通道上回读一次，放进 trace 供 verify 用
        try:
            trace.after["transform"] = backend.get_actor_transform(name).as_dict()
        except Exception as exc:  # noqa: BLE001 - 回读失败由 verify 报"无证据"
            trace.note(f"回读失败：{type(exc).__name__}: {exc}")

        if method == "HYBRID" and params.get("visual", True):
            shots.update(self._capture(ctx, backend, trace, "after"))
            trace.after["shots"] = shots

        return {
            "ok": True,
            "actor": name,
            "before": before,
            "target": target,
            "after": trace.after.get("transform"),
            "transport": method,
            "shots": shots,
            "ground_plan": ground_plan,
        }

    def _perform_mouse(
        self, ctx: SkillContext, params: Mapping[str, Any], trace: SkillTrace, name: str
    ) -> dict[str, Any]:
        """GUI 移动：Outliner 里选中 → Details 面板改 Z。

        只能改 Z（面板里 Location 那一行的第三个框）；改 X/Y 需要另一个标定，
        所以直接拒绝而不是"随便点点看"。
        """
        if params.get("location") is not None:
            raise NotSupported("GUI 方式不支持绝对定位（Details 面板需要逐个字段填），请改用结构化通道")

        axis = _axis_index(params.get("axis") or "z")
        if axis != 2:
            raise NotSupported(f"GUI 方式只支持改 Z 轴（当前请求轴向下标 {axis}）")

        delta = float(params.get("delta") or 0.0)
        z0 = params.get("current_z")
        if z0 is None:
            raise NotSupported("GUI 方式需要调用方提供 current_z（结构化通道不可用时无法读回当前坐标）")

        steps: list[dict[str, Any]] = []
        steps.append(gui_helpers.outliner_search(ctx, name))
        # 点第一条搜索结果（Outliner 顶部往下一点）
        steps.append(gui_helpers.click_in_roi(ctx, "world_outliner", rel_x=0.2, rel_y=0.10, name="select_first_result"))
        steps.append(gui_helpers.set_details_location_z(ctx, float(z0) + delta))

        trace.note(f"GUI 改 Z: {float(z0):.3f} -> {float(z0) + delta:.3f}（无精确回读）")
        return {"ok": True, "actor": name, "target_z": float(z0) + delta, "gui_only": True, "steps": steps}

    # -- 验证 ----------------------------------------------------------------

    def verify(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> Any:
        from ..validation.verifiers import Verifier

        v = Verifier()
        tol = float(params.get("tolerance", 0.5))

        if method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "HYBRID"):
            name = str(params.get("actor") or params.get("name") or "")
            backend = ctx.structured_or_none(method) or ctx.structured_or_none("UNREAL_MCP") or ctx.structured_or_none("UE_PYTHON")
            if backend is None:
                v.add(CheckResult("verify.readback", passed=False, skipped=True,
                                  reason="没有可用的结构化后端做回读"))
            else:
                v.read("after", lambda: backend.get_actor_transform(name).as_dict(),
                       optional=True)
                before_loc = (trace.before.get("transform") or {}).get("location")
                # 精确落地时 perform 已算好绝对目标；相对位移才用 delta 推算
                planned_target = (trace.result or {}).get("target")
                if planned_target:
                    target = [float(v) for v in planned_target]
                else:
                    target = self._target_location(params, trace.before.get("transform") or {})

                def _checks(vals: dict[str, Any]) -> list[CheckResult]:
                    after = vals.get("after")
                    if not after:
                        return [CheckResult("verify.readback", passed=False, skipped=True,
                                            reason="变换回读失败，无法验证")]
                    out = list(check_transform_equals(
                        after, expected_location=target, pos_tol=tol, label=name
                    ))
                    if params.get("location") is None and not params.get("place_on_ground"):
                        axis = _axis_index(params.get("axis") or "z")
                        if before_loc:
                            out.append(check_axis_delta(
                                before_loc, after["location"], axis=axis,
                                delta=float(params.get("delta") or 0.0), tol=tol,
                                name="move.delta",
                            ))
                            others = [i for i in range(3) if i != axis]
                            out.append(check_unchanged(before_loc, after["location"], axes=others, tol=tol))
                    return out

                v.check(_checks)

        if method in ("HYBRID", "MOUSE"):
            shots = trace.after.get("shots") or {}
            if shots.get("before") and shots.get("after"):
                try:
                    d = diff(load_rgb(shots["before"]), load_rgb(shots["after"]),
                             downscale_to_width=320, changed_pixel_threshold=12)
                    # **这条只是参考，不是通过门槛。** 实测（960x540 视口、
                    # 把一个 80cm 构件移动 400cm）：
                    #     全分辨率变化占比 1.35e-5，缩到 320px 后 max_delta 只剩 3
                    #     （阈值 12 根本够不到），bbox=None。
                    # 缩放的均值把微弱信号抹平了，而且小且远的物体在整幅画面里
                    # 本来就只占万分之一。拿它当 pass/fail 会制造**假红**，
                    # 进而触发重试 —— 对相对位移就是重复执行（真实事故）。
                    # 权威证据是结构化回读（下面那几条 transform/delta 检查）。
                    v.add(check_visual_changed(
                        d, min_ratio=float(params.get("min_visual_change", 0.0005)),
                        name="visual.changed", advisory=True,
                    ))
                except Exception as exc:  # noqa: BLE001
                    v.add(CheckResult("visual.changed", passed=False, skipped=True,
                                      reason=f"图像比较失败：{type(exc).__name__}: {exc}"))
            elif method == "MOUSE":
                v.add(CheckResult("visual.changed", passed=False, skipped=True,
                                  reason="GUI 方式未采到可比较的前后帧"))

        return v.run()

    # -- 工具 ----------------------------------------------------------------

    @staticmethod
    def _target_location(params: Mapping[str, Any], before: Mapping[str, Any]) -> list[float]:
        if params.get("location") is not None:
            loc = [float(v) for v in params["location"]]
            if len(loc) != 3:
                raise NotSupported("location 必须是 3 个数字")
            return loc
        base = list(before.get("location") or (0.0, 0.0, 0.0))
        if len(base) < 3:
            raise NotSupported("读到的 transform 缺 location，无法做相对偏移")
        axis = _axis_index(params.get("axis") or "z")
        base[axis] = float(base[axis]) + float(params.get("delta") or 0.0)
        return base

    @staticmethod
    def _capture(ctx: SkillContext, backend: Any, trace: SkillTrace, tag: str) -> dict[str, str]:
        """抓一张 UE 内部视口截图；后端不支持就退回桌面截图；都不行就跳过。"""
        out: dict[str, str] = {}
        try:
            res = backend.viewport_screenshot(width=960, height=540)
            images = res.get("images") or []
            if images:
                path = ctx.save_screenshot(images[0]["data"], f"move_{tag}_viewport.png")
                out[tag] = str(path)
                return out
        except Exception as exc:  # noqa: BLE001 - 截图不是关键路径
            trace.note(f"UE 内部截图不可用（{type(exc).__name__}），尝试桌面截图")
        try:
            shot, path = gui_helpers.desktop_screenshot(ctx, f"move_{tag}_desktop.png")
            out[tag] = str(path)
        except Exception as exc:  # noqa: BLE001
            trace.note(f"桌面截图也不可用：{type(exc).__name__}: {exc}")
        return out


def _fmt(loc: Sequence[float]) -> str:
    return "(" + ", ".join(f"{float(v):.1f}" for v in loc) + ")"


def _slim(res: Mapping[str, Any], limit: int = 8) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, (k, v) in enumerate(res.items()):
        if i >= limit:
            break
        if k in ("images",):
            continue
        out[k] = v if not isinstance(v, (list, dict)) or len(str(v)) < 300 else str(v)[:300]
    return out


__all__ = ["ActorMoveSkill", "AXIS_INDEX"]
