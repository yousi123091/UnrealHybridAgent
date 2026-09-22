"""无 tkinter 的降级 HUD —— 纯文本。

存在的理由不是"好看"，而是**本机 ``.venv`` 里没有 tkinter**（架构文档 §10.2）。
所以：

* 它让 UAH 在**任何**解释器上都能被看到 —— 包括跑 UHA 本身的 3.13 ``.venv``；
* 它和图形宿主**用同一套** ``render_card()``，所以"两个宿主显示不一致"不会发生；
* CI / 自动化测试用它做断言（不需要窗口系统）。

三种用法：

    python -m uah.hosts.desktop.text_hud --once     # 打印当前状态就退出
    python -m uah.hosts.desktop.text_hud            # 跟随 SSE 流，变化时重画
    python -m uah.hosts.desktop.text_hud --plain    # 不做 ANSI 清屏，逐行追加（日志友好）
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any

from ...core.models import AgentSnapshot
from ...core.transport import DEFAULT_HUB_HOST, DEFAULT_HUB_PORT, HubClient, hub_url
from ...ui.components.card import render_card
from ...ui.notify import CollectSink, Notifier, SoundSink

CLEAR = "\x1b[2J\x1b[H"


def draw(snapshots: list[AgentSnapshot], *, plain: bool = False, header: str = "") -> str:
    """把快照渲染成文本。**与图形宿主共用 render_card**，所以内容天然一致。"""
    now = time.time()
    lines: list[str] = []
    if header:
        lines.append(header)
        lines.append("─" * 46)
    if not snapshots:
        lines.append("（还没有 Agent 接入）")
    for snap in snapshots:
        lines.extend(render_card(snap, now=now).to_lines())
        lines.append("")
    return "\n".join(lines)


def _pick_notifier(sound: bool) -> Notifier:
    sinks: list[Any] = []
    if sound:
        sinks.append(SoundSink())
    sinks.append(CollectSink())
    return Notifier(sinks=sinks, state_path=None)


def run_once(url: str, *, plain: bool = False) -> int:
    client = HubClient(url, timeout_s=3.0)
    health = client.health()
    if health is None:
        print(f"连不上 UAH Hub：{url}", file=sys.stderr)
        return 2
    print(draw(client.state(), plain=plain,
               header=f"UAH @ {url}  protocol={health.get('protocol')}  agents={health.get('agents')}"))
    return 0


def watch(url: str, *, plain: bool = False, sound: bool = False,
          stop: threading.Event | None = None, show_header: bool = True) -> int:
    client = HubClient(url, timeout_s=3.0)
    snaps: dict[str, AgentSnapshot] = {}
    notifier = _pick_notifier(sound)
    lock = threading.Lock()
    dirty = threading.Event()
    status_text = ["未连接"]

    def _on_snapshot(snap: AgentSnapshot) -> None:
        with lock:
            snaps[snap.agent.id] = snap
        notifier.consider(snap)
        dirty.set()

    def _on_removed(agent_id: str) -> None:
        with lock:
            snaps.pop(agent_id, None)
        dirty.set()

    def _on_status(text: str) -> None:
        status_text[0] = text
        dirty.set()

    stream_stop = stop or threading.Event()
    thread = threading.Thread(
        target=lambda: client.stream(_on_snapshot, on_removed=_on_removed,
                                     on_status=_on_status, stop=stream_stop),
        name="uah-text-hud", daemon=True,
    )
    thread.start()

    try:
        while not stream_stop.is_set():
            if not dirty.wait(1.0):
                continue
            dirty.clear()
            with lock:
                ordered = sorted(snaps.values(), key=lambda s: s.status.value)
            header = (
                f"UAH @ {url}  ·  {status_text[0]}  ·  agents={len(ordered)}"
                if show_header else ""
            )
            text = draw(ordered, plain=plain, header=header)
            if plain:
                print(text, flush=True)
            else:
                sys.stdout.write(CLEAR + text + "\n")
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        stream_stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="uah-text-hud", description="UAH 文本 HUD（无 tkinter 依赖）")
    p.add_argument("--url", default=hub_url(DEFAULT_HUB_HOST, DEFAULT_HUB_PORT))
    p.add_argument("--once", action="store_true", help="打印当前状态后退出")
    p.add_argument("--plain", action="store_true", help="不做 ANSI 清屏")
    p.add_argument("--sound", action="store_true", help="状态提醒时响一声")
    args = p.parse_args(argv)
    if args.once:
        return run_once(args.url, plain=args.plain)
    return watch(args.url, plain=args.plain, sound=args.sound)


if __name__ == "__main__":
    raise SystemExit(main())
