""""视觉判断"——判断场景里有没有异常（本期的例子：有东西浮在空中）。

这里有一个刻意的取舍，值得写清楚：

    "这个东西是不是浮空"**不适合**交给一个视觉模型去回答。

视觉模型的答案是概率性的、不可复现的，而"浮空"这件事在 UE 里有**确定答案**：
    Actor 的世界坐标 Z 减去它的包围盒底边 = 离地高度
只要拿到 ``world_bounds_origin/extent``，判断就是纯算术，100% 可复现。

所以本模块的默认判断器是**几何判断**（读 UE 的结构化数据），
视觉只承担两件事：
    1. 给"任务是否完成"提供一张**可归档的证据图**；
    2. 用前后帧差异证明"确实变了"（纯像素运算，同样可复现）。

这一层设计直接决定了自动化验证能不能进 CI——含糊的判断做不到。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: 场景地面的判定容差（cm）。UE 默认单位是厘米，1cm 的浮空属于噪声。
DEFAULT_GROUND_TOLERANCE = 5.0

#: 用"所有底边的第 N 分位"估计地面高度。
#:
#: 为什么不是中位数：实测在示例宫殿场景（1184 个 Actor）上，**中位数 = 856cm**，
#: 因为高处的构件（屋顶、斗拱、梁、椽）在数量上占了多数。用中位数当"地面"，
#: 结果是 569 个构件集体被判为"浮空"——包括整栋建筑的屋顶。
#: 低分位数贴近真正的落地构件，不会被"高处构件占多数"带偏。
DEFAULT_GROUND_PERCENTILE = 5.0

#: 被判浮空的占比超过这个阈值，就认为这次判断**不可信**，拒绝据此自动修改场景。
#: 一栋建筑里"一小部分东西浮着"是异常；"一大半东西浮着"说明地面估计错了。
DEFAULT_MAX_FLAGGED_RATIO = 0.25


@dataclass
class Observation:
    """一个 Actor 的可判断观测值。"""

    label: str
    location: tuple[float, float, float]
    bounds_origin: tuple[float, float, float] | None = None
    bounds_extent: tuple[float, float, float] | None = None
    class_name: str = ""

    @property
    def z(self) -> float:
        return self.location[2]

    @property
    def bottom_z(self) -> float | None:
        """包围盒底边世界 Z —— 判断浮空要用它，而不是中心点。"""
        if self.bounds_origin is None or self.bounds_extent is None:
            return None
        return self.bounds_origin[2] - self.bounds_extent[2]

    @property
    def height(self) -> float | None:
        if self.bounds_extent is None:
            return None
        return self.bounds_extent[2] * 2.0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Observation":
        def vec3(*keys: str) -> tuple[float, float, float] | None:
            for k in keys:
                v = payload.get(k)
                if isinstance(v, (list, tuple)) and len(v) >= 3:
                    return (float(v[0]), float(v[1]), float(v[2]))
                if isinstance(v, Mapping) and {"x", "y", "z"} <= set(v):
                    return (float(v["x"]), float(v["y"]), float(v["z"]))
            return None

        loc = vec3("location", "world_location") or (0.0, 0.0, 0.0)
        return cls(
            label=str(payload.get("label") or payload.get("name") or ""),
            location=loc,
            bounds_origin=vec3("world_bounds_origin", "bounds_origin"),
            bounds_extent=vec3("world_bounds_extent", "bounds_extent"),
            class_name=str(payload.get("class") or payload.get("class_name") or ""),
        )


@dataclass
class FloatingCandidate:
    """一个疑似浮空的 Actor。"""

    observation: Observation
    ground_z: float
    gap: float                   # 离地净空高度（cm）
    basis: str                   # 用了哪种底边：bounds 优先，退化到中心点

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.observation.label,
            "class": self.observation.class_name,
            "location": list(self.observation.location),
            "ground_z": round(self.ground_z, 3),
            "gap": round(self.gap, 3),
            "basis": self.basis,
        }


def observations_from_actors(actors: Iterable[Mapping[str, Any]]) -> list[Observation]:
    return [Observation.from_payload(a) for a in actors if isinstance(a, Mapping)]


def usable_observations(
    observations: Sequence[Observation],
    *,
    ignore_labels: Iterable[str] = (),
    ignore_substrings: Iterable[str] = (
        "Light", "Camera", "Sky", "Atmosphere", "Fog", "PlayerStart", "Brush",
    ),
) -> list[Observation]:
    """过滤掉"本来就该悬空"的东西（灯、相机、天空球…）和没有包围盒的观测。"""
    ignore = {s.lower() for s in ignore_labels}
    subs = tuple(s.lower() for s in ignore_substrings)

    usable: list[Observation] = []
    for obs in observations:
        if not obs.label or obs.label.lower() in ignore:
            continue
        if any(s in obs.class_name.lower() for s in subs):
            continue
        if obs.bottom_z is None:
            continue
        usable.append(obs)
    return usable


def estimate_ground(
    observations: Sequence[Observation],
    *,
    percentile: float = DEFAULT_GROUND_PERCENTILE,
) -> float | None:
    """用底边的低分位数估计"地面高度"。

    见 :data:`DEFAULT_GROUND_PERCENTILE` 的说明：这里刻意**不用中位数**。
    """
    bottoms = sorted(o.bottom_z for o in observations if o.bottom_z is not None)
    if not bottoms:
        return None
    pct = max(0.0, min(float(percentile), 100.0))
    idx = int(len(bottoms) * pct / 100.0)
    idx = max(0, min(idx, len(bottoms) - 1))
    return bottoms[idx]


def judge_floating(
    observations: Sequence[Observation],
    *,
    ground_tolerance: float = DEFAULT_GROUND_TOLERANCE,
    ground_percentile: float = DEFAULT_GROUND_PERCENTILE,
    ignore_labels: Iterable[str] = (),
    ignore_substrings: Iterable[str] = (
        "Light", "Camera", "Sky", "Atmosphere", "Fog", "PlayerStart", "Brush",
    ),
) -> list[FloatingCandidate]:
    """挑出"底边明显高于地面"的 Actor，按离地高度降序。

    地面高度不是常数——取**所有底边的低分位数**（见 `DEFAULT_GROUND_PERCENTILE`），
    这样"整个场景被抬高"不会被误判成"有东西浮空"。
    没有 bounds 信息的 Actor（灯、相机等）会被跳过，而不是拿中心点去猜。
    """
    usable = usable_observations(
        observations, ignore_labels=ignore_labels, ignore_substrings=ignore_substrings
    )
    if not usable:
        return []

    ground_z = estimate_ground(usable, percentile=ground_percentile)
    if ground_z is None:
        return []

    out: list[FloatingCandidate] = []
    for obs in usable:
        bottom = obs.bottom_z
        assert bottom is not None
        gap = bottom - ground_z
        if gap > ground_tolerance:
            out.append(FloatingCandidate(obs, ground_z, gap, basis="bounds_bottom"))

    out.sort(key=lambda c: c.gap, reverse=True)
    return out


def judge_by_center(
    observations: Sequence[Observation],
    *,
    ground_tolerance: float = DEFAULT_GROUND_TOLERANCE,
) -> list[FloatingCandidate]:
    """退路：没有 bounds 数据时，用包围盒/中心点近似判断。

    明确标注 ``basis=center_fallback``，让日志里能看出"这次判断精度较低"。
    """
    if not observations:
        return []
    zs = sorted(o.z for o in observations)
    ground_z = zs[len(zs) // 2]
    out = [
        FloatingCandidate(o, ground_z, o.z - ground_z, basis="center_fallback")
        for o in observations
        if o.z - ground_z > ground_tolerance
    ]
    out.sort(key=lambda c: c.gap, reverse=True)
    return out


# --- 受控检测：显式地面参照 + 限定区域 ----------------------------------------
#
# 为什么要有这一段：上面那些"统计地面"的做法在**多层建筑**里全都不成立。
# 实测（示例宫殿场景，1179 个可用 Actor，真值 = 没有任何东西浮空）：
#
#     全局中位数当地面   -> 569 个误报（48.3%）
#     全局第 5 分位当地面 -> 1117 个误报（94.7%）
#     局部邻域低分位      -> 909 个误报（77.1%）
#     "正下方支撑面"       -> 582 个（49.4%）且 gap 是连续谱，没有可切的断点
#                            （台阶踏面之间几乎不重叠、重檐压着屋顶、装饰件
#                              —— 几何上全长得像悬空）
#
# 结论：**全场景无引导的浮空检测，仅凭包围盒几何是做不到的**。
# 与其给一个 50% 误报率、却"看起来能自动干活"的检测器（那是最危险的假绿），
# 不如把检测限定在**语义明确**的场景里：
#
#     * 地面从一个**显式参照物**取（例如某块广场/台基的顶面），不靠统计猜；
#     * 只在**指定的水平区域**内巡检（开放广场这类没有竖向堆叠的地方）；
#     * 结论不可信时由上层拒绝自动修改（见 summarise 的 reliable）。


@dataclass
class GroundReference:
    """一块作为"地面"的参照物（通常是广场、台基、地板）。"""

    label: str
    top_z: float
    bottom_z: float
    footprint: tuple[float, float, float, float]  # x_min, x_max, y_min, y_max

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "top_z": round(self.top_z, 3),
            "bottom_z": round(self.bottom_z, 3),
            "footprint": [round(v, 2) for v in self.footprint],
        }


def rects_overlap(
    a: Sequence[float], b: Sequence[float], *, min_fraction: float = 0.0
) -> bool:
    """``a`` 是否至少以 ``min_fraction`` 的比例覆盖 ``b``（水平矩形）。

    ``min_fraction=0`` 表示"只要有**正面积**的重叠就算"。
    完全不相交一律返回 False —— 曾经这里写成 ``return min_fraction <= 0.0``，
    结果两个八竿子打不着的矩形被判成"重叠"，覆盖率检查形同虚设。
    """
    ax0, ax1, ay0, ay1 = (float(v) for v in a[:4])
    bx0, bx1, by0, by1 = (float(v) for v in b[:4])
    ox = min(ax1, bx1) - max(ax0, bx0)
    oy = min(ay1, by1) - max(ay0, by0)
    if ox <= 0.0 or oy <= 0.0:
        return False
    area_b = max(1e-9, (bx1 - bx0) * (by1 - by0))
    return (ox * oy) / area_b >= min_fraction


def find_reference(
    observations: Sequence[Observation], label: str
) -> GroundReference | None:
    """按 label 找一块可用作地面的参照物。找不到/没有包围盒 -> None（不猜）。"""
    target = (label or "").strip().lower()
    if not target:
        return None
    for obs in observations:
        if obs.label.lower() != target:
            continue
        if obs.bounds_origin is None or obs.bounds_extent is None:
            return None
        ox, oy, oz = obs.bounds_origin
        ex, ey, ez = obs.bounds_extent
        return GroundReference(
            label=obs.label,
            top_z=oz + ez,
            bottom_z=oz - ez,
            footprint=(ox - ex, ox + ex, oy - ey, oy + ey),
        )
    return None


def within_region(obs: Observation, region: Sequence[float]) -> bool:
    """``region = (x_min, x_max, y_min, y_max)``，按**中心点**落在区域内判断。"""
    x, y = obs.location[0], obs.location[1]
    x0, x1, y0, y1 = (float(v) for v in region[:4])
    return x0 <= x <= x1 and y0 <= y <= y1


def region_from_actor(obs: Observation, pad: float = 0.0) -> tuple[float, float, float, float] | None:
    """用某个 Actor 的包围盒当巡检区域，可外扩 ``pad``。"""
    if obs.bounds_origin is None or obs.bounds_extent is None:
        return None
    ox, oy, _oz = obs.bounds_origin
    ex, ey, _ez = obs.bounds_extent
    return (ox - ex - pad, ox + ex + pad, oy - ey - pad, oy + ey + pad)


def judge_against_ground(
    observations: Sequence[Observation],
    *,
    ground_z: float,
    ground_tolerance: float = DEFAULT_GROUND_TOLERANCE,
    region: Sequence[float] | None = None,
    exclude_labels: Iterable[str] = (),
    ignore_substrings: Iterable[str] = (
        "Light", "Camera", "Sky", "Atmosphere", "Fog", "PlayerStart", "Brush",
    ),
) -> list[FloatingCandidate]:
    """受控判定：**给定地面高度**（不是猜的），在（可选的）限定区域内找悬空物。

    ``ground_z`` 必须由调用方提供（一般来自 :func:`find_reference` 的顶面），
    这里不做任何统计推断——因为统计推断在多层场景里已经被证伪。
    """
    ignored = {s.lower() for s in exclude_labels}
    subs = tuple(s.lower() for s in ignore_substrings)

    out: list[FloatingCandidate] = []
    for obs in observations:
        if not obs.label or obs.label.lower() in ignored:
            continue
        if any(s in obs.class_name.lower() for s in subs):
            continue
        if region is not None and not within_region(obs, region):
            continue
        bottom = obs.bottom_z
        if bottom is None:
            continue
        gap = bottom - ground_z
        if gap > ground_tolerance:
            out.append(FloatingCandidate(obs, ground_z, gap, basis="explicit_ground"))

    out.sort(key=lambda c: c.gap, reverse=True)
    return out


def summarise(
    candidates: Sequence[FloatingCandidate],
    *,
    scanned: int | None = None,
    usable: int | None = None,
    basis: str | None = None,
    ground_percentile: float = DEFAULT_GROUND_PERCENTILE,
    max_flagged_ratio: float = DEFAULT_MAX_FLAGGED_RATIO,
    ground_reference: Mapping[str, Any] | None = None,
    unguided: bool = False,
    enforce_ratio: bool = True,
    coverage_ok: bool = True,
) -> dict[str, Any]:
    """汇总成可下结论的字典，并给出**可信度**与**可执行性**。

    三个门槛，各管一件事，缺一个都会出问题：

    ``usable is not None``
        分母已知。不传分母就退回 ``len(candidates)``，占比恒为 100%，
        任何非空结论都会被判死 —— 这是刻意的 **fail-closed**：
        算不准可信度时，宁可拒绝自动改场景。

    ``enforce_ratio``
        **占比门槛只在"地面是统计推断"时才有意义。**
        它原本是用来发现"地面估错了"的：一大半东西被判浮空，多半是
        地面算偏了。但如果地面是**显式参照物测出来的**，就不存在"估错"，
        占比高只说明"这块区域里确实有这么多东西浮着"。
        所以受控模式下要关掉这个门槛，否则会出现荒谬结果：
        小区域里只有 3 个构件、其中 1 个真悬空 → 33% → 被自己的安全闸拦死。
        （这个 bug 是实测跑 Demo2 时撞出来的。）

    ``coverage_ok``
        受控模式该管的事：**参照物的水平范围必须真的覆盖被巡检的区域**。
        否则等于拿 A 处的地面去量 B 处的东西 —— 这才是受控模式真正会犯的错。

    ``actionable`` = 可信 + 不是统计地面。只有它为真，上层才允许自动改场景。
    """
    known_denominator = usable is not None
    total = usable if known_denominator else len(candidates)
    ratio = (len(candidates) / total) if total else 0.0
    ratio_ok = ratio <= max_flagged_ratio if enforce_ratio else True
    reliable = bool(known_denominator and ratio_ok and coverage_ok)
    actionable = reliable and not unguided

    out: dict[str, Any] = {
        "floating_count": len(candidates),
        "ground_z": round(candidates[0].ground_z, 3) if candidates else None,
        "candidates": [c.as_dict() for c in candidates],
        "ground_percentile": ground_percentile,
        "flagged_ratio": round(ratio, 4),
        "usable_actors": total,
        "ratio_enforced": enforce_ratio,
        "coverage_ok": coverage_ok,
        "reliable": reliable,
        "actionable": actionable,
        "unguided": bool(unguided),
    }
    if basis:
        out["basis"] = basis
    if scanned is not None:
        out["scanned"] = scanned
    if ground_reference is not None:
        out["ground_reference"] = dict(ground_reference)
    if not known_denominator:
        out["reliability_basis"] = "unknown_denominator"

    warnings: list[str] = []
    if not known_denominator:
        warnings.append(
            "调用方未提供参与判断的 Actor 总数，无法评估判决可信度——"
            "按 fail-closed 处理，**不会**据此自动修改场景。"
        )
    if enforce_ratio and not ratio_ok:
        warnings.append(
            f"被判浮空的 Actor 占比 {ratio:.1%} 超过阈值 {max_flagged_ratio:.0%}——"
            "这通常说明**地面高度估错了**，而不是真有这么多东西浮着。"
            "结论仅作参考，**不会**据此自动修改场景。"
        )
    elif not enforce_ratio and ratio > max_flagged_ratio:
        # 受控模式下占比高不否决结论，但值得提醒核查参照物
        warnings.append(
            f"提示：被判浮空的占比 {ratio:.1%} 偏高。受控模式（显式地面）下这不否决结论，"
            "但建议核对地面参照物是否选得合适。"
        )
    if not coverage_ok:
        warnings.append(
            "巡检区域超出了地面参照物的水平范围——这条路等于拿别处的地面来量，"
            "**不会**据此自动修改场景。"
        )
    if unguided:
        warnings.append(
            "地面是统计推断的（未指定地面参照物）。多层建筑里统计地面不可靠："
            "实测全局中位数误报 48%、低分位误报 95%。建议传 ground_ref 指定一块地板/广场。"
        )
    if warnings:
        out["warning"] = " ".join(warnings)
    return out


__all__ = [
    "DEFAULT_GROUND_TOLERANCE",
    "DEFAULT_GROUND_PERCENTILE",
    "DEFAULT_MAX_FLAGGED_RATIO",
    "Observation",
    "FloatingCandidate",
    "GroundReference",
    "observations_from_actors",
    "usable_observations",
    "estimate_ground",
    "judge_floating",
    "judge_by_center",
    "find_reference",
    "within_region",
    "region_from_actor",
    "rects_overlap",
    "judge_against_ground",
    "summarise",
]
