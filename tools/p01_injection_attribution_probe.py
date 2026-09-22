"""P0.1 — which injection paths are distinguishable from physical input?

The whole "human always wins" design depends on one fact: can we tell the agent's own
input apart from the human's? This probe answers it with the OS, not with a guess.

It installs ONE low-level mouse hook — correctly: installed AND pumped on the same
dedicated thread, ``restype`` set, observe-only (always CallNextHookEx, never swallows),
time-boxed, and always unhooked — then drives three different injection paths and
reports the ``LLMHF_INJECTED`` flag observed for each.

  path 1  user32.SetCursorPos            (what some native mouse libs use for "instant move")
  path 2  user32.SendInput(MOUSEEVENTF_MOVE)
  path 3  Agent-TARS computer_move_mouse  (what UHA actually calls)

Run:  python -m tools.p01_injection_attribution_probe
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.SetCursorPos.restype = wt.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SendInput.restype = ctypes.c_uint

WH_MOUSE_LL = 14
HC_ACTION = 0
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class _IU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _IU)]


def _send(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, data, flags, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _cursor() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


class FlagRecorder:
    """Records (wParam, injected?) for every LL mouse event while a window is open."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.hook = None
        self.ready = False
        self.error: str | None = None
        self._stop = False
        self._cb = None
        self._th: threading.Thread | None = None

    def _proc(self, nCode, wParam, lParam):
        # Observe-only: read two integers, append, chain. Never swallow.
        try:
            if nCode == HC_ACTION and lParam:
                info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if wParam == WM_MOUSEMOVE:
                    self.events.append({
                        "msg": "move",
                        "injected": bool(info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)),
                        "pt": [info.pt.x, info.pt.y],
                    })
        except Exception:
            pass
        return int(user32.CallNextHookEx(self.hook, nCode, wParam, lParam) or 0)

    def _run(self) -> None:
        self._cb = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wt.WPARAM, wt.LPARAM)(self._proc)
        self.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._cb, None, 0)
        if not self.hook:
            self.error = f"SetWindowsHookExW failed err={ctypes.get_last_error()}"
            return
        self.ready = True
        msg = wt.MSG()
        while not self._stop:
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.001)

    def __enter__(self) -> "FlagRecorder":
        self._th = threading.Thread(target=self._run, name="p01-attrib-hook", daemon=True)
        self._th.start()
        t0 = time.time()
        while time.time() - t0 < 2.0 and not self.ready and self.error is None:
            time.sleep(0.01)
        return self

    def __exit__(self, *exc) -> bool:
        self._stop = True
        try:
            if self.hook:
                user32.UnhookWindowsHookEx(self.hook)
        except Exception:
            pass
        if self._th:
            self._th.join(timeout=1.0)
        return False

    def window(self, label: str):
        return _Window(self, label)


class _Window:
    def __init__(self, rec: FlagRecorder, label: str):
        self.rec, self.label = rec, label
        self.start = 0

    def __enter__(self):
        self.start = len(self.rec.events)
        return self

    def __exit__(self, *exc):
        got = self.rec.events[self.start:]
        moves = [e for e in got if e["msg"] == "move"]
        inj = sum(1 for e in moves if e["injected"])
        phys = len(moves) - inj
        self.rec.results.append({
            "path": self.label,
            "move_events": len(moves),
            "flagged_injected": inj,
            "flagged_physical": phys,
            "distinguishable": phys == 0 and inj > 0,
            "sample_pts": [e["pt"] for e in moves[:3]],
        })
        return False


def main() -> int:
    if not hasattr(FlagRecorder, "results"):
        FlagRecorder.results = []  # type: ignore[attr-defined]

    origin = _cursor()
    print(f"[probe] cursor origin = {origin}")
    out: dict = {"origin": list(origin), "paths": [], "adapter": None}

    with FlagRecorder() as rec:
        rec.results = []  # type: ignore[attr-defined]
        if not rec.ready:
            print(json.dumps({"error": rec.error}))
            return 1
        print("[probe] hook installed OK (installed+pumped on same thread)")

        with rec.window("user32.SetCursorPos"):
            for i in range(1, 6):
                user32.SetCursorPos(origin[0] + i * 12, origin[1] + i * 7)
                time.sleep(0.03)

        user32.SetCursorPos(*origin)
        time.sleep(0.1)

        with rec.window("user32.SendInput(MOUSEEVENTF_MOVE)"):
            for i in range(1, 6):
                _send(MOUSEEVENTF_MOVE, 10, 6)
                time.sleep(0.03)

        user32.SetCursorPos(*origin)
        time.sleep(0.1)

        # -- path 3: what UHA actually calls (real Agent-TARS service) ----------
        adapter_err = None
        try:
            from src.adapters.computer_use import ComputerUseAdapter
            from src.safety.controller import get_safety_controller

            sc = get_safety_controller(hotkey="ctrl+alt+f12")
            sc.set_banner_available(True)   # probe declares a banner so it may inject
            cu = ComputerUseAdapter(base_url="http://127.0.0.1:8788", client_id="p01-attrib-probe", timeout=30)
            cu.connect(required=True)
            sc.request_control(task="p01_attribution_probe")
            sc.banner_visible(ok=True)
            with rec.window("Agent-TARS computer_move_mouse"):
                cu.acquire_control(wait=True)
                try:
                    for i in range(1, 6):
                        cu.move_mouse(origin[0] + i * 40, origin[1] + i * 25)
                        time.sleep(0.05)
                finally:
                    cu.release_control(force=True)
                    cu.close()
            out["safety_after"] = {
                "state": sc.state.value,
                "allow_input": sc.allow_input,
                "human_override": sc.snapshot().human_override,
            }
        except Exception as exc:  # noqa: BLE001
            adapter_err = f"{type(exc).__name__}: {exc}"

        out["paths"] = rec.results
        out["adapter"] = adapter_err

    user32.SetCursorPos(*origin)
    out["restored"] = list(_cursor()) == list(origin)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
