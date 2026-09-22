"""P0.1 Human Override detector — physical vs injected input, flag-based.

Design rules (UHA P0.1 §3 / §5 / §34)
-------------------------------------
* **Human physical input always reaches the OS.** Nothing here returns early, swallows,
  consumes or suppresses an event. Every hook callback ends in
  ``CallNextHookEx(hhk, nCode, wParam, lParam)`` with the arguments it was given.
* **The injected flag is the ground truth**, not a cursor-delta heuristic.
  ``LLMHF_INJECTED`` / ``LLKHF_INJECTED`` are set by ``SendInput``; a low-level event with
  those bits clear is *genuinely* human. Measured on this machine
  (``tools/p01_injection_attribution_probe.py``):
    - ``SendInput(MOUSEEVENTF_MOVE)`` -> 5/5 events flagged injected
    - Agent-TARS / nut-js ``computer_move_mouse`` -> **0 low-level events at all**
      (it drives the pointer with ``SetCursorPos``, which bypasses the hook chain)
  Both facts are safe for us: agent motion can never masquerade as human.
* **Hooks are observe-only and correctly parked.** They are installed *and* pumped on one
  dedicated thread (Windows dispatches low-level hooks on the installing thread and
  requires that thread to pump). ``restype`` is set, because a truncated 64-bit ``HHOOK``
  makes ``UnhookWindowsHookEx`` fail. ``stop()`` really unhooks — and it is called from
  ``force_cleanup_input`` and from process teardown.
* If hooks cannot be installed we degrade to polling and say so (``hook_mode``), applying a
  short suppression window around our own injections so the agent never cancels itself.
  The degraded mode is *less* precise and is reported as such in diagnostics.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)

# -- win32 decls: restype/argtypes are mandatory on 64-bit Windows -----------------
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.GetCursorPos.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.PeekMessageW.restype = wt.BOOL

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002
LLMHF_INJECTED = 0x00000001
LLMHF_LOWER_IL_INJECTED = 0x00000002
HC_ACTION = 0

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
_WHEEL = {WM_MOUSEWHEEL, WM_MOUSEHWHEEL}
_BUTTON_DOWN = {WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN}
_BUTTON_UP = {WM_LBUTTONUP, WM_RBUTTONUP, WM_MBUTTONUP}

LRESULT = ctypes.c_longlong
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# polling fallback: a few keys worth watching for a human takeover
_POLL_KEYS = (("ctrl", 0x11), ("alt", 0x12), ("shift", 0x10), ("space", 0x20))


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


class HumanOverrideDetector:
    """Flag-based physical-input observer. Never consumes input."""

    def __init__(
        self,
        *,
        on_physical_input: Callable[[str], None] | None = None,
        poll_hz: float = 30.0,
        move_throttle_s: float = 0.05,
        inject_grace_s: float = 0.35,
    ):
        self.on_physical_input = on_physical_input
        self.poll_hz = float(poll_hz)
        self.move_throttle_s = float(move_throttle_s)
        self.inject_grace_s = float(inject_grace_s)

        self.physical_events = 0
        self.injected_events = 0
        self.human_override_count = 0
        self.last_physical_kind = ""
        self.last_physical_at = 0.0

        self._lock = threading.RLock()
        self._active = False
        self._hook_mode = "stopped"
        self._mouse_hook = None
        self._kb_hook = None
        self._mouse_cb = None
        self._kb_cb = None
        self._hook_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._last_cursor = (0, 0)
        self._last_move_cb_at = 0.0
        self._pump_at = 0.0
        self.cleanup_results: list[dict] = []
        self._hook_fault = ''
        self._refresh_at = 0.0
        self._retired_hooks = []
        self.refresh_count = 0

        # injection attribution (used by the polling fallback only)
        self._inject_depth = 0
        self._inject_ended_at = 0.0

    # -- attribution ----------------------------------------------------------

    def begin_agent_injection(self) -> None:
        with self._lock:
            self._inject_depth += 1

    def end_agent_injection(self) -> None:
        with self._lock:
            self._inject_depth = max(0, self._inject_depth - 1)
            self._inject_ended_at = time.time()

    def agent_injecting(self) -> bool:
        with self._lock:
            return self._inject_depth > 0

    def _recent_injection(self) -> bool:
        with self._lock:
            return self._inject_depth > 0 or (time.time() - self._inject_ended_at) < self.inject_grace_s

    # -- state ----------------------------------------------------------------

    @property
    def hook_mode(self) -> str:
        return self._hook_mode

    @property
    def hook_alive(self) -> bool:
        return bool(self._active and not self._hook_fault and self._mouse_hook and self._kb_hook and self._hook_thread
                    and self._hook_thread.is_alive() and time.monotonic()-self._pump_at < .5)

    def _cursor(self) -> tuple[int, int]:
        pt = wt.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
        return self._last_cursor

    def _handle_physical(self, kind: str) -> None:
        with self._lock:
            self.physical_events += 1
            self.last_physical_kind = kind
            self.last_physical_at = time.time()
        if self.on_physical_input:
            try:
                self.on_physical_input(kind)
            except Exception:
                pass

    # -- hook callbacks (must be tiny; must always chain) ----------------------

    def _mouse_proc(self, nCode, wParam, lParam):
        try:
            if nCode == HC_ACTION and lParam:
                info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
                    self.injected_events += 1
                elif wParam == WM_MOUSEMOVE:
                    now = time.time()
                    if now - self._last_move_cb_at >= self.move_throttle_s:
                        self._last_move_cb_at = now
                        self._handle_physical("mouse_move")
                elif wParam in _BUTTON_DOWN:
                    self._handle_physical("mouse_button_down")
                elif wParam in _WHEEL:
                    self._handle_physical("mouse_wheel")
                elif wParam in _BUTTON_UP:
                    self._handle_physical("mouse_button_up")
        except Exception:
            pass
        return int(user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam) or 0)

    def _kb_proc(self, nCode, wParam, lParam):
        try:
            if nCode == HC_ACTION and lParam:
                info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if info.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED):
                    self.injected_events += 1
                elif wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self._handle_physical("keyboard")
        except Exception:
            pass
        return int(user32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam) or 0)

    def _hook_thread_body(self) -> None:
        """Install AND pump on this one thread — the documented Windows requirement."""
        try:
            self._mouse_cb = HOOKPROC(self._mouse_proc)
            self._kb_cb = HOOKPROC(self._kb_proc)
            self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_cb, None, 0)
            self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, None, 0)
        except Exception:
            self._mouse_hook = self._kb_hook = None
        if not self._mouse_hook and not self._kb_hook:
            self._hook_mode = "polling_fallback"
            return
        self._pump_at = time.monotonic()
        self._refresh_at = self._pump_at
        self._hook_mode = "ll_flag_based"
        msg = wt.MSG()
        while self._active:
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            self._pump_at = time.monotonic()
            if self._pump_at-self._refresh_at >= .2:
                self._refresh_hooks()
            if got:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.001)

    def _refresh_hooks(self) -> None:
        """Renew on the owning pump thread, checking that the old hooks existed.

        Windows can silently remove a timed-out hook while its Python handle and
        thread remain alive. Install the replacement first to avoid an intentional
        observation gap; a failed old unhook latches unhealthy, never auto-resumes.
        This is bounded health detection, not a zero-latency input guarantee.
        """
        with self._lock:
            if not self._active or self._hook_fault:return
            self._refresh_at=time.monotonic()
            for name,hook_type,callback in (('_mouse_hook',WH_MOUSE_LL,self._mouse_cb),('_kb_hook',WH_KEYBOARD_LL,self._kb_cb)):
                old=getattr(self,name)
                fresh=user32.SetWindowsHookExW(hook_type,callback,None,0)
                if not fresh:
                    self._hook_fault='replacement_install_failed:'+name
                    return
                setattr(self,name,fresh)
                ctypes.set_last_error(0)
                if not user32.UnhookWindowsHookEx(old):
                    error=ctypes.get_last_error()
                    self._hook_fault=f'previous_hook_missing_or_unhook_failed:{name}:{error}'
                    self._retired_hooks.append(old)
                    return
            self.refresh_count+=1

    # -- polling fallback ------------------------------------------------------

    def _poll_loop(self) -> None:
        """Degraded mode only. Suppressed while we are (or just were) injecting."""
        interval = 1.0 / max(self.poll_hz, 1.0)
        self._last_cursor = self._cursor()
        while self._active:
            try:
                cur = self._cursor()
                if cur != self._last_cursor:
                    if not self._recent_injection():
                        self._handle_physical("mouse_move")
                    self._last_cursor = cur
                for name, vk in _POLL_KEYS:
                    if user32.GetAsyncKeyState(vk) & 0x0001:
                        self._handle_physical(f"key:{name}")
            except Exception:
                pass
            time.sleep(interval)

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        with self._lock:
            if self._active:
                return True
            if self._mouse_hook or self._kb_hook:
                raise RuntimeError('Previous native hooks did not cleanly unhook')
            self.cleanup_results = []
            self._hook_fault = ''
            self._active = True
        self._hook_thread = threading.Thread(
            target=self._hook_thread_body, name="uha-hook-install-pump", daemon=True
        )
        self._hook_thread.start()
        # give the installer a moment so hook_mode is accurate before we return
        for _ in range(200):
            if self._hook_mode != "stopped":
                break
            time.sleep(0.005)
        if self._hook_mode == "polling_fallback":
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name="uha-human-override-poll", daemon=True
            )
            self._poll_thread.start()
        return True

    def stop(self, *, wait: bool = True) -> None:
        """Idempotent. Unhooks, stops both threads. Safe to call repeatedly."""
        with self._lock:
            if not self._active and self._hook_mode == "stopped":
                return
            self._active = False
        for h, name in ((self._mouse_hook, "_mouse_hook"), (self._kb_hook, "_kb_hook")):
            if not h:
                continue
            try:
                ctypes.set_last_error(0)
                ok = bool(user32.UnhookWindowsHookEx(h))
                error = 0 if ok else ctypes.get_last_error()
                self.cleanup_results.append({'hook':name,'ok':ok,'winerror':error})
            except Exception as exc:
                ok = False
                self.cleanup_results.append({'hook':name,'ok':False,'error':str(exc)})
            if ok:setattr(self, name, None)
        for attr in ("_hook_thread", "_poll_thread"):
            th = getattr(self, attr, None)
            if th is not None:
                if wait and th is not threading.current_thread():th.join(timeout=1.5)
                if not th.is_alive():setattr(self, attr, None)
        for handle in self._retired_hooks:
            ctypes.set_last_error(0)
            ok=bool(user32.UnhookWindowsHookEx(handle))
            self.cleanup_results.append({'hook':'retired','ok':ok,'winerror':0 if ok else ctypes.get_last_error()})
        self._retired_hooks=[]
        pending = bool(self._mouse_hook or self._kb_hook or self._hook_thread or self._poll_thread)
        self._hook_mode = "cleanup_failed" if pending else "stopped"

    def stats(self) -> dict:
        return {
            "hook_mode": self._hook_mode,
            "hook_alive": self.hook_alive,
            "pump_age_ms": round((time.monotonic()-self._pump_at)*1000,3) if self._pump_at else None,
            "unhook_results": list(self.cleanup_results),
            "cleanup_complete": self._hook_mode == 'stopped' and not (self._mouse_hook or self._kb_hook or self._hook_thread or self._poll_thread),
            "hook_fault": self._hook_fault,
            "refresh_count": self.refresh_count,
            "health_refresh_interval_ms": 200,
            "attribution": (
                "injected_flag" if self._hook_mode == "ll_flag_based"
                else "polling_with_injection_suppression"
            ),
            "physical_events": self.physical_events,
            "injected_events": self.injected_events,
            "human_override_count": self.human_override_count,
            "last_physical_kind": self.last_physical_kind,
            "last_physical_at": self.last_physical_at,
            "blocking_hooks": False,
            "hooks_consume_events": False,
            "swallows_user_input": False,
        }


__all__ = ["HumanOverrideDetector"]
