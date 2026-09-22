"""探针 5：Computer Use（Agent-TARS）MCP HTTP 端点到底怎么了。

报错是 HTTP 400 "Server already initialized"。服务确实在监听，只是握手被拒。
搞清楚：是"必须复用已有会话"，还是"必须带 session id"，还是"根本不该再 initialize"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.core.config import load_config  # noqa: E402

BASE = "http://127.0.0.1:8788"


def show(title, obj):
    print("=" * 72)
    print(title)
    print("-" * 72)
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:1800])


def main() -> int:
    cfg = load_config()
    cu = cfg.section("mcp_servers.computer_use")
    print("CU section:", json.dumps({k: v for k, v in cu.items() if k != "token"}, ensure_ascii=False))

    c = httpx.Client(timeout=15.0)

    for path in ("/health", "/", "/sse"):
        try:
            r = c.get(BASE + path)
            show(f"GET {path} -> {r.status_code}", {"ctype": r.headers.get("content-type"), "body": r.text[:600]})
        except Exception as exc:
            show(f"GET {path} FAILED", str(exc))

    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "uha-probe", "version": "0.1"},
        },
    }
    for label, headers in (
        ("no session header", {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}),
        ("with bogus session", {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                "mcp-session-id": "probe-session-1"}),
    ):
        try:
            r = c.post(BASE + "/mcp", headers=headers, json=init)
            show(f"POST /mcp initialize ({label}) -> {r.status_code}", {
                "headers": {k: v for k, v in r.headers.items() if "session" in k.lower() or "content-type" in k.lower()},
                "body": r.text[:800],
            })
        except Exception as exc:
            show(f"POST /mcp initialize ({label}) FAILED", str(exc))

    # 直接试 tools/list（不 initialize），看服务是否本来就绪
    tl = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    for label, headers in (
        ("no session", {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}),
        ("bogus session", {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                           "mcp-session-id": "probe-session-1"}),
    ):
        try:
            r = c.post(BASE + "/mcp", headers=headers, json=tl)
            body = r.text[:800]
            show(f"POST /mcp tools/list ({label}) -> {r.status_code}", body)
        except Exception as exc:
            show(f"POST /mcp tools/list ({label}) FAILED", str(exc))

    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
