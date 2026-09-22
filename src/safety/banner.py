"""P0 always-on-top Computer Use safety banner."""

from __future__ import annotations

import threading
import time
import os
from typing import Any, Callable

from .controller import SafetyController, SafetyState

try:
    import tkinter as tk
except Exception:  # pragma: no cover
    tk = None  # type: ignore


BANNER_TEXT = {
    "active": (
        "⚠ UHA COMPUTER USE\n"
        "Agent is injecting mouse/keyboard input.\n"
        "Move your mouse or press a key to take over.\n"
        "Ctrl + Alt + F12 — EMERGENCY STOP\n"
        "{task}"
    ),
    "waiting": (
        "⚠ UHA COMPUTER USE SESSION\n"
        "Agent currently waiting (injection permission held).\n"
        "Move mouse / press key to take over.\n"
        "{task}"
    ),
    "emergency": (
        "⛔ EMERGENCY STOPPED\n"
        "Agent injection DISABLED.\n"
        "User always keeps physical control.\n"
        "Resume manually when ready."
    ),
    "human_override": (
        "✋ HUMAN OVERRIDE\n"
        "Agent input paused.\n"
        "You are in control.\n"
        "Resume manually when ready."
    ),
    "finished": (
        "✓ COMPUTER USE FINISHED\n"
        "Agent injection permission released.\n"
        "Physical input was never blocked."
    ),
}

COLORS = {
    "active": ("#7f1d1d", "#fff7ed"),
    "waiting": ("#92400e", "#fffbeb"),
    "emergency": ("#450a0a", "#fee2e2"),
    "human_override": ("#1e3a5f", "#e0f2fe"),
    "finished": ("#14532d", "#dcfce7"),
}


class SafetyBanner:
    """Independent of agent HUD. Does not steal focus. Shows EStop hotkey."""

    def __init__(self, controller: SafetyController, *, geometry: str | None = None):
        self.controller = controller
        controller.set_banner_probe(lambda: self.visible)
        self.geometry = geometry or "1280x72+40+8"
        self._thread: threading.Thread | None = None
        self._root: Any = None
        self._label: Any = None
        self._btn: Any = None
        self._alive = False
        self._visible = False
        self._resume_shown = False
        self.available = tk is not None
        self._poll_ms = 200
        self._last_state = ""

    def start(self) -> bool:
        if tk is None:
            self.available = False
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="uha-safety-banner", daemon=True)
        self._thread.start()
        # wait briefly for root
        for _ in range(20):
            if self._root is not None:
                return True
            time.sleep(0.05)
        return self._root is not None

    def stop(self) -> None:
        self._alive = False
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._visible = False

    @property
    def visible(self) -> bool:
        return bool(self._visible and self._alive and self._thread and self._thread.is_alive())

    def show_state(self, banner_state: str, task: str = "") -> None:
        self._last_state = banner_state
        # actual UI update happens in poll loop

    def _run(self) -> None:
        if tk is None:
            return
        try:
            self._root = tk.Tk()
        except Exception:
            self._root = None
            return
        root = self._root
        root.title("UHA Safety Banner")
        root.geometry(self.geometry)
        root.attributes("-topmost", True)
        try:
            root.overrideredirect(True)
        except Exception:
            pass
        try:
            root.attributes("-alpha", 0.96)
        except Exception:
            pass

        self._label = tk.Label(root, text="UHA SAFETY", anchor="w", justify="left",
                               bg="#7f1d1d", fg="#fff7ed", font=("Segoe UI", 11, "bold"),
                               padx=12, pady=6)
        self._label.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # STOP is a convenience control, NOT the safety mechanism (P0.1 §8).
        # The real safety mechanisms are the physical override and the global hotkey.
        self._btn = tk.Button(root, text="STOP", command=self._on_stop_click,
                              bg="#7f1d1d", fg="white", activebackground="#450a0a",
                              font=("Segoe UI", 10, "bold"), width=14)
        self._btn.pack(side=tk.RIGHT, fill=tk.Y, padx=4, pady=4)
        # Explicit user resume (P0.1 §10): the ONLY way out of a terminal state.
        self._btn_resume = tk.Button(root, text="Resume Computer Use", command=self._on_resume_click,
                                     bg="#14532d", fg="white", activebackground="#052e16",
                                     font=("Segoe UI", 10, "bold"), width=20)

        def _tick() -> None:
            if not self._alive:
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            snap = self.controller.snapshot()
            try:
                self.controller.check_heartbeat()
                snap = self.controller.snapshot()
            except Exception:
                pass
            state = snap.banner_state
            if snap.state == SafetyState.EMERGENCY_STOP or snap.human_override:
                if getattr(snap, "human_override", False) and snap.state.value == "HUMAN_OVERRIDE":
                    state = "human_override"
                else:
                    state = "emergency"
            elif snap.state.value == "HUMAN_OVERRIDE":
                state = "human_override"
            elif snap.agent_injection_permission and snap.allow_input:
                state = "active"
            elif snap.lifecycle.value in ("CONTROL_ACQUIRED", "ACTIVE", "REQUESTED", "BANNER_VISIBLE") and snap.banner_visible:
                state = "waiting"
            elif snap.lifecycle.value == "RELEASED" and state == "finished":
                state = "finished"
            elif not snap.banner_visible and snap.state.value in ("IDLE", "RELEASED"):
                if self._visible:
                    try:
                        root.withdraw()
                    except Exception:
                        pass
                    self._visible = False
                root.after(self._poll_ms, _tick)
                return

            bg, fg = COLORS.get(state, COLORS["active"])
            text = BANNER_TEXT.get(state, BANNER_TEXT["active"]).format(task=snap.task or "-")
            if state in ("active", "waiting"):
                text += f"\nMove mouse/press key to take over | owner=USER inject={snap.agent_injection_permission}"
            terminal = snap.state.value in ("EMERGENCY_STOP", "HUMAN_OVERRIDE")
            if terminal:
                text += "\nYou keep physical control. Resume needs an explicit click."
            try:
                self._label.configure(text=text, bg=bg, fg=fg)
                self._btn.configure(bg=bg)
                # the resume control only exists while a terminal state is active
                if terminal and not self._resume_shown:
                    self._btn_resume.pack(side=tk.RIGHT, fill=tk.Y, padx=4, pady=4)
                    self._resume_shown = True
                elif not terminal and self._resume_shown:
                    self._btn_resume.pack_forget()
                    self._resume_shown = False
                if not self._visible:
                    root.deiconify()
                    try:
                        root.lift()
                    except Exception:
                        pass
                    self._visible = True
                    self.controller.banner_visible(True)
            except Exception:
                pass
            root.after(self._poll_ms, _tick)

        root.after(self._poll_ms, _tick)
        try:
            root.mainloop()
        except Exception:
            pass

    def _on_stop_click(self) -> None:
        try:
            self.controller.emergency_stop(reason="banner_stop_button", source="banner_button")
        except Exception:
            pass

    def _on_resume_click(self) -> None:
        """Explicit user resume (P0.1 §10/§11).

        Resume does NOT restart the old action queue: the queue is cleared and the
        agent must re-observe the screen before it may act again.
        """
        try:
            self.controller.resume_safety(explicit=True)
        except Exception:
            pass


def build_safety_banner(controller: SafetyController, *, geometry: str | None = None):
    """Return a banner that can actually be shown on *this* interpreter.

    ``import tkinter`` fails on the project ``.venv`` and on the managed Python 3.13
    (only the system Python 3.12 ships ``_tkinter``). Rather than let ``grant_control``
    check a boolean nobody can satisfy, fall back to the pure-Win32 backend, which
    needs nothing but ``user32``/``gdi32``.
    """
    geo = geometry or "1280x84+40+8"
    # A CU caller can run on a background executor thread even when tkinter is
    # installed. Use the native owner-thread implementation on Windows.
    if os.name == 'nt':
        from .banner_win32 import Win32SafetyBanner
        return Win32SafetyBanner(controller, geometry=geo)
    if tk is not None:
        try:
            b = SafetyBanner(controller, geometry=geo)
            if b.available:
                return b
        except Exception:
            pass
    from .banner_win32 import Win32SafetyBanner

    return Win32SafetyBanner(controller, geometry=geo)


__all__ = ["SafetyBanner", "BANNER_TEXT", "build_safety_banner"]
