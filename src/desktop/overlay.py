from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..core.session_control import SessionController, TaskPhase

DEFAULT_GEOMETRY = "320x180+20+20"
LEGACY_OVERLAY_RELEASE_ENABLED = False

try:  # pragma: no cover - 部分精简 Python 无 tkinter
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore


class ControlOverlay:
    """后台线程驱动的状态浮窗。无 tkinter / headless 时静默降级。"""

    def __init__(
        self,
        controller: SessionController,
        *,
        geometry: str = DEFAULT_GEOMETRY,
        title: str = "UnrealHybridAgent",
        poll_ms: int = 200,
        on_pause: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        on_emergency: Callable[[], None] | None = None,
    ):
        self.controller = controller
        self.geometry = geometry
        self.title = title
        self.poll_ms = int(poll_ms)
        self._on_pause = on_pause or controller.pause
        self._on_stop = on_stop or controller.stop
        self._on_emergency = on_emergency or controller.emergency_stop
        self._thread: threading.Thread | None = None
        self._root: Any = None
        self._labels: dict[str, Any] = {}
        self._alive = False
        self._visible = False
        # Deferred from this release after a real Tcl owner-thread crash.
        self.available = False

    def start(self) -> None:
        # Keep the implementation for the next version; never create Tk here.
        if not LEGACY_OVERLAY_RELEASE_ENABLED:
            return
        if tk is None:
            self.available = False
            return
        if self._thread and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="uha-overlay", daemon=True)
        self._thread.start()

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

    def show(self) -> None:
        self._visible = True

    def hide(self) -> None:
        self._visible = False
        root = self._root
        if root is not None:
            try:
                root.after(0, lambda: root.withdraw())
            except Exception:
                pass

    def refresh_now(self) -> dict[str, Any]:
        return self.controller.snapshot().as_dict()

    def _run(self) -> None:
        if tk is None:
            return
        try:
            self._root = tk.Tk()
        except Exception:
            self._root = None
            return
        root = self._root
        root.title(self.title)
        root.geometry(self.geometry)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.94)
        except Exception:
            pass

        frame = tk.Frame(root, bg="#1e1e1e", padx=10, pady=8)
        frame.pack(fill=tk.BOTH, expand=True)

        def _lbl(key: str, text: str):
            lab = tk.Label(frame, text=text, anchor="w", bg="#1e1e1e", fg="#d4d4d4",
                           font=("Segoe UI", 9))
            lab.pack(fill=tk.X)
            self._labels[key] = lab
            return lab

        _lbl("title", "UnrealHybridAgent 正在运行")
        _lbl("task", "当前任务：-")
        _lbl("step", "当前步骤：-")
        _lbl("method", "当前执行方式：-")
        _lbl("phase", "当前状态：IDLE")
        _lbl("status", "-")
        _lbl("next", "下一步：-")
        warn = tk.Label(frame, text="", anchor="w", bg="#1e1e1e", fg="#f4a261",
                        font=("Segoe UI", 9, "bold"), wraplength=300, justify="left")
        warn.pack(fill=tk.X, pady=(6, 4))
        self._labels["warn"] = warn

        btns = tk.Frame(frame, bg="#1e1e1e")
        btns.pack(fill=tk.X, pady=(4, 0))
        tk.Button(btns, text="暂停", command=self._on_pause, width=8).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="停止", command=self._on_stop, width=8).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="紧急停止", command=self._on_emergency, width=10,
                  bg="#8b0000", fg="white").pack(side=tk.LEFT, padx=2)

        def _tick() -> None:
            if not self._alive:
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            st = self.controller.snapshot()
            should = st.should_show_overlay or self._visible
            try:
                if should:
                    root.deiconify()
                else:
                    root.withdraw()
                self._labels["task"]["text"] = f"当前任务：{st.task or '-'}"
                self._labels["step"]["text"] = f"当前步骤：{st.step or '-'}"
                self._labels["method"]["text"] = f"当前执行方式：{st.method or '-'}"
                self._labels["phase"]["text"] = f"当前状态：{st.phase.value}"
                self._labels["status"]["text"] = st.status_text or "-"
                self._labels["next"]["text"] = f"下一步：{st.next_step or '-'}"
                self._labels["warn"]["text"] = st.overlay_message
            except Exception:
                pass
            root.after(self.poll_ms, _tick)

        root.after(self.poll_ms, _tick)
        try:
            root.mainloop()
        except Exception:
            pass


__all__ = ["ControlOverlay", "DEFAULT_GEOMETRY"]
