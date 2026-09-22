"""挑一个**带 tkinter** 的解释器来跑图形 HUD。

为什么需要这个（本机实测，架构文档 §10.2）：

=============================== =========
解释器                            tkinter
=============================== =========
`E:/UnrealHybridAgent/.venv`     ❌ 没有   ← UHA 主程序跑在这里
系统 Python 3.12                 ✅ 有
托管 Python 3.13.12              ❌ 没有
=============================== =========

UHA 跑在 3.13 的 ``.venv`` 里，而那个环境**没有 tkinter**（``ControlOverlay``
因此在 UHA 里静默失效）。所以桌面 HUD 必须另有办法找到能画窗口的解释器。

策略：按顺序探测候选解释器能不能 ``import tkinter``，第一个成功的就用它。
**不去改动 ``.venv``、也不要求用户装任何东西。**
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


def repo_root() -> Path:
    # uah/hosts/desktop/launch.py -> parents[3] == 仓库根
    return Path(__file__).resolve().parents[3]


def _candidates() -> list[str]:
    out: list[str] = [sys.executable]
    env = os.environ.get("UAH_PYTHON")
    if env:
        out.insert(0, env)
    home = Path.home()
    patterns = [
        home / "AppData" / "Local" / "Programs" / "Python" / "Python3*" / "python.exe",
        Path("C:/") / "Program Files" / "Python3*" / "python.exe",
        Path("C:/") / "Program Files (x86)" / "Python3*" / "python.exe",
        Path("C:/") / "Python3*" / "python.exe",
    ]
    for pattern in patterns:
        for hit in sorted(glob.glob(str(pattern)), reverse=True):
            out.append(hit)
    # PATH 上的 python（尽量靠后，避免拿到奇怪的解释器）
    for name in ("python.exe", "python3.exe"):
        which = _which(name)
        if which:
            out.append(which)
    # 去重且保序
    seen: set[str] = set()
    uniq: list[str] = []
    for item in out:
        key = os.path.normcase(os.path.abspath(item))
        if key not in seen and os.path.isfile(item):
            seen.add(key)
            uniq.append(item)
    return uniq


def _which(name: str) -> str | None:
    for folder in (os.environ.get("PATH") or "").split(os.pathsep):
        if not folder:
            continue
        cand = Path(folder) / name
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def has_tkinter(exe: str, *, timeout_s: float = 20.0) -> bool:
    """真跑一次 ``import tkinter`` —— 不信版本号，只信事实。"""
    try:
        proc = subprocess.run(
            [exe, "-c", "import tkinter, sys; sys.stdout.write('ok')"],
            capture_output=True, timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0 and b"ok" in (proc.stdout or b"")
    except Exception:  # noqa: BLE001
        return False


def tk_python(*, verbose: bool = False) -> str | None:
    """返回第一个带 tkinter 的解释器路径；都没有则 ``None``。"""
    for exe in _candidates():
        ok = has_tkinter(exe)
        if verbose:
            print(f"  {'OK  ' if ok else 'no  '} {exe}")
        if ok:
            return exe
    return None


def spawn_hud(url: str, *, extra: Iterable[str] = (), verbose: bool = False) -> int:
    """用合适的解释器把桌面 HUD 拉起来（独立进程）。返回进程 pid，失败返回 0。"""
    exe = tk_python(verbose=verbose)
    if exe is None:
        print("找不到带 tkinter 的 Python 解释器，无法启动图形 HUD。\n"
              "可用替代：python -m uah.hosts.desktop.text_hud\n"
              "或设置环境变量 UAH_PYTHON 指向一个带 tkinter 的解释器。", file=sys.stderr)
        return 0
    cmd: list[str] = [exe, "-m", "uah.hosts.desktop", "--url", url, *extra]
    proc = subprocess.Popen(cmd, cwd=str(repo_root()),
                            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    if verbose:
        print(f"hud pid={proc.pid} python={exe}")
    return int(proc.pid)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from ...core.transport import DEFAULT_HUB_HOST, DEFAULT_HUB_PORT, hub_url

    p = argparse.ArgumentParser(prog="uah-hud-launch", description="启动桌面 HUD（自动挑解释器）")
    p.add_argument("--url", default=hub_url(DEFAULT_HUB_HOST, DEFAULT_HUB_PORT))
    p.add_argument("--list", action="store_true", help="只列出候选解释器与 tkinter 可用性")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.list:
        exe = tk_python(verbose=True)
        print(f"选中：{exe or '（无）'}")
        return 0 if exe else 2
    pid = spawn_hud(args.url, verbose=True)
    return 0 if pid else 2


__all__ = ["tk_python", "has_tkinter", "spawn_hud", "repo_root", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
