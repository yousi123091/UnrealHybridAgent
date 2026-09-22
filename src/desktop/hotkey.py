"""Emergency Stop：确定性释放键鼠 + 热键注册。

* **不**依赖 LLM / Vision / Router。
* **只释放 UHA 自己按下过的键/鼠标键**（P0.1 §14）。

修订说明（P0.1 §14 / RC-3）
--------------------------
早期实现对所有修饰键 + A–Z + Enter/Esc/Space/Tab + 三个鼠标键无差别发 keyup/buttonup，
共 38 个 UP 事件——**其中绝大多数 UHA 从未按下**。用户此刻若正按住某个键（UE 视口用
Space/右键平移、拖拽选择），这些合成 UP 会直接打断用户自己的操作。那等于往用户的输入
流里注入噪声，比"拥有输入"更糟。

现在改为**记账式释放**：适配器在真正按下键/开始拖拽时登记，释放时只针对登记项，
并且发 UP 之前先用 `GetAsyncKeyState` 确认该键确实处于按下状态。

热键实现优先 **ctypes RegisterHotKey**（零第三方依赖）。
若不可用，退回轮询 GetAsyncKeyState（仍为本地确定性逻辑）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from typing import Callable, Iterable

user32 = ctypes.WinDLL("user32", use_last_error=True)

user32.SendInput.restype = ctypes.c_uint
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
VK_MAP = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "shift": 0x10,
    "win": 0x5B, "meta": 0x5B,
}

# 完整虚拟键表——**仅供人工诊断**（release_everything_now）。
# 自动路径绝不使用它：盲目释放用户正按住的键会打断用户自己的操作。
_RELEASE_VKS = [
    0x10, 0x11, 0x12, 0x5B, 0x5C,  # shift ctrl alt lwin rwin
    0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A,
    0x4B, 0x4C, 0x4D, 0x4E, 0x4F, 0x50, 0x51, 0x52, 0x53, 0x54,
    0x55, 0x56, 0x57, 0x58, 0x59, 0x5A,
    0x0D, 0x1B, 0x20, 0x09,  # enter esc space tab
]

# --- 记账表：只记录 UHA 自己按下的东西（P0.1 §14） ------------------------

NAME_TO_VK: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "leftcontrol": 0x11, "rightcontrol": 0xA3,
    "alt": 0x12, "menu": 0x12, "leftalt": 0x12, "rightalt": 0xA5,
    "shift": 0x10, "leftshift": 0x10, "rightshift": 0xA1,
    "win": 0x5B, "meta": 0x5B, "leftwin": 0x5B, "command": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "arrowup": 0x26, "down": 0x28, "arrowdown": 0x28,
    "left": 0x25, "arrowleft": 0x25, "right": 0x27, "arrowright": 0x27,
    "capslock": 0x14, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
}
NAME_TO_VK.update({chr(0x41 + i).lower(): 0x41 + i for i in range(26)})
NAME_TO_VK.update({chr(0x30 + i): 0x30 + i for i in range(10)})
NAME_TO_VK.update({f"f{i}": 0x70 + (i - 1) for i in range(1, 25)})
NAME_TO_VK.update({f"num{i}": 0x60 + i for i in range(10)})

_BUTTON_VK = {"left": 0x01, "right": 0x02, "middle": 0x04}

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTUP = 0x0008
MOUSEEVENTF_MIDDLEUP = 0x0020
_BUTTON_UP_FLAG = {
    "left": MOUSEEVENTF_LEFTUP,
    "right": MOUSEEVENTF_RIGHTUP,
    "middle": MOUSEEVENTF_MIDDLEUP,
}

_ledger_lock = threading.RLock()
_pressed_keys: set[int] = set()        # VK 值，UHA 按下且尚未释放
_pressed_buttons: set[str] = set()     # UHA 按下且尚未释放（拖拽开始→成功结束）
_unresolved_keys: set[str] = set()     # 名字认不出来，无法跟踪——如实记录


def _resolve_vk(key: str) -> int | None:
    k = str(key or "").strip().lower()
    if not k:
        return None
    if k in NAME_TO_VK:
        return NAME_TO_VK[k]
    if k.startswith("0x"):
        try:
            return int(k, 16) & 0xFFFF
        except ValueError:
            return None
    return None


def resolve_keys(keys: str | Iterable[str]) -> tuple[list[int], list[str]]:
    """`"ctrl a"` / `"ctrl+a"` / `["enter"]` -> ([vk...], [认不出来的名字...])"""
    if isinstance(keys, str):
        parts = [p for p in keys.replace("+", " ").split() if p]
    else:
        parts = [str(p) for p in keys]
    vks: list[int] = []
    unknown: list[str] = []
    for p in parts:
        vk = _resolve_vk(p)
        if vk is None:
            unknown.append(p)
        else:
            vks.append(vk)
    return vks, unknown


def track_key_press(keys: str | Iterable[str]) -> dict[str, list[str]]:
    """适配器在真正发出 key_press 之前调用。"""
    vks, unknown = resolve_keys(keys)
    with _ledger_lock:
        _pressed_keys.update(vks)
        _unresolved_keys.update(unknown)
    return {"tracked_vks": [hex(v) for v in vks], "unresolved": unknown}


def track_key_release(keys: str | Iterable[str]) -> dict[str, list[str]]:
    vks, _ = resolve_keys(keys)
    with _ledger_lock:
        for vk in vks:
            _pressed_keys.discard(vk)
    return {"released_vks": [hex(v) for v in vks]}


def track_press_maybe_stuck(keys: str | Iterable[str]) -> None:
    """一次会自行 press+release 的调用失败了——保守地记为"可能仍按下"。"""
    vks, unknown = resolve_keys(keys)
    with _ledger_lock:
        _pressed_keys.update(vks)
        _unresolved_keys.update(unknown)


def track_button_press(button: str) -> None:
    with _ledger_lock:
        _pressed_buttons.add(str(button))


def track_button_release(button: str) -> None:
    with _ledger_lock:
        _pressed_buttons.discard(str(button))


def tracked_input() -> dict[str, list[str]]:
    with _ledger_lock:
        return {
            "keys": sorted(hex(v) for v in _pressed_keys),
            "buttons": sorted(_pressed_buttons),
            "unresolved": sorted(_unresolved_keys),
        }


def clear_tracking() -> None:
    with _ledger_lock:
        _pressed_keys.clear()
        _pressed_buttons.clear()
        _unresolved_keys.clear()


def release_all_keys_and_buttons() -> dict[str, object]:
    """紧急停止核心：**只**释放 UHA 自己按下且尚未释放的键/鼠标键。

    P0.1 §14：不猜测、不全量。发 UP 之前用 `GetAsyncKeyState` 复核，避免往用户的
    输入流里注入多余的 UP 事件。可重复调用（幂等）。
    """
    keys: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    with _ledger_lock:
        pending_keys = sorted(_pressed_keys)
        pending_buttons = sorted(_pressed_buttons)
        unresolved = sorted(_unresolved_keys)
        _pressed_keys.clear()
        _pressed_buttons.clear()
        _unresolved_keys.clear()

    for vk in pending_keys:
        try:
            if not (user32.GetAsyncKeyState(vk) & 0x8000):
                skipped.append(hex(vk))          # 已经是抬起状态，不必再发 UP
                continue
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
            _send_input(inp)
            keys.append(hex(vk))
        except Exception:
            with _ledger_lock: _pressed_keys.add(vk)
            failed.append(hex(vk))

    buttons: list[str] = []
    for name in pending_buttons:
        flag = _BUTTON_UP_FLAG.get(name)
        vk = _BUTTON_VK.get(name)
        if flag is None or vk is None:
            continue
        try:
            if not (user32.GetAsyncKeyState(vk) & 0x8000):
                skipped.append(f"{name}button")
                continue
            inp = INPUT(type=INPUT_MOUSE)
            inp.union.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
            _send_input(inp)
            buttons.append(name)
        except Exception:
            with _ledger_lock: _pressed_buttons.add(name)
            failed.append(name)

    return {
        "ok": not failed and not unresolved,
        "failed": failed,
        "keys_released": keys,
        "buttons_released": buttons,
        "already_up": skipped,
        "unresolved": unresolved,
        "scope": "uha_tracked_only",
    }


def release_everything_now(*, explicit: bool = False) -> dict[str, object]:
    """人工诊断用逃生舱——**不要**在自动路径里调用。

    只有在你明确知道用户没有按住任何东西时才有意义，否则会打断用户正在进行的
    拖拽/按键。必须显式传 `explicit=True`。
    """
    if not explicit:
        raise RuntimeError("release_everything_now requires explicit=True (manual use only)")
    keys: list[str] = []
    buttons: list[str] = []
    for vk in _RELEASE_VKS:
        try:
            if not (user32.GetAsyncKeyState(vk) & 0x8000):
                continue
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
            _send_input(inp)
            keys.append(hex(vk))
        except Exception:
            continue
    for flag, name in (
        (MOUSEEVENTF_LEFTUP, "left"),
        (MOUSEEVENTF_RIGHTUP, "right"),
        (MOUSEEVENTF_MIDDLEUP, "middle"),
    ):
        try:
            if not (user32.GetAsyncKeyState(_BUTTON_VK[name]) & 0x8000):
                continue
            inp = INPUT(type=INPUT_MOUSE)
            inp.union.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
            _send_input(inp)
            buttons.append(name)
        except Exception:
            continue
    return {"keys_released": keys, "buttons_released": buttons, "scope": "manual_escape_hatch"}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", _INPUTunion)]


def _send_input(*inputs: INPUT) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))
    if sent != n:
        raise OSError(f"SendInput delivered {sent}/{n} events")


def parse_hotkey(spec: str) -> tuple[int, int]:
    """`ctrl+alt+shift+f12` -> (modifiers, vk)。"""
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    mods = 0
    vk = 0
    for p in parts:
        if p in VK_MAP:
            mods |= VK_MAP[p]
            continue
        if p.startswith("f") and p[1:].isdigit():
            n = int(p[1:])
            if 1 <= n <= 24:
                vk = 0x70 + (n - 1)  # VK_F1=0x70
                continue
        if len(p) == 1:
            vk = ord(p.upper())
            continue
        if p.startswith("0x"):
            vk = int(p, 16)
    if vk == 0:
        vk = 0x7B  # F12
    if mods == 0:
        mods = MOD_CONTROL | MOD_ALT | MOD_SHIFT
    return mods | MOD_NOREPEAT, vk


class EmergencyHotkey:
    """注册系统热键；触发时执行确定性 emergency 回调。"""

    def __init__(self, callback: Callable[[], None], *, hotkey: str = "ctrl+alt+shift+f12"):
        self.callback = callback
        self.hotkey_spec = hotkey
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._hotkey_id = 1
        self.registered = False
        self._use_polling = False
        self._mods, self._vk = parse_hotkey(hotkey)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="uha-estop-hotkey", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.registered:
            try:
                user32.UnregisterHotKey(None, self._hotkey_id)
            except Exception:
                pass
            self.registered = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        # 尝试 RegisterHotKey
        ok = False
        try:
            ok = bool(user32.RegisterHotKey(None, self._hotkey_id, self._mods, self._vk))
        except Exception:
            ok = False
        if not ok:
            self._use_polling = True
            self._poll_loop()
            return
        self.registered = True
        msg = ctypes.wintypes.MSG()
        while not self._stop.is_set():
            has = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if has == 0:
                break
            if has == -1:
                break
            if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                try:
                    self.callback()
                except Exception:
                    pass
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.registered:
            try:
                user32.UnregisterHotKey(None, self._hotkey_id)
            except Exception:
                pass
            self.registered = False

    def _poll_loop(self) -> None:
        """回退：轮询修饰键+主键是否同时按下。"""
        mods_map = [
            (self._mods & MOD_CONTROL, 0x11),
            (self._mods & MOD_ALT, 0x12),
            (self._mods & MOD_SHIFT, 0x10),
            (self._mods & MOD_WIN, 0x5B),
        ]
        while not self._stop.is_set():
            try:
                all_down = all(user32.GetAsyncKeyState(vk) & 0x8000 for flag, vk in mods_map if flag)
                main_down = bool(user32.GetAsyncKeyState(self._vk) & 0x8000)
                if all_down and main_down:
                    self.callback()
                    time.sleep(0.5)  # 防抖
            except Exception:
                pass
            time.sleep(0.05)

    @property
    def mode(self) -> str:
        return "polling" if self._use_polling else ("registered" if self.registered else "stopped")


def build_emergency_handler(controller, coordinator=None, computer_use=None) -> Callable[[], None]:
    """组装紧急停止钩子：释放键鼠 → 释放 DesktopLock → 标记 ABORTED。"""

    def _handler() -> None:
        release_all_keys_and_buttons()
        if computer_use is not None:
            try:
                # 硬释放桌面锁（force）
                if hasattr(computer_use, "release_control"):
                    computer_use.release_control(force=True)
            except Exception:
                pass
        if coordinator is not None:
            try:
                coordinator.force_release_all()
            except Exception:
                pass
        try:
            controller.emergency_stop(reason="EMERGENCY_STOP")
        except Exception:
            pass

    return _handler


__all__ = [
    "release_all_keys_and_buttons",
    "parse_hotkey",
    "EmergencyHotkey",
    "build_emergency_handler",
]
