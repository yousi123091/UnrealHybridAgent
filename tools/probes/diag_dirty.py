"""受控实验 + 复原：确认"写操作是否真的把 Level 标记为脏"。

顺带把 Hall_Floor 恢复到 Demo1 之前的原始坐标（Z=840）。
之前为了让 demo 跑通，Z 被抬到 880，这里复位——**实验不留副作用**。

流程：读脏 -> 读坐标 -> 写回 840 -> 读脏 -> 读坐标 -> 保存 -> 读脏
每一步都打印，用来判断"脏包 0→0"到底是真干净还是查询有问题。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402

DIRTY = """
import unreal, json as _json
pkgs = unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
names = []
for p in pkgs:
    try:
        names.append(str(p.get_name()))
    except Exception:
        names.append(str(p))
print(_json.dumps({"dirty_count": len(names), "dirty_packages": names}))
"""


def main() -> int:
    cfg = load_config()
    c = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-restore")
    c.initialize()
    c.tool_names(refresh=True)

    def call(domain, action, params=None):
        res = c.call_tool(domain, {"action": action, "params": params or {}})
        return res.get("data") if isinstance(res, dict) else res

    def dirty():
        res = c.call_tool("util", {"action": "execute_python", "params": {"code": DIRTY}})
        data = res.get("data") or {}
        raw = data.get("result")
        return json.loads(raw) if isinstance(raw, str) else data

    def z(label="Hall_Floor"):
        t = call("actor", "get_transform", {"actor_label": label}) or {}
        return t.get("location")

    print("step0  dirty =", dirty())
    print("step0  loc   =", z())

    print("\nstep1  写回原始坐标 [7800, 0, 840] ...")
    print("       ->", call("actor", "set_location", {"actor_label": "Hall_Floor", "location": [7800.0, 0.0, 840.0]}))

    print("step1  dirty =", dirty())
    print("step1  loc   =", z())

    print("\nstep2  保存 Level ...")
    print("       ->", call("level", "save_current_level"))

    print("step2  dirty =", dirty())
    print("step2  loc   =", z())

    print("\nstep3  再写一次 +20 然后又写回，验证'写必脏'的因果")
    call("actor", "set_location", {"actor_label": "Hall_Floor", "location": [7800.0, 0.0, 860.0]})
    print("       移后 dirty =", dirty())
    call("actor", "set_location", {"actor_label": "Hall_Floor", "location": [7800.0, 0.0, 840.0]})
    print("       复位 dirty =", dirty())
    print("       保存 ->", call("level", "save_current_level"))
    print("       保存后 dirty =", dirty())
    print("       最终 loc =", z())

    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
