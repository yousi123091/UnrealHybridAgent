"""分层会话锁：READ 并行 / WRITE 串行 / DESKTOP 独占。

并发策略目标是缩短等待，不是堆线程：

* READ / ANALYSIS —— 可并行（查询 Actor、截图分析、语义分类、Router 评分…）
* WRITE           —— 必须串行（set_transform / spawn / save / Python mutation…）
* COMPUTER_CONTROL—— 独占桌面（现有 Computer Use DesktopLock 语义）

申请顺序固定为：UE_WRITE_LOCK → DESKTOP_LOCK，避免死锁。
只读路径**不**申请写锁，也**不**申请桌面锁。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from .errors import PreconditionFailed


class ReadWriteLock:
    """多读单写锁。读共享，写独占。"""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._write_wait = 0

    @contextmanager
    def read(self, *, timeout: float | None = None) -> Iterator[None]:
        deadline = None if timeout is None else time.time() + timeout
        with self._cond:
            # 写优先：有写者或写者在等时，新读者等待，避免写饥饿
            while self._writer or self._write_wait > 0:
                remain = None if deadline is None else deadline - time.time()
                if remain is not None and remain <= 0:
                    raise TimeoutError("acquire read lock timeout")
                self._cond.wait(timeout=remain)
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def write(self, *, timeout: float | None = None) -> Iterator[None]:
        deadline = None if timeout is None else time.time() + timeout
        with self._cond:
            self._write_wait += 1
            try:
                while self._writer or self._readers > 0:
                    remain = None if deadline is None else deadline - time.time()
                    if remain is not None and remain <= 0:
                        raise TimeoutError("acquire write lock timeout")
                    self._cond.wait(timeout=remain)
                self._writer = True
            finally:
                self._write_wait -= 1
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


@dataclass
class LockStats:
    acquisitions: dict[str, int] = field(default_factory=dict)
    total_wait_s: dict[str, float] = field(default_factory=dict)
    current_holders: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "acquisitions": dict(self.acquisitions),
            "total_wait_s": {k: round(v, 4) for k, v in self.total_wait_s.items()},
            "current_holders": dict(self.current_holders),
        }


class SessionCoordinator:
    """UE 会话内的统一锁协调器。

    * read         —— 并行
    * write        —— 串行（UE_WRITE_LOCK）
    * desktop      —— 独占（可桥接 Computer Use DesktopLock）
    * gui_mutation —— 固定顺序：write → desktop
    """

    def __init__(self, *, write_timeout: float = 60.0, desktop_timeout: float = 30.0):
        self._rw = ReadWriteLock()
        self._desktop = threading.RLock()
        self._stats = LockStats()
        self._meta_lock = threading.Lock()
        self.write_timeout = float(write_timeout)
        self.desktop_timeout = float(desktop_timeout)
        self._external_desktop_release: Callable[[], None] | None = None
        self._write_holders: list[str] = []

    def stats(self) -> dict[str, Any]:
        with self._meta_lock:
            d = self._stats.as_dict()
            d["write_holders"] = list(self._write_holders)
            return d

    def _note(self, kind: str, wait: float, holder: str = "") -> None:
        with self._meta_lock:
            self._stats.acquisitions[kind] = self._stats.acquisitions.get(kind, 0) + 1
            self._stats.total_wait_s[kind] = self._stats.total_wait_s.get(kind, 0.0) + wait
            if holder:
                self._stats.current_holders[kind] = holder
            elif kind in self._stats.current_holders:
                self._stats.current_holders.pop(kind, None)

    @contextmanager
    def read(self, *, op: str = "", timeout: float | None = None) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            with self._rw.read(timeout=timeout if timeout is not None else 120.0):
                self._note("read", time.perf_counter() - t0, op or "read")
                yield
        finally:
            pass

    @contextmanager
    def write(self, *, op: str = "", holder: str = "", timeout: float | None = None) -> Iterator[None]:
        label = holder or op or "write"
        t0 = time.perf_counter()
        with self._rw.write(timeout=timeout if timeout is not None else self.write_timeout):
            self._note("write", time.perf_counter() - t0, label)
            with self._meta_lock:
                self._write_holders.append(label)
            try:
                yield
            finally:
                with self._meta_lock:
                    if label in self._write_holders:
                        self._write_holders.remove(label)

    @contextmanager
    def desktop(self, *, holder: str = "", timeout: float | None = None) -> Iterator[None]:
        t0 = time.perf_counter()
        got = self._desktop.acquire(timeout=timeout if timeout is not None else self.desktop_timeout)
        if not got:
            raise PreconditionFailed("获取 DesktopLock 超时", details={"holder": holder})
        self._note("desktop", time.perf_counter() - t0, holder or "desktop")
        try:
            yield
        finally:
            try:
                self._desktop.release()
            except RuntimeError:
                pass

    @contextmanager
    def gui_mutation(self, *, op: str = "", holder: str = "") -> Iterator[None]:
        """GUI 改 UE：先 UE_WRITE_LOCK 再 DESKTOP_LOCK（固定顺序）。"""
        with self.write(op=op, holder=holder or op):
            with self.desktop(holder=holder or op):
                yield

    def force_release_all(self) -> dict[str, Any]:
        """紧急停止：尽量释放可释放的锁状态（写锁由 context 退出释放）。

        Desktop 若由外部 CU 持有，通过 hook 释放。
        """
        released: list[str] = []
        if self._external_desktop_release:
            try:
                self._external_desktop_release()
                released.append("external_desktop")
            except Exception:
                pass
        with self._meta_lock:
            self._write_holders.clear()
            self._stats.current_holders.clear()
        return {"released": released}

    def set_external_desktop_release(self, fn: Callable[[], None] | None) -> None:
        self._external_desktop_release = fn


# 写操作分类：哪些 skill/op 需要 UE_WRITE_LOCK
WRITE_OPS = frozenset({
    "set_transform", "set_location", "set_rotation", "set_scale",
    "spawn", "destroy", "modify_material", "save_level", "save",
    "rename", "blueprint_mutation", "python_mutation", "batch_mutation",
    "actor_move", "level_save",
})

READ_OPS = frozenset({
    "get_transform", "get_actors", "find_actor", "query", "inspect",
    "actor_find", "actor_inspect", "visual_inspect", "screenshot", "analyze",
})

GUI_OPS = frozenset({
    "mouse", "keyboard", "click", "type", "hotkey", "drag", "computer_use",
})


def requires_write_lock(op_or_skill: str) -> bool:
    name = str(op_or_skill or "").lower()
    return name in {x.lower() for x in WRITE_OPS} or name.startswith("set_") or "save" in name or "mutat" in name


def requires_desktop_lock(op_or_skill: str) -> bool:
    name = str(op_or_skill or "").lower()
    return name in {x.lower() for x in GUI_OPS} or name in {"keyboard", "mouse"}


_DEFAULT_COORD: SessionCoordinator | None = None
_COORD_LOCK = threading.Lock()


def get_coordinator() -> SessionCoordinator:
    global _DEFAULT_COORD
    with _COORD_LOCK:
        if _DEFAULT_COORD is None:
            _DEFAULT_COORD = SessionCoordinator()
        return _DEFAULT_COORD


def reset_coordinator() -> SessionCoordinator:
    global _DEFAULT_COORD
    with _COORD_LOCK:
        _DEFAULT_COORD = SessionCoordinator()
        return _DEFAULT_COORD


__all__ = [
    "ReadWriteLock",
    "SessionCoordinator",
    "LockStats",
    "get_coordinator",
    "reset_coordinator",
    "requires_write_lock",
    "requires_desktop_lock",
    "WRITE_OPS",
    "READ_OPS",
]
