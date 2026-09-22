"""Pure-Win32 safety banner (ctypes only, no tkinter).

Why this exists
---------------
The tkinter banner cannot be used by the interpreter UHA actually runs on: the project
``.venv`` (and the managed 3.13) ship without ``_tkinter``. Only the system Python 3.12
has it. So in the real deployment ``SafetyBanner.available`` was False — and because
``grant_control`` only checked a boolean the agent wrote itself, "banner before control"
was guaranteed on paper and absent in practice.

This backend needs nothing but ``user32``/``gdi32``.

Input-safety shape (P0.1 §3/§12)
--------------------------------
Two windows, deliberately:

  * **strip**   — the status text. ``WS_EX_LAYERED | WS_EX_TRANSPARENT``: fully
    click-through. It is physically incapable of eating the user's clicks.
  * **buttons** — a small window (~360x44) at the right end of the strip holding
    ``Resume Computer Use`` and ``STOP``. A clickable control has to be hit-testable,
    so its footprint is kept small and it only exists while a terminal state is active.

Neither window activates, takes focus, or uses a low-level hook. Neither blocks input.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
import uuid
from typing import Any

from .controller import SafetyController

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_longlong]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p,
]
user32.RegisterClassW.restype = wt.ATOM
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
user32.UnregisterClassW.restype = wt.BOOL
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DrawTextW.restype = ctypes.c_int
user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wt.UINT]
user32.BeginPaint.restype = wt.HDC
user32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.EndPaint.restype = wt.BOOL
user32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.FillRect.restype = ctypes.c_int
user32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HBRUSH]
user32.GetClientRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wt.DWORD]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.SetTextColor.restype = wt.COLORREF
gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
kernel32.GetModuleHandleW.restype = wt.HMODULE

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020

WM_PAINT = 0x000F
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
LWA_ALPHA = 0x00000002
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0040, 0x0010
HWND_TOPMOST = -1
DT_LEFT = 0x00000000
DT_WORDBREAK = 0x00000010
DT_VCENTER = 0x00000004
DT_CENTER = 0x00000001
TRANSPARENT_BK = 1

_PALETTE = {
    "active": ((0x7F, 0x1D, 0x1D), (0xFF, 0xF7, 0xED)),
    "waiting": ((0x92, 0x40, 0x0E), (0xFF, 0xFB, 0xEB)),
    "emergency": ((0x45, 0x0A, 0x0A), (0xFE, 0xE2, 0xE2)),
    "human_override": ((0x1E, 0x3A, 0x5F), (0xE0, 0xF2, 0xFE)),
    "finished": ((0x14, 0x53, 0x2D), (0xDC, 0xFC, 0xE7)),
}


def _rgb(rgb: tuple[int, int, int]) -> int:
    return rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p),   # ctypes.wintypes has no HCURSOR
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", RECT),
        ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_ubyte * 32),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)


def _parse_geometry(spec: str) -> tuple[int, int, int, int]:
    try:
        size, x, y = str(spec).split("+")[0], *[int(v) for v in str(spec).split("+")[1:3]]
        w, h = (int(v) for v in size.split("x"))
        return w, h, x, y
    except Exception:
        return 1280, 84, 40, 8


class Win32SafetyBanner:
    """Same public surface as ``SafetyBanner`` (available/start/stop/visible)."""

    backend = "win32_ctypes"

    def __init__(self, controller: SafetyController, *, geometry: str | None = None):
        self.controller = controller
        controller.set_banner_probe(lambda: self.visible)
        self.geometry = geometry or "1280x84+40+8"
        self.available = True
        self._thread: threading.Thread | None = None
        self._alive = False
        self._visible = False
        self._strip: Any = None
        self._buttons: Any = None
        self._resume_rect = RECT()
        self._stop_rect = RECT()
        self._buttons_shown = False
        self._last_state = ""
        self._wndprocs: list[Any] = []
        self.cleanup_results = []

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="uha-win32-banner", daemon=True)
        self._thread.start()
        for _ in range(40):
            if self.visible:
                return True
            time.sleep(0.05)
        return self.visible

    def stop(self) -> None:
        self._alive = False
        for hwnd in (self._buttons, self._strip):
            if hwnd:
                try:
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE; destroy on owner thread
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=2.0)
            if not self._thread.is_alive():self._thread = None
        self._visible = False

    @property
    def visible(self) -> bool:
        user32.IsWindowVisible.argtypes = [wt.HWND]
        user32.IsWindowVisible.restype = wt.BOOL
        return bool(self._visible and self._strip and self._thread and self._thread.is_alive() and user32.IsWindowVisible(self._strip))

    # -- state mapping -------------------------------------------------------

    def _state_and_text(self) -> tuple[str, str, str]:
        snap = self.controller.snapshot()
        try:
            self.controller.check_heartbeat()
            snap = self.controller.snapshot()
        except Exception:
            pass
        task = snap.task or "-"
        if snap.state.value == "HUMAN_OVERRIDE":
            state = "human_override"
        elif snap.state.value == "EMERGENCY_STOP":
            state = "emergency"
        elif snap.allow_input and snap.agent_injection_permission:
            state = "active"
        elif snap.banner_visible and snap.lifecycle.value in (
            "CONTROL_ACQUIRED", "ACTIVE", "REQUESTED", "BANNER_VISIBLE",
        ):
            state = "waiting"
        else:
            state = "finished"

        if state == "active":
            text = (
                "⚠ UHA COMPUTER USE — Agent is injecting mouse/keyboard input.\n"
                "Move your mouse or press a key to take over.\n"
                f"Ctrl + Alt + F12 — EMERGENCY STOP | task: {task}"
            )
        elif state == "waiting":
            text = (
                "⚠ UHA COMPUTER USE SESSION — Agent currently waiting.\n"
                "Move mouse / press key to take over.\n"
                "Ctrl + Alt + F12 — EMERGENCY STOP"
            )
        elif state == "emergency":
            text = (
                "⛔ EMERGENCY STOPPED — Agent injection DISABLED.\n"
                "User always keeps physical control. Resume manually when ready."
            )
        elif state == "human_override":
            text = (
                "✋ HUMAN OVERRIDE — Agent input paused.\n"
                "You are in control. Resume manually when ready."
            )
        else:
            text = "✓ COMPUTER USE FINISHED — agent injection permission released.\nPhysical input was never blocked."
        return state, text, task

    # -- window procs --------------------------------------------------------

    def _strip_paint(self, hwnd) -> None:
        state, text, _ = self._state_and_text()
        bg, fg = _PALETTE.get(state, _PALETTE["active"])
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rect = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        brush = gdi32.CreateSolidBrush(_rgb(bg))
        user32.FillRect(hdc, ctypes.byref(rect), brush)
        gdi32.DeleteObject(brush)
        gdi32.SetBkMode(hdc, TRANSPARENT_BK)
        gdi32.SetTextColor(hdc, _rgb(fg))
        inner = RECT(rect.left + 14, rect.top + 8, rect.right - 14, rect.bottom - 8)
        user32.DrawTextW(hdc, text, -1, ctypes.byref(inner), DT_LEFT | DT_WORDBREAK | DT_VCENTER)
        user32.EndPaint(hwnd, ctypes.byref(ps))

    def _strip_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            try:
                self._strip_paint(hwnd)
            except Exception:
                pass
            return 0
        if msg == WM_TIMER:
            state, _, _ = self._state_and_text()
            if state != self._last_state:
                self._last_state = state
                self._sync_buttons(state)
            user32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _buttons_paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rect = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        bg = gdi32.CreateSolidBrush(_rgb((0x1F, 0x29, 0x37)))
        user32.FillRect(hdc, ctypes.byref(rect), bg)
        gdi32.DeleteObject(bg)
        gdi32.SetBkMode(hdc, TRANSPARENT_BK)
        for r, label, colour in (
            (self._resume_rect, "Resume Computer Use", (0x14, 0x53, 0x2D)),
            (self._stop_rect, "STOP (secondary)", (0x7F, 0x1D, 0x1D)),
        ):
            br = gdi32.CreateSolidBrush(_rgb(colour))
            user32.FillRect(hdc, ctypes.byref(r), br)
            gdi32.DeleteObject(br)
            gdi32.SetTextColor(hdc, _rgb((0xFF, 0xFF, 0xFF)))
            user32.DrawTextW(hdc, label, -1, ctypes.byref(r), DT_CENTER | DT_VCENTER)
        user32.EndPaint(hwnd, ctypes.byref(ps))

    def _buttons_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            try:
                self._buttons_paint(hwnd)
            except Exception:
                pass
            return 0
        if msg == WM_LBUTTONUP:
            x = ctypes.c_short(lparam & 0xFFFF).value
            y = ctypes.c_short((lparam >> 16) & 0xFFFF).value

            def inside(r: RECT) -> bool:
                return r.left <= x < r.right and r.top <= y < r.bottom

            try:
                if inside(self._stop_rect):
                    self.controller.emergency_stop(reason="banner_stop_button", source="win32_banner")
                elif inside(self._resume_rect):
                    self.controller.resume_safety(explicit=True)
            except Exception:
                pass
            user32.InvalidateRect(hwnd, None, True)
            return 0
        if msg == WM_TIMER:
            user32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _sync_buttons(self, state: str) -> None:
        terminal = state in ("emergency", "human_override")
        if terminal and not self._buttons_shown and self._buttons:
            user32.ShowWindow(self._buttons, 5)          # SW_SHOW
            user32.SetWindowPos(self._buttons, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._buttons_shown = True
        elif not terminal and self._buttons_shown and self._buttons:
            user32.ShowWindow(self._buttons, 0)          # SW_HIDE
            self._buttons_shown = False

    # -- banner thread -------------------------------------------------------

    def _run(self) -> None:
        w, h, x, y = _parse_geometry(self.geometry)
        btn_w, btn_h = 366, 42
        hinst = kernel32.GetModuleHandleW(None)

        strip_proc = WNDPROC(self._strip_proc)
        btn_proc = WNDPROC(self._buttons_proc)
        self._wndprocs = [strip_proc, btn_proc]

        # A registered class owns a raw callback pointer. Reusing the old class
        # after its Python callback was collected caused repeated-session crashes.
        suffix = uuid.uuid4().hex
        strip_class, button_class = 'UHA_Strip_'+suffix, 'UHA_Buttons_'+suffix
        for cls_name, proc in ((strip_class, strip_proc), (button_class, btn_proc)):
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.hbrBackground = None  # WM_PAINT supplies and frees its own brushes
            wc.lpszClassName = cls_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                self.cleanup_results.append({'class':cls_name,'registration_failed':True})
                return

        self._strip = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            strip_class, "UHA SAFETY",
            WS_POPUP | WS_VISIBLE, x, y, w, h, None, None, hinst, None,
        )
        if not self._strip:
            for name in (strip_class,button_class):user32.UnregisterClassW(name,hinst)
            return
        try:
            user32.SetLayeredWindowAttributes(self._strip, 0, 238, LWA_ALPHA)
        except Exception:
            pass
        user32.SetWindowPos(self._strip, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

        # clickable controls: small, docked to the right, hidden unless a terminal state
        bx, by = x + w - btn_w - 4, y + h + 2
        self._buttons = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE,
            button_class, "UHA SAFETY CONTROLS",
            WS_POPUP, bx, by, btn_w, btn_h, None, None, hinst, None,
        )
        self._resume_rect = RECT(6, 6, 214, btn_h - 6)
        self._stop_rect = RECT(222, 6, btn_w - 6, btn_h - 6)
        if self._buttons:
            try:
                user32.SetLayeredWindowAttributes(self._buttons, 0, 240, LWA_ALPHA)
            except Exception:
                pass

        user32.SetTimer(self._strip, 1, 400, None)
        if self._buttons:
            user32.SetTimer(self._buttons, 2, 400, None)

        self._visible = True
        self.controller.banner_visible(ok=True)

        msg = wt.MSG()
        while self._alive and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._visible = False
        for hwnd in (self._buttons,self._strip):
            if hwnd:user32.DestroyWindow(hwnd)
        self._buttons=self._strip=None
        for name in (strip_class,button_class):
            self.cleanup_results.append({'class':name,'unregistered':bool(user32.UnregisterClassW(name,hinst))})


__all__ = ["Win32SafetyBanner"]
