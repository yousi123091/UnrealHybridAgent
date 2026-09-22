"""临时探针：直连 Unreal MCP，打印 actor 域各种动作的原始响应形态。

目的：确认 GenOrca v2.2.0 的 actor.get_transform 到底返回什么结构，
从而修正 _extract_rows / ActorRef.from_payload。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402


def show(title, obj, limit=4000):
    print("=" * 72)
    print(title)
    print("-" * 72)
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:limit])


def main() -> int:
    cfg = load_config()
    section = cfg.section("mcp_servers.unreal_mcp")
    print("SECTION:", json.dumps({k: v for k, v in section.items() if k != "env"}, ensure_ascii=False))

    client = make_client_from_config(section, name="uha-probe")
    client.initialize()
    tools = client.tool_names(refresh=True)
    show("TOOLS", tools)

    res = client.call_tool("actor", {"action": "list_actions", "params": {}})
    show("actor.list_actions (raw)", res)

    data = (res.get("data") or {}) if isinstance(res, dict) else {}
    actions = (data.get("actions") or {}) if isinstance(data, dict) else {}
    for a in ("get_transform", "set_location", "get_all_details", "get_actor_bounds"):
        show(f"signature: {a}", actions.get(a))

    for label in ["Hall_Floor", "Hall_Floor_1", "Floor", "HallFloor"]:
        res = client.call_tool("actor", {"action": "get_transform", "params": {"actor_label": label}})
        show(f"actor.get_transform actor_label={label!r}", res)

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
