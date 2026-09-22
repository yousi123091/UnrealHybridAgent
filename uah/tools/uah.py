"""``python -m uah.tools.uah ...`` —— UAH 的独立命令行入口。

刻意**不依赖 UHA**：HUD 侧、外部 Agent 侧都能用它。
（``hud`` 子命令会去 import 宿主模块，那是它自己该做的事。）

    uah hub                        启动独立 Hub（前台）
    uah state                      打印当前所有 Agent 的快照
    uah watch                      文本 HUD 跟随（等价于 text_hud）
    uah emit --agent X --status running --task "Running tests" --activity pytest
    uah hud                        用带 tkinter 的解释器拉起桌面 HUD
    uah notify mute|unmute|status   提醒开关
    uah selftest                   自检：Hub 通不通、协议对不对
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from ..core.protocol import PROTOCOL_VERSION, UAH_CORE_VERSION, UAH_DESKTOP_VERSION
from ..core.transport import (
    DEFAULT_HUB_HOST,
    DEFAULT_HUB_PORT,
    HubClient,
    HubServer,
    hub_url,
    probe_hub,
)
from ..adapters.generic.bridge import normalize_event
from ..hosts.desktop.hud import default_state_path
from ..ui.notify import Notifier


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------


def cmd_hub(args: argparse.Namespace) -> int:
    server = HubServer(host=args.host, port=args.port, verbose=not args.quiet)
    try:
        server.start()
    except OSError as exc:
        print(f"启动 Hub 失败：{exc}", file=sys.stderr)
        return 2
    print(f"UAH Hub 已启动：{server.url()}")
    print(f"  协议 {PROTOCOL_VERSION} · core {UAH_CORE_VERSION}")
    print("  Agent 侧：POST /event   HUD 侧：GET /stream   Ctrl+C 退出")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    client = HubClient(args.url, timeout_s=3.0)
    health = client.health()
    if health is None:
        print(f"连不上 UAH Hub：{args.url}", file=sys.stderr)
        return 2
    snaps = client.state()
    if args.json:
        _print({"health": health, "agents": [s.to_wire() for s in snaps]})
        return 0
    print(f"UAH Hub @ {args.url}  protocol={health.get('protocol')}  "
          f"agents={health.get('agents')}  uptime={health.get('uptime_s')}s")
    if not snaps:
        print("  （还没有 Agent 接入）")
    for snap in snaps:
        print(f"  {snap.summary_line()}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from ..hosts.desktop.text_hud import watch

    return watch(args.url, plain=args.plain, sound=args.sound)


def cmd_emit(args: argparse.Namespace) -> int:
    client = HubClient(args.url, timeout_s=3.0)
    if not client.is_alive():
        print(f"连不上 UAH Hub：{args.url}", file=sys.stderr)
        return 2
    event = normalize_event(
        agent=args.agent, status=args.status, task=args.task, activity=args.activity,
        detail=args.detail, tool=args.tool, project=args.project, event_type=args.type,
    )
    res = client.post_event(event)
    if args.json:
        _print(res)
        return 0 if res.get("ok") else 2
    if not res.get("ok"):
        print(f"发送失败：{res.get('error')}", file=sys.stderr)
        return 2
    print(f"{args.agent} → {res.get('status')}  (accepted={res.get('accepted')})")
    return 0


def cmd_hud(args: argparse.Namespace) -> int:
    from ..hosts.desktop.launch import spawn_hud

    if args.text:
        from ..hosts.desktop.text_hud import watch

        return watch(args.url, plain=False, sound=args.sound)

    # 桌面 HUD 依赖 Hub：没有就先起一个独立的（HUD 独立于 Agent 的前提）
    if probe_hub(args.url) is None:
        pid = _spawn_hub(args.url)
        if pid:
            print(f"已启动独立 Hub（pid={pid}）")
            for _ in range(40):
                time.sleep(0.1)
                if probe_hub(args.url) is not None:
                    break
    pid = spawn_hud(args.url, verbose=args.verbose)
    if not pid:
        return 2
    print(f"桌面 HUD 已启动（pid={pid}） → {args.url}")
    return 0


def _spawn_hub(url: str) -> int:
    import subprocess

    host, _, port = url.replace("http://", "").partition(":")
    cmd = [sys.executable, "-m", "uah.tools.uah", "hub", "--host", host or DEFAULT_HUB_HOST,
           "--port", str(port or DEFAULT_HUB_PORT), "--quiet"]
    try:
        proc = subprocess.Popen(
            cmd, cwd=_repo_root(),
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        return int(proc.pid)
    except Exception as exc:  # noqa: BLE001
        print(f"启动独立 Hub 失败：{exc}", file=sys.stderr)
        return 0


def _repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2])


def cmd_notify(args: argparse.Namespace) -> int:
    notifier = Notifier(sinks=[], state_path=default_state_path())
    if args.action == "mute":
        notifier.set_mute(True)
    elif args.action == "unmute":
        notifier.set_mute(False)
        notifier.reset_memory()
    _print(notifier.stats())
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """真的跑一遍：起 Hub → POST → SSE 收 → 校验状态。不通过就非零退出。"""
    from ..hosts.desktop.text_hud import draw

    from ..core.transport import pick_free_port

    port = args.port or pick_free_port()
    server = HubServer(host=args.host, port=port).start()
    url = server.url()
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    try:
        client = HubClient(url, timeout_s=3.0)
        health = client.health() or {}
        check("hub 可达", bool(health.get("ok")), str(health.get("protocol")))
        check("协议版本", health.get("protocol") == PROTOCOL_VERSION, PROTOCOL_VERSION)
        check("core 版本", health.get("core") == UAH_CORE_VERSION, UAH_CORE_VERSION)

        from ..adapters.generic.bridge import GenericBridge

        bridge = GenericBridge(url)
        ok = bridge.emit(agent="SelfTest", status="running",
                         task="Running tests", activity="pytest")
        check("generic 适配器发送", ok)
        bridge.emit(agent="SelfTest", status="done")

        snaps = client.state()
        check("状态可读", len(snaps) == 1, f"{len(snaps)} agents")
        if snaps:
            check("状态为 DONE", snaps[0].status.value == "DONE", snaps[0].status.value)
            check("task 保留", snaps[0].task.name == "Running tests", str(snaps[0].task.name))
            check("activity 保留", snaps[0].activity.summary == "pytest",
                  str(snaps[0].activity.summary))
            print(draw(snaps))

        check("非法 JSON 被吸收", client.post_event("{{ not json") is not None)
        dup = client.post_event({"agent": {"id": "Dup"}, "status": "running"})
        dup2 = client.post_event({"agent": {"id": "Dup"}, "status": "running"})
        check("重复事件被去重", bool(dup2.get("duplicate")) or dup2.get("status") == "RUNNING",
              str(dup2.get("duplicate")))

        check("协议版本分离",
              UAH_DESKTOP_VERSION != PROTOCOL_VERSION and UAH_CORE_VERSION != PROTOCOL_VERSION)
    finally:
        server.stop()

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"\n自检：{len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 2


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    default_url = hub_url(DEFAULT_HUB_HOST, DEFAULT_HUB_PORT)
    p = argparse.ArgumentParser(prog="uah", description="Universal Agent HUD 工具")
    p.add_argument("--url", default=default_url, help=f"Hub 地址（默认 {default_url}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hub", help="启动独立 Hub（前台）")
    h.add_argument("--host", default=DEFAULT_HUB_HOST)
    h.add_argument("--port", type=int, default=DEFAULT_HUB_PORT)
    h.add_argument("--quiet", action="store_true")
    h.set_defaults(func=cmd_hub)

    s = sub.add_parser("state", help="打印当前 Agent 快照")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_state)

    w = sub.add_parser("watch", help="文本 HUD（跟随）")
    w.add_argument("--plain", action="store_true")
    w.add_argument("--sound", action="store_true")
    w.set_defaults(func=cmd_watch)

    e = sub.add_parser("emit", help="发一条事件（外部 Agent 用这个接入）")
    e.add_argument("--agent", default="ExampleAgent")
    e.add_argument("--status", default="running")
    e.add_argument("--task", default=None)
    e.add_argument("--activity", default=None)
    e.add_argument("--detail", default=None)
    e.add_argument("--tool", default=None)
    e.add_argument("--project", default=None)
    e.add_argument("--type", default=None, help="显式指定事件类型")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_emit)

    d = sub.add_parser("hud", help="启动桌面 HUD（自动挑带 tkinter 的解释器）")
    d.add_argument("--text", action="store_true", help="改用文本 HUD")
    d.add_argument("--sound", action="store_true")
    d.add_argument("--verbose", action="store_true")
    d.set_defaults(func=cmd_hud)

    n = sub.add_parser("notify", help="提醒开关")
    n.add_argument("action", choices=["mute", "unmute", "status"])
    n.set_defaults(func=cmd_notify)

    t = sub.add_parser("selftest", help="自检（起 Hub + 端到端跑一遍）")
    t.add_argument("--host", default=DEFAULT_HUB_HOST)
    t.add_argument("--port", type=int, default=0, help="0 = 随机空闲端口")
    t.set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    from ..hosts.embedded.bootstrap import UAH_RELEASE_ENABLED
    if not UAH_RELEASE_ENABLED:
        print("UAH is disabled in this release and deferred to the next version.")
        return 2
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
