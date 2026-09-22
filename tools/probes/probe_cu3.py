"""探针 7：列出 Computer Use 的全部工具与关键入参，找"聚焦窗口"能力。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    c = make_client_from_config(cfg.section("mcp_servers.computer_use"), name="uha-probe7")
    c.initialize()
    specs = c.list_tools(refresh=True)
    print(f"共 {len(specs)} 个工具\n")
    for t in specs:
        props = (t.input_schema or {}).get("properties") or {}
        req = (t.input_schema or {}).get("required") or []
        print(f"- {t.name}")
        print(f"    desc: {t.description[:150]}")
        print(f"    args: {', '.join(sorted(props)) or '-'}   required={req or '-'}")

    print("\n--- 与窗口/焦点/进程相关 ---")
    for t in specs:
        blob = (t.name + " " + t.description).lower()
        if any(k in blob for k in ("window", "focus", "activ", "app", "process", "list")):
            print(" *", t.name, "|", t.description[:180])
            print("   schema:", json.dumps(t.input_schema, ensure_ascii=False)[:400])

    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
