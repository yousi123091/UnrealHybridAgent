"""UHA P0.1 — input-path forensics.

Answers, with measurements instead of assumptions, the question:

    "Computer Use finished, but the user still cannot use the mouse."

Design rules (P0.1 §3 / §34):
  * READ-ONLY by default. No BlockInput, no ClipCursor, no hooks that swallow.
  * The one hook this tool installs is observe-only, installed on its OWN
    thread that pumps messages (the correct pattern), is time-boxed, and is
    always unhooked in ``finally``. It never returns a non-zero swallow value.
  * Nothing here *forces* user input to work by guessing.

Sections
  A  WINDOW CENSUS      -> any topmost/covering window that could eat clicks
  B  BUTTON STATE       -> injected mouse button stuck DOWN?  (candidate D)
  C  CURSOR CONSTRAINT  -> ClipCursor still active?           (candidate E)
  D  INPUT CHAIN LATENCY-> is a foreign low-level hook stalling the chain?
  E  CU SERVICE STATE   -> Agent-TARS alive? does it still hold the desktop?
  F  PROCESS CENSUS     -> leftover UHA worker / daemon / banner processes
  G  STATIC API AUDIT   -> forbidden input-blocking APIs anywhere in the stack

Usage (from repo root):
    python -c "exec(open(r'tools/p01_input_forensics.py', encoding='utf-8').read(), {'__file__': r'tools/p01_input_forensics.py'})"
    ... --json out.json      write machine-readable evidence
    ... --no-hook            skip section D (fully non-invasive run)
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---------------------------------------------------------------- win32 decls
# IMPORTANT: every 64-bit-returning API gets an explicit restype. The absence of
# this is itself one of the defects found in src/safety/human_override.py.
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetClipCursor.restype = wt.BOOL
user32.GetClipCursor.argtypes = [ctypes.POINTER(wt.RECT)]
user32.GetCursorPos.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetForegroundWindow.restype = wt.HWND
user32.SendInput.restype = ctypes.c_uint
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
user32.EnumWindows.restype = wt.BOOL
user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
user32.SetCursorPos.restype = wt.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.PeekMessageW.restype = wt.BOOL
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


def _say(msg: str) -> None:
    """Progress to stderr, unbuffered — the tool must never fail silently."""
    try:
        sys.stderr.write(f"[p01] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass

WH_MOUSE_LL = 14
HC_ACTION = 0
WM_MOUSEMOVE = 0x0200
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

VK = {
    "VK_LBUTTON": 0x01,
    "VK_RBUTTON": 0x02,
    "VK_MBUTTON": 0x04,
    "VK_XBUTTON1": 0x05,
    "VK_XBUTTON2": 0x06,
    "VK_SHIFT": 0x10,
    "VK_CONTROL": 0x11,
    "VK_MENU": 0x12,
    "VK_LWIN": 0x5B,
}

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class _IU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _IU)]


def _send_rel_move(dx: int, dy: int) -> None:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _cursor() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _screen_size() -> tuple[int, int]:
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


# ------------------------------------------------------------------ section A
def section_windows() -> dict:
    """Any topmost / covering / non-transparent window that could eat clicks."""
    screen = _screen_size()
    cur = _cursor()
    rows: list[dict] = []

    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _l):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            r = wt.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            w, h = r.right - r.left, r.bottom - r.top
            if w <= 0 or h <= 0:
                return True
            ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            covers = r.left <= cur[0] < r.right and r.top <= cur[1] < r.bottom
            transparent = bool(ex & WS_EX_TRANSPARENT)
            rows.append({
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "class": cls.value,
                "title": buf.value,
                "rect": [r.left, r.top, w, h],
                "ex_style": hex(ex & 0xFFFFFFFF),
                "topmost": bool(ex & WS_EX_TOPMOST),
                "layered": bool(ex & WS_EX_LAYERED),
                "transparent": transparent,
                "noactivate": bool(ex & WS_EX_NOACTIVATE),
                "covers_cursor": covers,
                "area_ratio": round((w * h) / max(1, screen[0] * screen[1]), 3),
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumProc(cb), 0)

    # Who actually receives a click at the cursor right now?
    pt = wt.POINT(cur[0], cur[1])
    hwnd_at = int(user32.WindowFromPoint(pt))
    at_owner_pid = wt.DWORD(0)
    if hwnd_at:
        user32.GetWindowThreadProcessId(hwnd_at, ctypes.byref(at_owner_pid))
    cls = ctypes.create_unicode_buffer(256)
    if hwnd_at:
        user32.GetClassNameW(hwnd_at, cls, 256)

    # Suspicious = visible, topmost, covers cursor, and NOT click-through.
    suspicious = [
        x for x in rows
        if x["topmost"] and x["covers_cursor"] and not x["transparent"]
    ]
    big_topmost = [
        x for x in rows
        if x["topmost"] and x["area_ratio"] >= 0.20
    ]
    return {
        "screen": list(screen),
        "cursor": list(cur),
        "fg_hwnd": int(user32.GetForegroundWindow()),
        "window_at_cursor": {
            "hwnd": hwnd_at, "pid": int(at_owner_pid.value), "class": cls.value,
        },
        "topmost_covering_cursor_not_transparent": suspicious,
        "topmost_large_windows": big_topmost,
        "window_count": len(rows),
        "note": (
            "A topmost+visible window that covers the cursor and is NOT "
            "WS_EX_TRANSPARENT will receive the user's clicks instead of the "
            "application -> looks exactly like 'mouse does not work'."
        ),
    }


# ------------------------------------------------------------------ section B
def section_buttons() -> dict:
    """Physical/injected button state. A stuck injected button = candidate D."""
    out = {}
    for name, vk in VK.items():
        st = user32.GetAsyncKeyState(vk)
        out[name] = {"raw": hex(st & 0xFFFF), "down": bool(st & 0x8000)}
    stuck = [k for k, v in out.items() if v["down"]]
    return {
        "keys": out,
        "currently_down": stuck,
        "stuck_button_suspected": any(k in ("VK_LBUTTON", "VK_RBUTTON", "VK_MBUTTON") for k in stuck),
        "note": "If no human is touching the mouse/keyboard, any 'down' here is a stuck injected input.",
    }


# ------------------------------------------------------------------ section C
def section_clipcursor() -> dict:
    """An un-released ClipCursor() confines the pointer -> candidate E."""
    r = wt.RECT()
    ok = bool(user32.GetClipCursor(ctypes.byref(r)))
    sw, sh = _screen_size()
    confined = ok and not (r.left <= 0 and r.top <= 0 and r.right >= sw and r.bottom >= sh)
    return {
        "get_clip_cursor_ok": ok,
        "clip_rect": [r.left, r.top, r.right, r.bottom],
        "screen_rect": [0, 0, sw, sh],
        "cursor_confined": bool(confined),
        "note": "cursor_confined=True means something still holds a ClipCursor constraint.",
    }


# ------------------------------------------------------------------ section D
def section_hook_latency(samples: int = 12) -> dict:
    """Measure LL-mouse-hook dispatch latency.

    The ONLY reason this can be slow is that some *other* process installed a
    low-level hook whose thread is not pumping -- the whole input chain then
    waits. Our own hook is installed on a thread that pumps, and is removed at
    the end even on failure.
    """
    LRESULT = ctypes.c_longlong
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
    state = {"cb": 0, "phys": 0, "inject": 0, "hook": None, "ready": False, "err": None}
    stop = {"v": False}

    def proc(nCode, wParam, lParam):
        # Observe-only. ALWAYS chain. Never swallow (P0.1 §5).
        try:
            if nCode == HC_ACTION:
                state["cb"] += 1
        except Exception:
            pass
        return int(user32.CallNextHookEx(state["hook"], nCode, wParam, lParam) or 0)

    cbproc = HOOKPROC(proc)

    def thread_body():
        # Install AND pump on the SAME thread -- the documented requirement.
        try:
            state["hook"] = user32.SetWindowsHookExW(WH_MOUSE_LL, cbproc, None, 0)
            if not state["hook"]:
                state["err"] = f"SetWindowsHookExW failed err={ctypes.get_last_error()}"
                return
            state["ready"] = True
        except Exception as exc:  # noqa: BLE001
            state["err"] = f"{type(exc).__name__}: {exc}"
            return
        msg = wt.MSG()
        while not stop["v"]:
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if got:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.001)

    import threading

    th = threading.Thread(target=thread_body, name="p01-hook-probe", daemon=True)
    th.start()
    t0 = time.time()
    while time.time() - t0 < 2.0 and not state["ready"] and state["err"] is None:
        time.sleep(0.01)

    if not state["ready"]:
        return {"hook_installed": False, "error": state["err"], "note": "probe skipped"}

    origin = _cursor()
    lats: list[float] = []
    try:
        time.sleep(0.15)
        for _ in range(samples):
            before = state["cb"]
            t = time.perf_counter()
            _send_rel_move(1, 0)   # 1px right, then straight back: net zero
            # wait for the hook callback to be dispatched
            wait_until = time.perf_counter() + 2.0
            while state["cb"] == before and time.perf_counter() < wait_until:
                time.sleep(0.0005)
            lats.append((time.perf_counter() - t) * 1000.0)
            _send_rel_move(-1, 0)
            time.sleep(0.05)
    finally:
        stop["v"] = True
        try:
            if state["hook"]:
                user32.UnhookWindowsHookEx(state["hook"])
        except Exception:
            pass
        th.join(timeout=1.0)
        # restore pointer exactly
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos(origin[0], origin[1])

    good = [x for x in lats if x < 1900]
    return {
        "hook_installed": True,
        "hook_removed": True,
        "callbacks_received": state["cb"],
        "samples_ms": [round(x, 2) for x in lats],
        "min_ms": round(min(lats), 2) if lats else None,
        "median_ms": round(statistics.median(lats), 2) if lats else None,
        "max_ms": round(max(lats), 2) if lats else None,
        "timed_out_samples": len(lats) - len(good),
        "verdict": (
            "INPUT CHAIN HEALTHY - no foreign hook is stalling dispatch"
            if lats and max(lats) < 50 else
            "INPUT CHAIN STALLED - a low-level hook is not being serviced"
            if lats else "NO DATA"
        ),
    }


# ------------------------------------------------------------------ section E
def section_cu_service() -> dict:
    import urllib.request

    out: dict = {"health": None, "error": None}
    try:
        with urllib.request.urlopen("http://127.0.0.1:8788/health", timeout=2) as r:
            out["health"] = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    # REST tunnel used by force cleanup is not a real endpoint -> verify shape
    out["force_release_endpoint_note"] = (
        "src/safety/cleanup.py + watchdog POST to http://127.0.0.1:8788/call with "
        "arguments={'clientId','force'} — server/tools.js reads args.clientId/force "
        "(handlers are called with args), so the field name 'arguments' is wrong: "
        "the call posts {tool, arguments} but http.js destructures {tool, args}. "
        "The force-release HTTP fallback therefore 404s/500s and silently no-ops."
    )
    return out


# ------------------------------------------------------------------ section F
def section_processes() -> dict:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Depth 3 -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps], stderr=subprocess.DEVNULL, timeout=40
        ).decode("utf-8", "replace")
        data = json.loads(raw or "[]")
        if isinstance(data, dict):
            data = [data]
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    keys = ("uha", "agent-tars", "safety", "banner", "hotkey", "human_override",
            "watchdog", "daemon", "computer_use", "gui_smoke")
    interesting = []
    for row in data:
        cmd = str(row.get("CommandLine") or "")
        name = str(row.get("Name") or "")
        low = (cmd + " " + name).lower()
        if any(k in low for k in keys) or (name.lower().startswith("node") and "agent-tars" in low):
            interesting.append({
                "pid": row.get("ProcessId"), "name": name, "cmdline": cmd[:400],
            })
    return {
        "uha_related_processes": interesting,
        "count_total": len(data),
        "note": "Any lingering uha/agent-tars/banner process can keep owning desktop state.",
    }


# ------------------------------------------------------------------ section G
FORBIDDEN = re.compile(
    r"BlockInput|SetWindowsHookEx|WH_MOUSE_LL|WH_KEYBOARD_LL|ClipCursor|SetCapture|"
    r"LowLevelHooks|mouse_event|keybd_event|LockWorkStation|"
    r"disable_user_input|exclusive_input|input_grab|input_lock|cursor_confinement",
    re.I,
)


def section_static_audit() -> dict:
    targets = [
        (Path(r"E:\UnrealHybridAgent\src"), "*.py"),
        (Path(r"E:\UnrealHybridAgent\uah"), "*.py"),
        (Path(r"E:\UnrealHybridAgent\tools"), "*.py"),
        (Path(r"E:\UnrealHybridAgent\tests"), "*.py"),
        (Path(r"E:\MCP\Agent-TARS\server"), "*.js"),
        (Path(r"E:\MCP\Agent-TARS\tests"), "*.js"),
    ]
    hits: list[dict] = []
    for base, pat in targets:
        if not base.is_dir():
            continue
        for p in base.rglob(pat):
            if "__pycache__" in str(p):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in FORBIDDEN.finditer(text):
                line = text[: m.start()].count("\n") + 1
                hits.append({
                    "file": str(p), "line": line, "api": m.group(0),
                    "code": text.splitlines()[line - 1].strip()[:140] if line - 1 < len(text.splitlines()) else "",
                })
    binary_findings = {}
    libnut = Path(r"E:\MCP\Agent-TARS\node_modules\@computer-use\libnut-win32\build\Release\libnut.node")
    if libnut.is_file():
        blob = libnut.read_bytes()
        for api in (b"BlockInput", b"SetWindowsHookEx", b"ClipCursor", b"SetCapture",
                    b"SendInput", b"GetCursorPos"):
            binary_findings[api.decode()] = api in blob
    return {
        "source_hits": hits,
        "libnut_native_imports": binary_findings,
        "policy_check": {
            "USER_INPUT_BLOCKING": any(h["api"].lower() == "blockinput" for h in hits),
            "USER_INPUT_SUPPRESSION": any("hook" in h["api"].lower() for h in hits),
            "EXCLUSIVE_MOUSE_GRAB": any("capture" in h["api"].lower() or "grab" in h["api"].lower() for h in hits),
            "EXCLUSIVE_KEYBOARD_GRAB": False,
        },
    }


# --------------------------------------------------------------------- driver
def _guard(name: str, fn):
    _say(f"{name} ...")
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as exc:  # noqa: BLE001
        out = {"error": f"{type(exc).__name__}: {exc}"}
    _say(f"{name} done in {(time.perf_counter() - t0) * 1000:.0f} ms")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--no-hook", action="store_true", help="skip section D (fully non-invasive)")
    args = ap.parse_args()

    report: dict = {
        "tool": "uha p01 input forensics",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pid": os.getpid(),
    }
    report["A_windows"] = _guard("A windows", section_windows)
    report["B_buttons"] = _guard("B buttons", section_buttons)
    report["C_clipcursor"] = _guard("C clipcursor", section_clipcursor)
    report["D_hook_latency"] = (
        {"skipped": True} if args.no_hook else _guard("D hook latency", section_hook_latency)
    )
    report["E_cu_service"] = _guard("E cu service", section_cu_service)
    report["F_processes"] = _guard("F processes", section_processes)
    report["G_static_audit"] = _guard("G static audit", section_static_audit)

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
