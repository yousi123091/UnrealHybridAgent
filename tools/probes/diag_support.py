#!/usr/bin/env python
"""只读诊断 3：「支撑面」定义 —— 物理上正确的那个。

    support(A) = max{ B.top : B≠A, XY 与 A 相交, B.top <= A.bottom + eps }
    gap(A)     = A.bottom - support(A)

没有 B 支撑 -> gap = inf（真的悬空）。
这比"邻域低分位"对，因为建筑里同一个水平位置本来就叠着多个楼层，
取低分位等于永远拿最下面那层当地面 —— 必然误报。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # tools/probes/ -> 项目根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402
from src.vision.judge import Observation, usable_observations  # noqa: E402

EPS = 1.0  # 允许 1cm 的建模误差


def boxes(usable: list[Observation]):
    out = []
    for o in usable:
        if o.bottom_z is None or o.bounds_origin is None or o.bounds_extent is None:
            continue
        ox, oy, oz = o.bounds_origin
        ex, ey, ez = o.bounds_extent
        out.append({
            "o": o, "label": o.label,
            "x0": ox - ex, "x1": ox + ex, "y0": oy - ey, "y1": oy + ey,
            "top": oz + ez, "bottom": oz - ez,
        })
    return out


def support_analysis(bs: list[dict], *, require_overlap: bool, radius: float, min_overlap: float):
    cell = 1000.0
    grid: dict[tuple[int, int], list[int]] = {}
    for i, b in enumerate(bs):
        cx0, cx1 = int(b["x0"] // cell), int(b["x1"] // cell)
        cy0, cy1 = int(b["y0"] // cell), int(b["y1"] // cell)
        for gx in range(cx0, cx1 + 1):
            for gy in range(cy0, cy1 + 1):
                grid.setdefault((gx, gy), []).append(i)

    results = []
    for i, a in enumerate(bs):
        cand: set[int] = set()
        cx0, cx1 = int(a["x0"] // cell), int(a["x1"] // cell)
        cy0, cy1 = int(a["y0"] // cell), int(a["y1"] // cell)
        for gx in range(cx0, cx1 + 1):
            for gy in range(cy0, cy1 + 1):
                cand.update(grid.get((gx, gy), ()))

        best_top = None
        best_label = None
        best_area = 0.0
        for j in cand:
            if j == i:
                continue
            b = bs[j]
            if b["top"] > a["bottom"] + EPS:
                continue
            # XY 重叠面积
            ox = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
            oy = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
            if ox <= 0.0 or oy <= 0.0:
                continue
            area = ox * oy
            ax = max(1.0, (a["x1"] - a["x0"]) * (a["y1"] - a["y0"]))
            if area / ax < min_overlap:
                continue
            if best_top is None or b["top"] > best_top:
                best_top, best_label, best_area = b["top"], b["label"], area
        gap = float("inf") if best_top is None else a["bottom"] - best_top
        results.append((gap, a["label"], a["bottom"], best_top, best_label, a["o"].class_name))
    return results


def main() -> int:
    cfg = load_config()
    client = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-diag3")
    client.initialize()
    client.tool_names(refresh=True)
    res = client.call_tool("actor", {"action": "get_all_details", "params": {}})
    data = res.get("data") if isinstance(res, dict) else res
    actors = (data or {}).get("actors") or []
    client.close()

    usable = usable_observations([Observation.from_payload(a) for a in actors])
    bs = boxes(usable)
    print(f"可用 actor = {len(usable)}；带包围盒 = {len(bs)}")

    for min_overlap in (0.0, 0.10, 0.25, 0.50):
        res_ = support_analysis(bs, require_overlap=True, radius=0.0, min_overlap=min_overlap)
        flags = [r for r in res_ if r[0] > 5.0]
        unsup = [r for r in res_ if r[0] == float("inf")]
        print(
            f"\n=== 最小 XY 重叠 = {min_overlap:.0%} -> 疑似浮空 {len(flags)}/{len(bs)} "
            f"({len(flags)/len(bs):.1%})，其中完全无支撑 {len(unsup)} 个 ==="
        )
        flags.sort(key=lambda r: (-1e18 if r[0] == float("inf") else -r[0]))
        for gap, label, bottom, top, sup, cls in flags[:10]:
            gs = "无" if top is None else f"{top:.0f}"
            ss = str(sup)[:28]
            gp = "inf" if gap == float("inf") else f"{gap:.1f}"
            print(f"   gap={gp:>9}  bottom={bottom:>8.1f}  支撑面={gs:>8}  {label:<38} <- {ss}")

    # --- 关键：gap 的分布。正常建筑堆叠的间隙 vs 真正悬空的间隙，应该能分开 ---
    print("\n\n########## gap 分布（min_overlap=25%） ##########")
    res_ = support_analysis(bs, require_overlap=True, radius=0.0, min_overlap=0.25)
    supported = [r for r in res_ if r[0] != float("inf")]
    unsupported = [r for r in res_ if r[0] == float("inf")]
    print(f"有支撑 {len(supported)} 个 / 无支撑 {len(unsupported)} 个（后者就是地形层本身）")

    buckets = [(0, 1), (1, 5), (5, 10), (10, 25), (25, 50), (50, 100),
               (100, 200), (200, 400), (400, 1e9)]
    print("\n  gap 区间            数量   占比     条形")
    for lo, hi in buckets:
        n = sum(1 for g, *_ in supported if lo <= g < hi)
        bar = "#" * min(50, n // 4)
        hi_s = "∞" if hi > 1e8 else f"{hi:.0f}"
        print(f"  [{lo:>5.0f}, {hi_s:>5})  {n:>6}  {n/len(supported):>6.1%}  {bar}")

    print("\n  --- 有支撑、但间隙 >= 100cm 的（这些才是真正的「悬空候选」） ---")
    cand = sorted([r for r in supported if r[0] >= 100.0], key=lambda r: -r[0])
    print(f"  共 {len(cand)} 个")
    for gap, label, bottom, top, sup, cls in cand[:20]:
        print(f"   gap={gap:>8.1f}  bottom={bottom:>8.1f}  下方支撑面={top:>8.0f}  {label:<40} <- {str(sup)[:26]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
