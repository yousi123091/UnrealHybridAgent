#!/usr/bin/env python
"""只读诊断：把「全局地面」换成「局部地面」（邻域低分位），看误报会不会消失。

局部地面的定义：
    对每个 Actor A，取水平距离 R 以内的邻居 B（不含 A），
    ground(A) = B 底边的低分位数。
    gap(A) = A 底边 - ground(A)

为什么这样能work：屋顶的邻居还是屋顶（≈2000），广场构件的邻居还是广场（≈0），
"多层建筑"这个致命问题被"只看邻域"自然化解了。
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


def local_ground(
    usable: list[Observation], radius: float, percentile: float, min_neighbors: int
) -> dict[str, tuple[float | None, int]]:
    """返回 {label: (ground_z, neighbor_count)}。邻居不足 -> ground=None。"""
    pts = [(o, o.location[0], o.location[1], o.bottom_z) for o in usable if o.bottom_z is not None]

    # 简单网格索引（2000cm 一格），避免 O(n^2) 全扫
    cell = max(radius, 200.0)
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (_o, x, y, _b) in enumerate(pts):
        grid.setdefault((int(x // cell), int(y // cell)), []).append(i)

    out: dict[str, tuple[float | None, int]] = {}
    r2 = radius * radius
    for i, (o, x, y, b) in enumerate(pts):
        cx, cy = int(x // cell), int(y // cell)
        span = 1 if cell < radius else 2
        neigh: list[float] = []
        for gx in range(cx - span, cx + span + 1):
            for gy in range(cy - span, cy + span + 1):
                for j in grid.get((gx, gy), ()):  # noqa: B007
                    if j == i:
                        continue
                    _o2, x2, y2, b2 = pts[j]
                    if (x2 - x) ** 2 + (y2 - y) ** 2 <= r2:
                        neigh.append(b2)
        if len(neigh) < min_neighbors:
            out[o.label] = (None, len(neigh))
            continue
        neigh.sort()
        idx = max(0, min(int(len(neigh) * percentile / 100.0), len(neigh) - 1))
        out[o.label] = (neigh[idx], len(neigh))
    return out


def main() -> int:
    cfg = load_config()
    client = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-diag2")
    client.initialize()
    client.tool_names(refresh=True)
    res = client.call_tool("actor", {"action": "get_all_details", "params": {}})
    data = res.get("data") if isinstance(res, dict) else res
    actors = (data or {}).get("actors") or []
    client.close()

    usable = usable_observations([Observation.from_payload(a) for a in actors])
    print(f"可用 actor = {len(usable)}")

    for radius, percentile, min_n in ((500.0, 10.0, 3), (1000.0, 10.0, 3), (2000.0, 10.0, 3), (1000.0, 5.0, 5)):
        g = local_ground(usable, radius, percentile, min_n)
        flags = []
        for o in usable:
            if o.bottom_z is None:
                continue
            gz, _n = g.get(o.label, (None, 0))
            if gz is None:
                flags.append((float("inf"), o.label, o.bottom_z, None, o.class_name))
            elif o.bottom_z - gz > 5.0:
                flags.append((o.bottom_z - gz, o.label, o.bottom_z, gz, o.class_name))
        flags.sort(reverse=True)
        ratio = len(flags) / len(usable)
        print(
            f"\n=== R={radius:.0f}cm  p={percentile:.0f}  minN={min_n} "
            f"-> 疑似浮空 {len(flags)}/{len(usable)} ({ratio:.1%}) ==="
        )
        for gap, label, b, gz, cls in flags[:8]:
            gs = "无邻居" if gz is None else f"{gz:.0f}"
            print(f"   gap={gap:>9.1f}  bottom={b:>8.1f}  局部地面={gs:>8}  {label}")
        below = sum(1 for f in flags if f[3] is None)
        print(f"   （其中「邻域内没有支撑」= {below} 个）")
        print("   类分布:", dict(Counter(f[4].split('.')[-1] for f in flags).most_common(5)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
