#!/usr/bin/env python
"""桌面 HUD 的**真实窗口**冒烟测试。

必须用**带 tkinter 的解释器**跑（本机是系统 Python 3.12；项目 ``.venv`` 没有 tkinter）：

    "C:/Users/PUBLIC_USER/AppData/Local/Programs/Python/Python312/python.exe" uah/tests/gui_smoke.py

它不做断言式的"单元测试"，而是真的：起 Hub → 建窗口 → 灌两个 Agent 的事件 →
让 Tk 事件循环跑起来 → 检查卡片真的被创建、状态文本真的对 → 关窗口。

为什么需要它：``test_uah_phase1.py`` 只验证数据通路（SSE + render_card），
跑在 ``.venv`` 里，碰不到窗口系统。而"窗口到底能不能画出来"是需求 §十一
的硬要求，不能用"数据对了所以窗口应该也对"来搪塞。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if detail else ""))
    return bool(ok)


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("当前解释器没有 tkinter，无法做窗口冒烟测试。")
        print("请用带 tkinter 的解释器，例如系统 Python 3.12。")
        return 3

    from uah.adapters.generic.bridge import GenericBridge
    from uah.core.transport import HubServer, pick_free_port
    from uah.hosts.desktop.hud import HudApp
    from uah.hosts.desktop.launch import has_tkinter, tk_python

    print("=== 解释器 ===")
    print(f"  {sys.executable} ({sys.version.split()[0]})")
    picked = tk_python()
    check("能找到带 tkinter 的解释器", picked is not None, str(picked))
    check("当前解释器带 tkinter", has_tkinter(sys.executable))

    port = pick_free_port()
    server = HubServer(port=port).start()
    url = server.url()

    print("\n=== 建窗口 ===")
    import tempfile
    settings = Path(tempfile.mkdtemp()) / "window.json"
    app = HudApp(url, mode="dashboard", settings_path=settings, notify_sound=False, notify_toast=False, poll_ms=80, clock_ms=200)
    try:
        root = app.build()
    except Exception as exc:  # noqa: BLE001
        check("窗口能创建", False, f"{type(exc).__name__}: {exc}")
        server.stop()
        return 1
    check("窗口能创建", True, rect := f"{root.winfo_width()}x{root.winfo_height()}")
    check("窗口置顶已设置", bool(root.attributes("-topmost")), str(root.attributes("-topmost")))
    check("窗口尺寸在要求范围内（小、不占屏）",
          240 <= root.winfo_width() <= 420, str(root.winfo_width()))

    app.start_stream()

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                root.update()
            except Exception:  # noqa: BLE001 - 窗口已销毁
                return
            time.sleep(0.02)

    pump(0.6)
    check("Hub 连接状态已显示", "已连接" in app._conn_label.cget("text"), app._conn_label.cget("text"))
    check("初始显示空态提示", len(app._cards) == 0, f"{len(app._cards)} cards")

    print("\n=== 灌两个 Agent ===")
    bridge = GenericBridge(url)
    bridge.emit(agent="UHA", status="running", project="ExamplePalace",
                task="demo1_raise_and_save", activity="结构化执行")
    bridge.emit(agent="Codex", status="waiting_approval", project="demo",
                task="Running tests", activity="Run test command?")
    pump(1.0)

    check("两张卡片都建出来了", len(app._cards) == 2, str(sorted(app._cards)))
    views = {aid: card.view for aid, card in app._cards.items()}
    check("卡片标题正确", sorted(v.title for v in views.values()) == ["Codex", "UHA"],
          str(sorted(v.title for v in views.values())))
    check("UHA 卡片是 RUNNING",
          views["UHA"].status.value == "RUNNING", views["UHA"].status.value)
    check("Codex 卡片是 WAITING_APPROVAL",
          views["Codex"].status.value == "WAITING_APPROVAL", views["Codex"].status.value)
    check("卡片真的挂进了窗口树",
          all(card.frame.winfo_ismapped() for card in app._cards.values()),
          str([card.frame.winfo_ismapped() for card in app._cards.values()]))
    check("等待批准卡片用了告警边框色",
          views["Codex"].border_color == "#f0883e", views["Codex"].border_color)

    print("\n=== 状态更新与提醒（不发声/不弹系统通知）===")
    bridge.emit(agent="UHA", status="done", task="demo1_raise_and_save",
                activity="37 tests passed")
    pump(1.0)
    check("UHA 卡片更新为 DONE", app._cards["UHA"].view.status.value == "DONE",
          app._cards["UHA"].view.status.value)
    check("DONE 触发了提醒（有记录）",
          any(n.status.value == "DONE" for n in app.notifier.sent()),
          str([n.status.value for n in app.notifier.sent()]))
    check("提醒通道可查（声音/通知通道状态存在）",
          isinstance(app.notifier.stats().get("sinks"), list),
          str(app.notifier.stats()["sinks"]))

    print("\n=== 静音按钮 ===")
    before = app._mute_text()
    app.toggle_mute()
    pump(0.3)
    check("静音按钮切换生效", app.notifier.muted and app._mute_button.cget("text") != before,
          f"{before} → {app._mute_button.cget('text')}")
    app.toggle_mute()
    pump(0.2)
    check("再次点击恢复", not app.notifier.muted)

    print("\n=== 关闭 ===")
    app.close()
    pump(0.3)
    check("窗口已关闭", app._stop.is_set())
    server.stop()

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 60)
    if failed:
        print(f"失败 {len(failed)} / {len(RESULTS)} 项：")
        for name, _ok, detail in failed:
            print(f"  - {name}  {detail}")
        return _exit_now(1)
    print(f"窗口冒烟：全部通过（{len(RESULTS)} 项）")
    return _exit_now(0)


def _exit_now(code: int) -> int:
    """确定性退出。

    为什么测试里也要 ``os._exit``：Windows 上 tkinter 进程在**解释器收尾**阶段
    可能长时间不返回（Tcl 线程终结 / 已销毁控件的析构），
    于是"测试都跑完了、进程还在"—— 会让 CI 和 harness 看起来像卡死。
    该验证的东西都已经验证完了（窗口建了、卡片画了、关窗口也走完了），
    所以这里直接退出。产品入口 ``uah.hosts.desktop.__main__`` 用同一套收尾顺序。
    """
    import os
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    from uah.hosts.desktop.__main__ import _cleanup_and_exit
    _cleanup_and_exit(None, code)
    return int(code)  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
