"""Planner：把"想做什么"编成计划。

第一版只做两类输入：

1. **显式目标**（推荐）—— 例如 `demo1` / `demo2` 这种已经写死的场景，
   以及 CLI 里逐参数给全的调用。这是主力路径，确定性最高。
2. **自然语言**——关键词切分 + 阈值判断，产出初稿。
   它的定位是"省你打字"，不是"替你想事"：猜错了会**如实报告**
   （`Plan.meta.warnings`），不会假装懂了。

刻意不做的事：不让模型自由生成计划然后直接执行。
这个项目会去改用户的 UE 工程，计划必须先能被人读懂再执行；
等确定性路径跑稳了，再考虑把模型接在"生成初稿"这一环。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..skills.registry import match_by_text
from .task import Plan, PlanStep

#: 数值 + 单位（UE 用厘米，所以默认按 cm 解释）
_NUMBER = r"([-+]?\d+(?:\.\d+)?)"
_MOVE_PATTERN = re.compile(
    r"(?:提高|抬高|升高|上升|增加|降低|下降|减少|下移|上移|移动|挪)\s*"
    r"(?:到)?\s*"
    r"(?:(X|Y|Z|X轴|Y轴|Z轴)\s*(?:轴)?\s*)?"
    r"(?:方向上?)?\s*" + _NUMBER + r"\s*(厘米|cm|米|m)?",
    re.IGNORECASE,
)
_ACTOR_QUOTED = re.compile(r"[「『\"'《【]([^」』\"'》】]{1,60})[」』\"'》】]")
_ACTOR_AFTER_KEYWORD = re.compile(r"(?:Actor|actor|物体|对象|模型|叫|名为|名字是)\s*[:：]?\s*([A-Za-z0-9_\-\.\u4e00-\u9fa5]{2,60})")


def parse_axis(text: str) -> str:
    m = re.search(r"\b([XYZ])\b|([XYZ])\s*轴", text, re.IGNORECASE)
    if m:
        return (m.group(1) or m.group(2)).lower()
    return "z"


def parse_delta(text: str) -> tuple[float, str]:
    """返回 (delta, 单位说明)。默认按厘米理解——UE 的世界单位就是厘米。"""
    m = _MOVE_PATTERN.search(text)
    if not m:
        return 0.0, "cm"
    value = float(m.group(2))
    unit = (m.group(3) or "cm").lower()
    if unit in ("m", "米"):
        value *= 100.0
    if any(k in text for k in ("降低", "下降", "减少", "下移")):
        value = -abs(value)
    return value, "cm"


def parse_actor(text: str) -> str | None:
    m = _ACTOR_QUOTED.search(text)
    if m:
        return m.group(1).strip()
    m = _ACTOR_AFTER_KEYWORD.search(text)
    if m:
        return m.group(1).strip()
    return None


class Planner:
    """把输入变成 :class:`Plan`。"""

    # -- 自然语言 -------------------------------------------------------------

    def plan_from_text(self, text: str, *, params: Mapping[str, Any] | None = None) -> Plan:
        params = dict(params or {})
        warnings: list[str] = []
        matched = match_by_text(text)

        if not matched:
            warnings.append(
                "没识别出任何 skill，退化为一次只读巡检（visual_inspect）。"
                "如果要执行具体动作，请用 --skill 显式指定。"
            )
            return Plan(
                name="fallback_inspect",
                description=text,
                steps=[PlanStep("visual_inspect", {"check_floating": True}, "未识别意图，先看一眼场景")],
                meta={"warnings": warnings, "source": "text"},
            )

        warnings.append(
            f"关键词推断结果（按命中数排序）：{', '.join(s.name for s in matched)}。"
            "自然语言解析只是初稿，请核对 plan 后再执行。"
        )

        steps: list[PlanStep] = []
        primary = matched[0].name

        # 有"移动"意图 -> 先定位，再移动，最后（可选）保存
        wants_move = any(s.name == "actor_move" for s in matched)
        wants_save = any(s.name == "level_save" for s in matched)
        wants_vision = any(s.name == "visual_inspect" for s in matched)

        actor = params.get("actor") or parse_actor(text)
        axis = params.get("axis") or parse_axis(text)
        delta, unit = parse_delta(text)

        if wants_vision:
            steps.append(PlanStep("visual_inspect", {"check_floating": True}, "先看画面，拿到异常清单"))

        if wants_move or (actor and (delta or params.get("location"))):
            if not actor:
                raise ValueError("识别到移动意图但找不到 Actor 名字；请用引号包住名字或用 --actor 指定")
            if not delta and not params.get("location"):
                raise ValueError("识别到移动意图但找不到位移数值；请用 --delta 指定")
            steps.append(PlanStep("actor_find", {"actor": actor}, f"定位 {actor}"))
            steps.append(PlanStep(
                "actor_move",
                {"actor": actor, "axis": axis, "delta": delta, "visual": wants_vision},
                f"把 {actor} 的 {axis.upper()} 轴移动 {delta:+.1f}cm",
                after=("actor_find",),
            ))
            if wants_save:
                steps.append(PlanStep("level_save", {}, "保存 Level", after=("actor_move",)))
        elif primary in ("actor_find", "actor_inspect") and actor:
            steps.append(PlanStep(primary, {"actor": actor}, f"{primary}: {actor}"))
        elif wants_save:
            steps.append(PlanStep("level_save", {}, "保存 Level"))
        elif not steps:
            steps.append(PlanStep(primary, {}, f"按关键词选择的首选 skill：{primary}"))

        return Plan(
            name="text_plan",
            description=text,
            steps=steps,
            meta={"warnings": warnings, "source": "text", "unit": unit},
        )

    # -- 显式场景 -------------------------------------------------------------

    @staticmethod
    def plan_raise_and_save(actor: str, delta: float = 20.0, *, axis: str = "z", save: bool = True) -> Plan:
        """MVP Demo 1：找到已知 Actor → 抬高 → 复验 → 保存 → 复验。

        这是需求里点名要跑通的任务，所以它的每一步都刻意选在"可验证"上：
        移动用结构化通道（能读回坐标），保存用 Ctrl+S（UE 既定交互）。
        """
        steps = [
            PlanStep("actor_find", {"actor": actor}, f"找到 Actor「{actor}」并读取当前变换"),
            PlanStep(
                "actor_move",
                {"actor": actor, "axis": axis, "delta": delta},
                f"把 {axis.upper()} 轴坐标提高 {delta:g}",
                after=("actor_find",),
            ),
        ]
        if save:
            steps.append(PlanStep("level_save", {}, "保存当前 Level（Ctrl+S）", after=("actor_move",)))
        return Plan(
            name="demo1_raise_and_save",
            description=f"把「{actor}」的 {axis.upper()} 提高 {delta:g}，然后保存 Level",
            steps=steps,
            meta={"actor": actor, "delta": delta, "axis": axis, "demo": "demo1"},
        )

    @staticmethod
    def plan_floating_repair(
        *,
        ground_tolerance: float = 5.0,
        target_axis: str = "z",
        save: bool = False,
        drop_by: float = 50.0,
        ground_ref: str | None = None,
        region: list[float] | None = None,
        region_pad: float | None = None,
        precise: bool = False,
    ) -> Plan:
        """MVP Demo 2：截图 → 判断谁浮空 →（视觉+结构化）修正 → 再截图 → 复验。

        **必须给 ground_ref**（一块地板/广场的 label）。原因见
        ``vision/judge.py`` 的实测记录：多层建筑里"统计地面"的误报率
        是 48%~95%，拿它去自动改场景等于拿骰子改工程。

        ``precise=True``：搬运步改为 ``place_on_ground``（向下射线求支撑面，
        写**绝对**坐标，幂等）。否则仍是调用方给的近似 ``drop_by``。
        """
        inspect_params: dict = {"check_floating": True, "ground_tolerance": ground_tolerance}
        if ground_ref:
            inspect_params["ground_ref"] = ground_ref
        if region is not None:
            inspect_params["region"] = list(region)
        elif region_pad is not None:
            inspect_params["region_pad"] = float(region_pad)

        recheck_params = dict(inspect_params)
        recheck_params["expect_no_floating"] = True

        if precise:
            move_params: dict = {
                "actor": "@floating_top",
                "place_on_ground": True,
                "visual": True,
                "tolerance": ground_tolerance,
            }
            move_desc = (
                "把最明显的浮空 Actor **精确落回**支撑面"
                "（向下 line_trace 取中位命中 Z → 绝对 location，幂等）"
            )
        else:
            move_params = {
                "actor": "@floating_top",
                "axis": target_axis,
                "delta": -abs(float(drop_by)),
                "visual": True,
            }
            move_desc = (
                f"把最明显的浮空 Actor 沿 {target_axis.upper()} 轴下移 {abs(float(drop_by)):g}"
                "（HYBRID：动手前后各留一张视口截图 + 走结构化通道精确改值）"
                "——近似修正，不是精确落地"
            )

        steps = [
            PlanStep(
                "visual_inspect",
                inspect_params,
                "截图巡检，判断场景里有没有 Actor 浮空"
                + (f"（地面参照物：{ground_ref}）" if ground_ref else "（未指定地面参照物）"),
            ),
            PlanStep(
                "actor_move",
                move_params,
                move_desc,
                after=("visual_inspect",),
                # 这一步的**目的就是演练 HYBRID**（视觉留证 + 结构化改值）。
                prefer_method="HYBRID",
            ),
            PlanStep(
                "visual_inspect",
                recheck_params,
                "再截图复验，确认异常已消除",
                after=("actor_move",),
            ),
        ]
        if save:
            steps.append(PlanStep("level_save", {}, "保存 Level", after=("visual_inspect",)))
        return Plan(
            name="demo2_floating_repair_precise" if precise else "demo2_floating_repair",
            description=(
                "视觉发现浮空 -> 精确落回地面 -> 视觉复验"
                if precise
                else "视觉发现浮空 -> 混合通道修正 -> 视觉复验"
            ),
            steps=steps,
            meta={
                "demo": "demo2",
                "precise": precise,
                "ground_tolerance": ground_tolerance,
                "ground_ref": ground_ref,
                "region": region,
                "region_pad": region_pad,
            },
        )


def plan_from_text(text: str, *, params: Mapping[str, Any] | None = None) -> Plan:
    return Planner().plan_from_text(text, params=params)


__all__ = [
    "Planner",
    "plan_from_text",
    "parse_axis",
    "parse_delta",
    "parse_actor",
]
