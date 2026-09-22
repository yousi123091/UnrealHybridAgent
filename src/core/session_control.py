"""运行时任务控制状态机：Pause / Resume / Stop / Emergency Stop。

设计原则（对应 ROADMAP §十一）：

* PAUSE     —— 停止**新的**写操作与 Computer Use 动作；保留上下文，可 Resume。
* STOP      —— 终止当前任务，释放锁，不再 fallback。
* EMERGENCY —— **确定性本地逻辑**，不依赖 LLM/Vision/Router：
              停动作队列 + 释放键鼠 + 释放 DesktopLock + 释放 UE 写锁 + ABORTED_BY_USER。

状态：IDLE / ANALYZING / STRUCTURED_EXECUTION / COMPUTER_CONTROL / VERIFYING /
     PAUSED / STOPPING / ABORTED / DONE。
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class TaskPhase(str, enum.Enum):
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    STRUCTURED_EXECUTION = "STRUCTURED_EXECUTION"
    COMPUTER_CONTROL = "COMPUTER_CONTROL"
    VERIFYING = "VERIFYING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    ABORTED = "ABORTED"
    DONE = "DONE"
    FAILED = "FAILED"


class ControlCommand(str, enum.Enum):
    NONE = "NONE"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"
    EMERGENCY_STOP = "EMERGENCY_STOP"


@dataclass
class ControlState:
    phase: TaskPhase = TaskPhase.IDLE
    task: str = ""
    step: str = ""
    method: str = ""
    status_text: str = ""
    next_step: str = ""
    controls_mouse_keyboard: bool = False
    command: ControlCommand = ControlCommand.NONE
    abort_reason: str | None = None
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "task": self.task,
            "step": self.step,
            "method": self.method,
            "status_text": self.status_text,
            "next_step": self.next_step,
            "controls_mouse_keyboard": self.controls_mouse_keyboard,
            "command": self.command.value,
            "abort_reason": self.abort_reason,
            "updated_at": self.updated_at,
        }

    @property
    def should_show_overlay(self) -> bool:
        return self.phase not in (TaskPhase.IDLE, TaskPhase.DONE)

    @property
    def blocks_writes(self) -> bool:
        """暂停/停止/紧急停止/失败中止时，禁止新的写与 CU 动作。"""
        return self.command in (
            ControlCommand.PAUSE,
            ControlCommand.STOP,
            ControlCommand.EMERGENCY_STOP,
        ) or self.phase in (
            TaskPhase.PAUSED,
            TaskPhase.STOPPING,
            TaskPhase.ABORTED,
            TaskPhase.FAILED,
        )

    @property
    def overlay_message(self) -> str:
        if self.phase == TaskPhase.ABORTED:
            return f"已紧急停止/中止：{self.abort_reason or 'user'}"
        if self.phase == TaskPhase.PAUSED:
            return "已暂停：不再发送新的写操作与键鼠动作"
        if self.phase == TaskPhase.STOPPING:
            return "正在停止任务…"
        if self.controls_mouse_keyboard or self.phase == TaskPhase.COMPUTER_CONTROL:
            return "Computer Use 正在控制鼠标和键盘，请暂时不要操作电脑。"
        if self.phase in (TaskPhase.ANALYZING, TaskPhase.VERIFYING):
            return "后台分析中，当前未控制鼠标。"
        if self.phase == TaskPhase.STRUCTURED_EXECUTION:
            return "使用结构化通道（MCP/Python）执行，未控制鼠标。"
        return self.status_text or self.phase.value


class SessionController:
    """线程安全的任务控制器。Executor / Overlay / Hotkey 共用同一实例。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state = ControlState()
        self._listeners: list[Callable[[ControlState], None]] = []
        self._emergency_hooks: list[Callable[[], None]] = []
        self._resume_event = threading.Event()
        self._resume_event.set()

    # -- 观察 -----------------------------------------------------------------

    def snapshot(self) -> ControlState:
        with self._lock:
            return ControlState(**self.state.as_dict() | {"phase": TaskPhase(self.state.phase),
                                                          "command": ControlCommand(self.state.command)})

    def on_change(self, fn: Callable[[ControlState], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def on_emergency(self, fn: Callable[[], None]) -> None:
        """注册紧急停止钩子：释放键鼠/锁等。**同步、确定性、必须短。**"""
        with self._lock:
            self._emergency_hooks.append(fn)

    def _notify(self) -> None:
        snap = self.snapshot()
        for fn in list(self._listeners):
            try:
                fn(snap)
            except Exception:
                pass

    def _update(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self.state, k, v)
        self.state.updated_at = time.time()
        self._notify()

    # -- 任务标注（给 Overlay 看） --------------------------------------------

    def begin_task(self, task: str) -> None:
        """标注任务开始。

        **不得**清掉 PAUSE / STOP / EMERGENCY_STOP / ABORTED：
        Executor.run() 会调用 begin_task，若这里无条件重置 command/phase，
        用户按下暂停后发起的新任务会把暂停状态冲掉，造成“暂停不生效”的假绿。
        """
        with self._lock:
            if (
                self.state.command
                in (ControlCommand.PAUSE, ControlCommand.STOP, ControlCommand.EMERGENCY_STOP)
                or self.state.phase == TaskPhase.ABORTED
            ):
                self._update(task=task or self.state.task, step="")
                return
            self._update(phase=TaskPhase.ANALYZING, task=task, step="", abort_reason=None,
                         command=ControlCommand.NONE, controls_mouse_keyboard=False)
            self._resume_event.set()

    def set_step(self, step: str, *, method: str = "", status: str = "", next_step: str = "") -> None:
        with self._lock:
            self._update(step=step, method=method or self.state.method,
                         status_text=status or self.state.status_text,
                         next_step=next_step or self.state.next_step)

    def set_phase(self, phase: TaskPhase, *, controls_mouse: bool | None = None) -> None:
        with self._lock:
            kw: dict[str, Any] = {"phase": phase}
            if controls_mouse is not None:
                kw["controls_mouse_keyboard"] = controls_mouse
            elif phase != TaskPhase.COMPUTER_CONTROL:
                kw["controls_mouse_keyboard"] = False
            self._update(**kw)

    def set_method(self, method: str) -> None:
        with self._lock:
            self._update(method=method)

    def end_task(self, ok: bool = True) -> None:
        """任务结束标注。

        **不得**清掉用户控制命令（PAUSE / STOP / EMERGENCY_STOP / ABORTED）。
        否则执行循环里的 abort 收尾或只读任务的 end_task 会把暂停/中止状态
        悄悄冲掉，下一次 run 又能写 UE —— 这是假绿。
        """
        with self._lock:
            if (
                self.state.command
                in (ControlCommand.PAUSE, ControlCommand.STOP, ControlCommand.EMERGENCY_STOP)
                or self.state.phase == TaskPhase.ABORTED
            ):
                # 保留控制状态；仅刷新时间戳，便于 Overlay/日志观察
                self.state.updated_at = time.time()
                self._notify()
                return
            self._update(
                phase=TaskPhase.DONE if ok else TaskPhase.FAILED,
                controls_mouse_keyboard=False,
                command=ControlCommand.NONE,
            )
            self._resume_event.set()

    def acknowledge_control(self) -> None:
        """显式清空用户控制命令（仅供测试/运维在确认后调用）。

        生产路径不应在 end_task/begin_task 里自动清空 PAUSE/STOP/ESTOP。
        """
        with self._lock:
            self._update(
                phase=TaskPhase.IDLE,
                command=ControlCommand.NONE,
                abort_reason=None,
                controls_mouse_keyboard=False,
                status_text="",
            )
            self._resume_event.set()

    # -- 控制命令 --------------------------------------------------------------

    def pause(self) -> None:
        with self._lock:
            if self.state.phase in (TaskPhase.ABORTED, TaskPhase.DONE):
                return
            self._update(command=ControlCommand.PAUSE, phase=TaskPhase.PAUSED,
                         controls_mouse_keyboard=False)
            self._resume_event.clear()

    def resume(self) -> None:
        with self._lock:
            if self.state.command != ControlCommand.PAUSE:
                return
            self._update(command=ControlCommand.NONE, phase=TaskPhase.ANALYZING)
            self._resume_event.set()

    def stop(self) -> None:
        with self._lock:
            self._update(command=ControlCommand.STOP, phase=TaskPhase.STOPPING,
                         controls_mouse_keyboard=False, abort_reason="STOP")
            self._resume_event.set()  # 让 wait_if_paused 立刻返回，由上层检查 should_abort

    def emergency_stop(self, reason: str = "EMERGENCY_STOP") -> None:
        """紧急停止：先跑确定性钩子（释放键鼠/锁），再标 ABORTED。"""
        with self._lock:
            self._update(
                command=ControlCommand.EMERGENCY_STOP,
                phase=TaskPhase.ABORTED,
                controls_mouse_keyboard=False,
                abort_reason=reason,
            )
            self._resume_event.set()
            hooks = list(self._emergency_hooks)
        for hook in hooks:
            try:
                hook()
            except Exception:
                # 钩子失败不得阻止后续释放
                pass

    # -- 执行点调用 ------------------------------------------------------------

    def should_abort(self) -> bool:
        with self._lock:
            return self.state.command in (
                ControlCommand.STOP,
                ControlCommand.EMERGENCY_STOP,
            ) or self.state.phase == TaskPhase.ABORTED

    def blocks_writes(self) -> bool:
        with self._lock:
            return self.state.blocks_writes

    def wait_if_paused(self, timeout: float | None = None) -> bool:
        """暂停时阻塞；返回 False 表示应中止（STOP/ESTOP）。

        若在 timeout 内仍未 Resume，返回 **False**（不再假装可以继续写），
        避免“超时放行 → 后续写尝试 → end_task 洗掉 PAUSE”的连锁问题。
        """
        if self.should_abort():
            return False
        if self.state.blocks_writes and self.state.command == ControlCommand.PAUSE:
            if not self._resume_event.is_set():
                ok = self._resume_event.wait(timeout=timeout)
                if not ok:
                    return False
        if not self._resume_event.is_set():
            ok = self._resume_event.wait(timeout=timeout)
            if not ok:
                return not self.should_abort()
        return not self.should_abort() and not self.blocks_writes()

    def ensure_writable(self, action: str = "") -> None:
        """写操作 / CU 动作前调用。暂停或已中止则抛 RuntimeError。"""
        from .errors import PreconditionFailed

        if self.should_abort():
            raise PreconditionFailed(
                f"任务已中止（{self.state.abort_reason}），拒绝继续：{action}",
                details={"phase": self.state.phase.value, "action": action},
            )
        if self.state.blocks_writes:
            raise PreconditionFailed(
                f"任务处于 {self.state.phase.value}，拒绝写/键鼠操作：{action}",
                details={"phase": self.state.phase.value, "action": action},
            )


# 进程内默认控制器（CLI / Overlay / Hotkey 共用）
_DEFAULT: SessionController | None = None
_DEFAULT_LOCK = threading.Lock()


def get_controller() -> SessionController:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = SessionController()
        return _DEFAULT


def reset_controller() -> SessionController:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = SessionController()
        return _DEFAULT


__all__ = [
    "TaskPhase",
    "ControlCommand",
    "ControlState",
    "SessionController",
    "get_controller",
    "reset_controller",
]
