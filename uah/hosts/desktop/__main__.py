"""``python -m uah.hosts.desktop`` —— 启动独立桌面 HUD。

如果当前解释器没有 tkinter，**不报错退出**：打印一句清楚的提示，
并告诉用户 ``python -m uah.tools.uah hud`` 可以自动换一个能画窗口的解释器。
"""

from __future__ import annotations

import argparse
import sys

from ...core.transport import DEFAULT_HUB_HOST, DEFAULT_HUB_PORT, hub_url


def _cleanup_and_exit(app, code: int) -> None:
    """收尾并**确定性**退出。

    为什么需要 ``os._exit``：Windows 上 tkinter 进程在解释器收尾阶段
    （Tcl 线程终结 / 已销毁窗口的控件析构）**可能长时间不返回**，
    表现为"窗口已经关了，进程还在"。

    所有该做的收尾都已经在 ``app.close()`` 里**显式**做完了
    （取消 after 回调 → 断 SSE → 落盘静音配置 → 销毁窗口），
    所以这里直接退出，不留一个僵尸 GUI 进程。
    """
    import os
    import sys

    try:
        app.close()
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        # Tcl DLL teardown can hang even os._exit after all windows are destroyed.
        # Terminate ONLY this already-cleaned HUD process; never its independent Hub.
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.TerminateProcess(kernel.GetCurrentProcess(), int(code))
    os._exit(int(code))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="uah-hud", description="UAH 独立桌面 HUD")
    p.add_argument("--url", default=hub_url(DEFAULT_HUB_HOST, DEFAULT_HUB_PORT))
    p.add_argument("--mode", choices=["compact", "expanded", "dashboard"])
    p.add_argument("--width", type=int, default=320)
    p.add_argument("--max-height", type=int, default=560)
    p.add_argument("--corner", default="top-right",
                   choices=["top-right", "top-left", "bottom-right", "bottom-left"])
    p.add_argument("--no-topmost", action="store_true")
    p.add_argument("--no-sound", action="store_true")
    p.add_argument("--no-toast", action="store_true")
    args = p.parse_args(argv)

    try:
        from .hud import HudApp
    except Exception as exc:  # noqa: BLE001
        print(f"桌面 HUD 不可用：{exc}", file=sys.stderr)
        return 2

    from ...core.daemon import ensure_persistent_url
    ensure_persistent_url(args.url)
    app = HudApp(
        args.url,
        mode=args.mode,
        width=args.width,
        max_height=args.max_height,
        topmost=not args.no_topmost,
        corner=args.corner,
        notify_sound=not args.no_sound,
        notify_toast=not args.no_toast,
    )
    try:
        app.build()
    except RuntimeError as exc:
        print(f"{exc}\n\n改用：python -m uah.hosts.desktop.text_hud --url {args.url}",
              file=sys.stderr)
        return 3
    app.start_stream()
    code = 0
    try:
        app._root.mainloop()  # noqa: SLF001 - 入口即拥有主循环
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"HUD 主循环异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    _cleanup_and_exit(app, code)
    return code  # pragma: no cover - _cleanup_and_exit 不会返回


if __name__ == "__main__":
    raise SystemExit(main())
