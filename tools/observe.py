#!/usr/bin/env python
"""独立观测器（只读）：直接问 UE 当前状态，不经 UnrealHybridAgent 的任何封装。

用途：**独立复核**。框架自己说"成功"不算证据，这个脚本从 MCP 原始通道
重新读一遍坐标与脏包，用来和框架的报告对账。

    python tools/observe.py Hall_Floor
    python tools/observe.py --all --limit 12
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.adapters.unreal.unreal_mcp import DIRTY_STATE_CODE  # noqa: E402
from src.core.config import load_config  # noqa: E402

DIRTY_SNIPPET = """
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
    ap = argparse.ArgumentParser(description="独立读取 UE 状态（只读）")
    ap.add_argument("actor", nargs="?", help="要读的 Actor label")
    ap.add_argument("--all", action="store_true", help="列出所有 actor")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    client = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-observe")
    client.initialize()
    client.tool_names(refresh=True)

    def call(domain, action, params=None):
        res = client.call_tool(domain, {"action": action, "params": params or {}})
        return res.get("data") if isinstance(res, dict) else res

    out: dict[str, object] = {}

    if args.actor:
        out["transform"] = call("actor", "get_transform", {"actor_label": args.actor})
        out["bounds"] = call("actor", "get_actor_bounds", {"actor_label": args.actor})

    if args.all:
        res = call("actor", "get_all_details")
        actors = (res or {}).get("actors") or []
        out["actor_count"] = len(actors)
        out["sample"] = [
            {"label": a.get("label"), "class": a.get("class"), "location": a.get("location")}
            for a in actors[: args.limit]
        ]

    out["level"] = call("level", "get_current_level_path")

    # 脏包
    res = client.call_tool("util", {"action": "execute_python", "params": {"code": DIRTY_SNIPPET}})
    data = res.get("data") or {}
    raw = data.get("result")
    if isinstance(raw, str):
        try:
            out["dirty"] = json.loads(raw)
        except json.JSONDecodeError:
            out["dirty_raw"] = raw
    else:
        out["dirty"] = data

    client.close()

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
