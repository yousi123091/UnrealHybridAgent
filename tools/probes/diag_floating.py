#!/usr/bin/env python
"""只读诊断：搞清楚示例宫殿场景的"底边 Z"到底长什么样。

结论要回答一个问题：**这个场景存在统一的"地面"吗？**
如果不存在，那么"用某个分位数当地面"这条路本身就是错的，
必须换成"显式地面参照物"。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # tools/probes/ -> 项目根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402
from src.vision.judge import Observation, estimate_ground, usable_observations  # noqa: E402


def pct(vals: list[float], p: float) -> float:
    i = max(0, min(int(len(vals) * p / 100.0), len(vals) - 1))
    return vals[i]


def main() -> int:
    cfg = load_config()
    client = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-diag")
    client.initialize()
    client.tool_names(refresh=True)

    res = client.call_tool("actor", {"action": "get_all_details", "params": {}})
    data = res.get("data") if isinstance(res, dict) else res
    actors = (data or {}).get("actors") or []
    client.close()

    print(f"actor 总数 = {len(actors)}")

    # 字段名先看一眼（不同版本可能不一样）
    if actors:
        print("首个 actor 的字段:", sorted(actors[0].keys()))

    obs = [Observation.from_payload(a) for a in actors]
    usable = usable_observations(obs)
    print(f"可用（有包围盒、非灯光相机）= {len(usable)}")

    bottoms = sorted(o.bottom_z for o in usable if o.bottom_z is not None)
    if not bottoms:
        print("没有底边数据 —— 包围盒字段缺失")
        return 1

    print("\n--- 底边 Z 分布 ---")
    print(f"min={bottoms[0]:.1f}  max={bottoms[-1]:.1f}  跨度={bottoms[-1]-bottoms[0]:.1f}cm")
    for p in (1, 2, 5, 10, 25, 50, 75, 90, 95, 99):
        print(f"  p{p:>2} = {pct(bottoms, p):10.1f}")

    print("\n--- 用不同分位当「地面」，会判多少浮空 ---")
    for p in (1, 2, 5, 10, 25, 50):
        g = estimate_ground(usable, percentile=float(p))
        n = sum(1 for o in usable if o.bottom_z is not None and o.bottom_z - g > 5.0)
        print(f"  p{p:>2} 地面={g:9.1f} -> 浮空 {n}/{len(usable)} ({n/len(usable):.1%})")

    # 底边的"聚类"情况：真正的地面应该是一大坨
    print("\n--- 底边直方图（每 200cm 一档，前 12 档） ---")
    hist: Counter[int] = Counter()
    for b in bottoms:
        hist[int(b // 200) * 200] += 1
    for key in sorted(hist)[:12]:
        print(f"  [{key:>7}, {key+200:>7}) : {hist[key]:>5}  {'#' * min(60, hist[key] // 5)}")

    # 找出"最像地板"的 actor：包围盒很扁（Z 厚度小）且面积大
    print("\n--- 候选「地面参照物」（扁且大，底边最低的 10 个） ---")
    flat = []
    for o in usable:
        if o.bounds_extent is None or o.bottom_z is None:
            continue
        sx, sy, sz = o.bounds_extent
        thickness = sz * 2.0
        area = (sx * 2.0) * (sy * 2.0)
        if thickness <= 200.0 and area >= 100000.0:
            flat.append((o.bottom_z, o.label, o.class_name, thickness, area))
    flat.sort(key=lambda t: (t[0], -t[4]))
    for bottom, label, cls, th, area in flat[:10]:
        print(f"  bottom={bottom:9.1f} th={th:7.1f} area={area:12.0f}  {label}  [{cls.split('.')[-1]}]")
    print(f"  （共 {len(flat)} 个候选）")

    print("\n--- 类分布 top 12 ---")
    for cls, n in Counter(o.class_name.split(".")[-1] for o in usable).most_common(12):
        print(f"  {n:>5}  {cls}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
