"""探针 6：用**我们自己的**客户端复现 Computer Use 握手失败，逐步打印。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.adapters.mcp_client import StreamableHTTPTransport, make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    cu = cfg.section("mcp_servers.computer_use")
    url = cu["base_url"].rstrip("/") + (cu.get("endpoint") or "/mcp")

    print("### 1) 用 make_client_from_config 走一遍")
    client = make_client_from_config(cu, name="uha-probe6")
    print("   type:", type(client).__name__, "url:", getattr(client, "url", None))
    try:
        info = client.initialize()
        print("   initialize OK:", json.dumps(info, ensure_ascii=False)[:300])
        print("   session id:", getattr(client, "_session_id", None))
        tools = client.tool_names(refresh=True)
        print("   tools:", len(tools), tools[:8])
    except Exception as exc:
        print("   initialize FAILED:", type(exc).__name__, str(exc)[:600])
        print("   session id now:", getattr(client, "_session_id", None))
        print("   protocol_version now:", getattr(client, "protocol_version", None))
    finally:
        try:
            client.close()
        except Exception:
            pass

    print("\n### 2) 手动指定服务端支持的版本 2025-03-26 再试一次")
    t = StreamableHTTPTransport(url, name="uha-probe6b", timeout=20)
    t.protocol_version = "2025-03-26"
    try:
        info = t.initialize()
        print("   initialize OK:", json.dumps(info, ensure_ascii=False)[:300])
        print("   session:", t._session_id)
        print("   tools:", len(t.tool_names(refresh=True)))
    except Exception as exc:
        print("   FAILED:", type(exc).__name__, str(exc)[:600])
    finally:
        try:
            t.close()
        except Exception:
            pass

    print("\n### 3) 纯 httpx：initialize -> 带 session 的 tools/list")
    import httpx

    with httpx.Client(timeout=20) as c:
        r = c.post(url, headers={"Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "p", "version": "0"}}})
        sid = r.headers.get("mcp-session-id")
        print("   initialize:", r.status_code, "sid=", sid)
        for hdr_extra in ({"mcp-session-id": sid}, {"mcp-session-id": sid, "MCP-Protocol-Version": "2025-03-26"}):
            h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **hdr_extra}
            r2 = c.post(url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            print("   tools/list", list(hdr_extra.keys()), "->", r2.status_code, r2.text[:300].replace("\n", " "))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
