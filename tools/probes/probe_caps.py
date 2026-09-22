"""探针 3：确认截图/保存/脏包/投影 这几个关键动作的真实返回形态。

重点：
  * vision.capture_viewport —— base64 到底是 content:image 还是 text 里的 image_data？
  * level.save_current_level  —— 返回形态
  * util.world_to_screen      —— 世界坐标 -> 视口像素（给几何 grounding 用）
  * DIRTY_STATE_CODE          —— 我们的脏包脚本能不能跑通（依赖 MCPythonHelper）
  * actor.get_actor_bounds    —— 包围盒形态
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.adapters.unreal.unreal_mcp import DIRTY_STATE_CODE  # noqa: E402
from src.core.config import load_config  # noqa: E402


def show(title, obj, limit=1500):
    print("=" * 72)
    print(title)
    print("-" * 72)
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:limit])


def content_kinds(res):
    """只看 content 块的类型与字段名，避免把 base64 打出来。"""
    raw = res.get("raw") or {}
    out = []
    for item in raw.get("content") or []:
        if isinstance(item, dict):
            keys = {k: (f"<len {len(str(v))}>" if k in ("data", "text") and len(str(v)) > 200 else v) for k, v in item.items()}
            out.append(keys)
    return out


def main() -> int:
    cfg = load_config()
    client = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-probe3")
    client.initialize()
    client.tool_names(refresh=True)

    def call(domain, action, params=None):
        return client.call_tool(domain, {"action": action, "params": params or {}})

    # ---- capture_viewport：只看形状 ----
    res = call("vision", "capture_viewport", {"width": 320, "height": 180, "fov": 90})
    print("=" * 72)
    print("vision.capture_viewport -> content kinds")
    print(json.dumps(content_kinds(res), ensure_ascii=False, indent=2)[:1200])
    data = res.get("data")
    if isinstance(data, dict):
        print("data keys:", sorted(data.keys()))
        for k, v in data.items():
            if isinstance(v, str) and len(v) > 200:
                print(f"  {k}: <str len {len(v)}> head={v[:40]!r}")
            else:
                print(f"  {k}: {v!r}"[:300])
    print("images count:", len(res.get("images") or []))

    # ---- capture_actors ----
    res = call("vision", "capture_actors", {"actor_labels": ["Hall_Floor"], "width": 320, "height": 180})
    print("=" * 72)
    print("vision.capture_actors -> content kinds")
    print(json.dumps(content_kinds(res), ensure_ascii=False, indent=2)[:1200])
    d = res.get("data")
    if isinstance(d, dict):
        print("data keys:", sorted(d.keys()))

    # ---- world_to_screen ----
    show("util.world_to_screen([7800,0,840])", call("util", "world_to_screen", {"location": [7800.0, 0.0, 840.0]}))
    show("util.get_viewport_camera", call("util", "get_viewport_camera"))

    # ---- get_actor_bounds ----
    show("actor.get_actor_bounds(Hall_Floor)", call("actor", "get_actor_bounds", {"actor_label": "Hall_Floor"}))

    # ---- DIRTY_STATE_CODE ----
    res = call("util", "execute_python", {"code": DIRTY_STATE_CODE})
    show("DIRTY_STATE_CODE result", res.get("data"), 2500)

    # ---- save_current_level（当前无修改，看返回什么）----
    show("level.save_current_level", call("level", "save_current_level"))

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
