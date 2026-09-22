#!/usr/bin/env python
"""只读：为 Demo2 挑一个"受控试件"。

要的是：一块明确的广场地面 + 一个落在广场上、占地不大的构件。
先抬起来制造出**已知的**悬空，再让受控检测器去找它。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # tools/probes/ -> 项目根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402
from src.vision.judge import Observation  # noqa: E402


def main() -> int:
    cfg = load_config()
    client = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-fixture")
    client.initialize()
    client.tool_names(refresh=True)
    res = client.call_tool("actor", {"action": "get_all_details", "params": {}})
    data = res.get("data") if isinstance(res, dict) else res
    actors = (data or {}).get("actors") or []
    client.close()

    obs = [Observation.from_payload(a) for a in actors]

    print("=== 地面参照物候选（面积最大、最扁的） ===")
    refs = []
    for o in obs:
        if o.bounds_origin is None or o.bounds_extent is None:
            continue
        ox, oy, oz = o.bounds_origin
        ex, ey, ez = o.bounds_extent
        refs.append((4 * ex * ey, o.label, oz + ez, oz - ez, 2 * ez, 2 * ex, 2 * ey, ox, oy))
    refs.sort(reverse=True)
    for area, label, top, bottom, th, sx, sy, ox, oy in refs[:8]:
        print(f"  area={area:13.0f} top={top:8.1f} bottom={bottom:8.1f} th={th:6.1f} "
              f"size={sx:.0f}x{sy:.0f} centre=({ox:.0f},{oy:.0f})  {label}")

    # 以 EXT_Plaza_Central 为地面，找落在它上面、占地小的构件
    target = next((r for r in refs if r[1] == "EXT_Plaza_Central"), None)
    if target is None:
        print("\n找不到 EXT_Plaza_Central")
        return 1
    _a, _l, ptop, _pb, _th, psx, psy, pox, poy = target
    print(f"\n=== 以 EXT_Plaza_Central 为地面：top={ptop:.1f}，"
          f"范围约 x[{pox-psx/2:.0f},{pox+psx/2:.0f}] y[{poy-psy/2:.0f},{poy+psy/2:.0f}] ===")

    on_plaza = []
    for o in obs:
        if o.bounds_origin is None or o.bounds_extent is None or o.bottom_z is None:
            continue
        ox, oy, _oz = o.bounds_origin
        ex, ey, _ez = o.bounds_extent
        # 必须落在广场范围内（留边）
        if not (pox - psx / 2 + 200 <= ox <= pox + psx / 2 - 200):
            continue
        if not (poy - psy / 2 + 200 <= oy <= poy + psy / 2 - 200):
            continue
        gap = o.bottom_z - ptop
        size = max(2 * ex, 2 * ey)
        if -5.0 <= gap <= 5.0 and size <= 800.0:
            on_plaza.append((size, o.label, o.bottom_z, gap, ox, oy, o.class_name))

    on_plaza.sort()
    print(f"  贴地且占地 <=800cm 的构件共 {len(on_plaza)} 个，最小的 20 个：")
    for size, label, bottom, gap, ox, oy, cls in on_plaza[:20]:
        print(f"   size={size:7.1f} bottom={bottom:8.1f} gap={gap:6.2f} "
              f"xy=({ox:8.1f},{oy:8.1f})  {label}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
