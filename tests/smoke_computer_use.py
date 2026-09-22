"""冒烟测试 1：Computer Use 适配器连通性。

只做**只读**验证，不抢桌面锁、不动键鼠：
    * MCP 握手 + tools/list
    * /health 探测
    * 能力矩阵（哪些工具缺了）
    * 真截一张图并落盘（验证 image 内容解析正确）

运行：
    python -m tests.smoke_computer_use
或
    python tests/smoke_computer_use.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.adapters.computer_use import (  # noqa: E402
    EXCLUDED_TOOLS,
    INPUT_TOOLS,
    LOCK_TOOLS,
    READ_ONLY_TOOLS,
    ComputerUseAdapter,
)
from src.adapters.mcp_client import StreamableHTTPTransport  # noqa: E402

URL = "http://127.0.0.1:8788/mcp"
ART = ROOT / "artifacts"
FAILS: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print(f"--- smoke: computer use @ {URL} ---")

    # 1) 裸客户端握手
    raw = StreamableHTTPTransport(URL, timeout=20)
    try:
        info = raw.initialize()
        check("MCP initialize", bool(info.get("server_info")), str(info.get("server_info")))
        check("protocol version", bool(info.get("protocol_version")), str(info.get("protocol_version")))
        names = raw.tool_names(refresh=True)
        check("tools/list", len(names) >= 17, f"{len(names)} tools")
        h = raw.health()
        check("/health", bool(h.get("ok")), f"tools={h.get('tools')} lock_holder={((h.get('lock') or {}).get('holder'))}")
    finally:
        raw.close()

    # 2) 适配器层
    cu = ComputerUseAdapter("http://127.0.0.1:8788")
    try:
        cap = cu.capabilities()
        check("adapter connect", cap["available"], f"{cap['count']} tools")
        check(
            "all required tools present",
            not cap["missing"],
            "missing=" + (",".join(cap["missing"]) if cap["missing"] else "none"),
        )
        check("computer_do excluded", "computer_do" in EXCLUDED_TOOLS)
        check(
            "tool families covered",
            len(READ_ONLY_TOOLS) == 4 and len(LOCK_TOOLS) == 2 and len(INPUT_TOOLS) == 10,
            f"readonly={len(READ_ONLY_TOOLS)} lock={len(LOCK_TOOLS)} input={len(INPUT_TOOLS)}",
        )

        size = cu.get_screen_size()
        check("get_screen_size", size.width > 0 and size.height > 0, f"{size.width}x{size.height} scale={size.scale_factor}")

        st = cu.status()
        check("status", "lock" in st or isinstance(st, dict), str(st)[:120])

        out = ART / "smoke_screenshot.png"
        shot = cu.screenshot(max_width=640, save_to=out)
        check("screenshot parsed", shot.width > 0 and len(shot.image) > 1000, f"{shot.width}x{shot.height} {len(shot.image)} b64chars")
        check("screenshot saved", out.is_file() and out.stat().st_size > 1000, f"{out} {out.stat().st_size if out.exists() else 0} bytes")
    finally:
        cu.close()

    print("\n" + ("SMOKE OK" if not FAILS else f"SMOKE FAILED: {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
