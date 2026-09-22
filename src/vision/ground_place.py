"""精确落回地面：由射线命中面计算**绝对**落点。

为什么是绝对坐标而不是相对 delta：
  * 相对位移不可幂等（F3：重试会重复生效）；
  * 绝对 location 重复执行结果相同，天然安全。

支撑面取采样命中 Z 的**中位数**（滤掉恰好挡在下面的装饰件尖），
不取 max/min。调用方仍可用 ignore_labels / ground_ref 语义进一步约束。
"""

from __future__ import annotations

from statistics import median
from typing import Any, Mapping, Sequence


def _as_xyz(value: Sequence[float] | None, default: float = 0.0) -> list[float]:
    out = [default, default, default]
    if value:
        for i, v in enumerate(list(value)[:3]):
            out[i] = float(v)
    return out


def plan_ground_placement(
    *,
    location: Sequence[float],
    extent: Sequence[float] | None,
    hits: Sequence[Mapping[str, Any]],
    ignore_hit_labels: Sequence[str] | None = None,
    exclude_actor: str | None = None,
    min_hits: int = 1,
    ground_tolerance: float = 5.0,
) -> dict[str, Any]:
    """根据向下射线命中，算出「包围盒底边贴支撑面」的绝对 location。

    ``extent`` 约定与 UE Actor bounds 一致：**半高**（half-extent）。
    目标 Z = support_z + extent_z，使得 bottom = support_z。

    不可信时返回 ``actionable=False`` 且**不给出** location，
    调用方必须拒绝自动修改（与浮空检测同一套安全哲学）。
    """
    loc = _as_xyz(location)
    ext = _as_xyz(extent)
    exclude = {str(exclude_actor)} if exclude_actor else set()
    exclude.update({str(x) for x in (ignore_hit_labels or []) if x})

    zs: list[float] = []
    used: list[dict[str, Any]] = []
    for hit in hits or []:
        label = hit.get("hit_actor_label")
        if label is not None and str(label) in exclude:
            continue
        z = hit.get("impact_z")
        if z is None:
            impact = hit.get("impact_point") or hit.get("location")
            if isinstance(impact, (list, tuple)) and len(impact) >= 3:
                z = impact[2]
        if z is None:
            continue
        zs.append(float(z))
        used.append(dict(hit))

    if len(zs) < int(min_hits):
        return {
            "actionable": False,
            "reliable": False,
            "reason": f"有效射线命中不足（{len(zs)} < {min_hits}），拒绝给出绝对落点",
            "hit_count": len(zs),
            "hits": used,
        }

    support_z = float(median(zs))
    half_h = ext[2] if ext[2] else 0.0
    target_z = support_z + half_h
    target = [loc[0], loc[1], target_z]
    current_bottom = loc[2] - half_h
    gap = current_bottom - support_z
    delta_z = target_z - loc[2]

    return {
        "actionable": True,
        "reliable": True,
        "location": target,
        "support_z": support_z,
        "current_bottom": current_bottom,
        "gap": gap,
        "delta_z": delta_z,
        "extent": ext,
        "hit_count": len(zs),
        "hit_zs": zs,
        "hits": used,
        "ground_tolerance": float(ground_tolerance),
        "within_tolerance": abs(gap) <= float(ground_tolerance),
        "mode": "absolute_place",
        "note": "绝对落点（幂等）；支撑面=命中 Z 中位数，bottom 应落在 support_z",
    }


__all__ = ["plan_ground_placement"]
