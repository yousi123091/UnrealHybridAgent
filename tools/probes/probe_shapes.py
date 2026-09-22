"""探针 2：摸清其余关键动作的返回形态 + 做一次零净变化的写操作验证。

覆盖：
  actor.get_all_details   -> 列表形态
  level.get_current_level_path
  level.list_actions      -> save_current_level 签名
  util.list_actions       -> execute_python / world_to_screen / save_all_dirty 签名
  vision.list_actions     -> capture_viewport 签名
  util.execute_python     -> 脚本返回形态（不落脏）
  actor.set_location      -> 写操作返回形态（把 Hall_Floor 写回它自己的坐标，零净变化）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402


def head(title, obj, limit=2600):
    print("=" * 72)
    print(title)
    print("-" * 72)
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:limit])


def summary_actions(res):
    data = (res.get("data") or {}) if isinstance(res, dict) else {}
    return (data.get("actions") or {}) if isinstance(data, dict) else {}


def main() -> int:
    cfg = load_config()
    section = cfg.section("mcp_servers.unreal_mcp")
    client = make_client_from_config(section, name="uha-probe2")
    client.initialize()
    client.tool_names(refresh=True)

    def call(domain, action, params=None):
        return client.call_tool(domain, {"action": action, "params": params or {}})

    # ---- actor.get_all_details ----
    res = call("actor", "get_all_details")
    data = res.get("data") or {}
    print("=" * 72)
    print("actor.get_all_details  -> top-level keys:", list(data.keys()) if isinstance(data, dict) else type(data))
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                print(f"  list key {k!r}: len={len(v)}")
                if v:
                    head(f"  first row of {k}", v[0], 1200)
                    print("  row keys:", sorted(v[0].keys()) if isinstance(v[0], dict) else type(v[0]))

    # ---- level ----
    head("level.get_current_level_path", call("level", "get_current_level_path"))
    la = summary_actions(call("level", "list_actions"))
    for a in ("save_current_level", "save_all_dirty", "get_current_level_path"):
        if a in la:
            head(f"level.{a} signature", la[a], 400)

    # ---- util ----
    ua = summary_actions(call("util", "list_actions"))
    print("=" * 72)
    print("util actions:", sorted(ua.keys()))
    for a in ("execute_python", "world_to_screen", "screen_to_world", "save_all_dirty",
              "get_viewport_camera", "set_viewport_camera", "get_output_log", "execute_console_command"):
        if a in ua:
            head(f"util.{a} signature", ua[a], 500)

    # ---- vision ----
    va = summary_actions(call("vision", "list_actions"))
    print("=" * 72)
    print("vision actions:", sorted(va.keys()))
    for a in list(va.keys())[:12]:
        head(f"vision.{a} signature", va[a], 500)

    # ---- execute_python 返回形态 ----
    res = call("util", "execute_python", {"code": "import json\nprint(json.dumps({'hello':'world','n':42}))"})
    head("util.execute_python (raw result)", res, 2000)

    # ---- set_location 零净变化写测试 ----
    res = call("actor", "set_location", {"actor_label": "Hall_Floor", "location": [7800.0, 0.0, 840.0]})
    head("actor.set_location (write-back same loc)", res, 2000)

    # 再读一次确认没漂移
    head("actor.get_transform (after no-op write)", call("actor", "get_transform", {"actor_label": "Hall_Floor"}), 1500)

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
