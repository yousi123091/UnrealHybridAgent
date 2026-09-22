"""完成提醒 —— "Agent 已经完成，但我没有注意到" 这个真实痛点。

需求 §十三 的三条硬要求，逐条落在这里：

1. **要提醒**：``DONE`` / ``ERROR`` / ``WAITING_INPUT`` / ``WAITING_APPROVAL``
   （外加 ``BLOCKED``，它同样需要人）从"非提醒状态"变成提醒状态时触发。
2. **同一个状态变化只能提醒一次**：以 ``(agent_id, status, status_changed_at)``
   为去重键。同一个变化重复到达（重连、重放、心跳）**不会**再响。
3. **一定要有 mute**：``set_mute(True)``，并且**持久化**——不然重启一次又开始弹。
   另外还有每 Agent 的最小间隔，防止一个 Agent 高频抖动刷屏。

三个通道，任一可用即可，全部不可用也不影响 HUD 显示：

* ``HudFlashSink`` —— 窗口自身变化（由宿主注入；最可靠，不需要任何系统能力）
* ``SoundSink``    —— ``winsound``，Windows 标准库，两个解释器都有
* ``ToastSink``    —— Windows 通知，走 PowerShell；失败就自动永久禁用（不再重试）

**刻意不做**的事：不弹模态对话框、不重复响、不因为提醒失败而影响状态流。
``consider()`` 只返回一个 ``Notification``，谁来决定怎么表现由宿主决定。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..core.models import AgentSnapshot, Status

#: 会被提醒的状态。与 ``Status.is_alert`` 保持一致（那个属性是权威，这里只是展示用）。
ALERT_STATUSES = (Status.DONE, Status.ERROR, Status.WAITING_INPUT,
                  Status.WAITING_APPROVAL, Status.BLOCKED)

_SEVERITY: dict[Status, str] = {
    Status.DONE: "info",
    Status.ERROR: "error",
    Status.WAITING_INPUT: "attention",
    Status.WAITING_APPROVAL: "attention",
    Status.BLOCKED: "attention",
    Status.CANCELLED: "info",
}

_TITLE: dict[Status, str] = {
    Status.DONE: "已完成",
    Status.ERROR: "出错了",
    Status.WAITING_INPUT: "等待你的输入",
    Status.WAITING_APPROVAL: "等待你批准",
    Status.BLOCKED: "被卡住了",
}


@dataclass
class Notification:
    agent_id: str
    agent_name: str
    status: Status
    title: str
    message: str
    severity: str = "info"
    created_at: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str, float]:
        return (self.agent_id, self.status.value, self.created_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "status": self.status.value,
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------


class Sink:
    """提醒通道。``available`` 为假时会被跳过（不报错）。"""

    name = "sink"
    available = True

    def send(self, note: Notification) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class CollectSink(Sink):
    """只记录不表现。测试用。"""

    name = "collect"

    def __init__(self) -> None:
        self.items: list[Notification] = []
        self._lock = threading.Lock()

    def send(self, note: Notification) -> None:
        with self._lock:
            self.items.append(note)

    def drain(self) -> list[Notification]:
        with self._lock:
            out = list(self.items)
            self.items.clear()
        return out


class SoundSink(Sink):
    """提示音。非阻塞（``MessageBeep`` 本身很快，但仍放线程里以防万一）。"""

    name = "sound"

    _BEEP = {
        "info": 0x00000040,       # MB_ICONASTERISK
        "attention": 0x00000030,  # MB_ICONEXCLAMATION
        "error": 0x00000010,      # MB_ICONHAND
    }

    def __init__(self) -> None:
        try:
            import winsound  # noqa: F401
            self._winsound: Any = winsound
            self.available = True
        except Exception:  # noqa: BLE001 - 非 Windows / 精简运行时不响就是了
            self._winsound = None
            self.available = False

    def send(self, note: Notification) -> None:
        if not self.available:
            return
        flags = self._BEEP.get(note.severity, 0x40)

        def _beep() -> None:
            try:
                self._winsound.MessageBeep(flags)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_beep, name="uah-beep", daemon=True).start()


class ToastSink(Sink):
    """Windows 通知（走 PowerShell，best-effort）。

    失败一次就**永久禁用**：反复起 PowerShell 只为发一个通知，代价比通知本身大。
    所以 ``available`` 是个"用一次就定下来"的标志。
    """

    name = "toast"

    _PS = (
        "try {"
        "  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        "   ContentType=WindowsRuntime] > $null;"
        "  $t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "        [Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "  $x = $t.GetElementsByTagName('text');"
        "  $x.Item(0).AppendChild($t.CreateTextNode($env:UAH_TITLE)) > $null;"
        "  $x.Item(1).AppendChild($t.CreateTextNode($env:UAH_MSG)) > $null;"
        "  $n = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('UAH');"
        "  $n.Show([Windows.UI.Notifications.ToastNotification]::new($t));"
        "  exit 0"
        "} catch { exit 1 }"
    )

    def __init__(self, *, enabled: bool = True, timeout_s: float = 8.0) -> None:
        self.available = bool(enabled) and os.name == "nt"
        self.timeout_s = timeout_s
        self.last_error: str | None = None
        self._lock = threading.Lock()
        #: 发一条 toast 要起一次 PowerShell（实测 ~0.9s）。**绝不能同步做**：
        #: HUD 的提醒是在 UI 线程里评估的，同步发一条就会冻结界面近一秒。
        #: 所以丢给后台线程；同时只允许一条在飞，多了就丢（提示不该堆积）。
        self._sending = False

    def send(self, note: Notification) -> None:
        if not self.available:
            return
        with self._lock:
            if self._sending:
                return
            self._sending = True
        threading.Thread(target=self._send_worker, args=(note,),
                         name="uah-toast", daemon=True).start()

    def _send_worker(self, note: Notification) -> None:
        env = dict(os.environ)
        env["UAH_TITLE"] = f"{note.agent_name} · {note.title}"
        env["UAH_MSG"] = note.message[:200]
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", self._PS],
                capture_output=True, env=env, timeout=self.timeout_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or b"").decode("utf-8", "replace")[:200] or "exit!=0")
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响任何东西
            with self._lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.available = False
        finally:
            with self._lock:
                self._sending = False


class HudFlashSink(Sink):
    """把"该提醒了"交给宿主自己表现（高亮、抬升窗口、闪边框）。"""

    name = "hud"

    def __init__(self, callback: Callable[[Notification], None]) -> None:
        self.callback = callback

    def send(self, note: Notification) -> None:
        try:
            self.callback(note)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


class Notifier:
    """提醒策略：什么时候提醒、提醒几次、响不响、怎么响。"""

    def __init__(
        self,
        *,
        sinks: Iterable[Sink] | None = None,
        mute: bool = False,
        min_interval_s: float = 4.0,
        memory: int = 512,
        state_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.sinks: list[Sink] = list(sinks) if sinks is not None else [SoundSink(), ToastSink()]
        self.min_interval_s = float(min_interval_s)
        self._memory = int(memory)
        self._seen: dict[tuple, None] = {}
        self._last_by_agent: dict[str, float] = {}
        self._last_status_by_agent: dict[str, str] = {}
        self._suppressed = 0
        self._sent: list[Notification] = []
        self._lock = threading.RLock()
        self.state_path: Path | None = Path(state_path) if state_path else None
        self._mute = bool(mute)
        if self.state_path is not None:
            self._load()

    # -- mute ---------------------------------------------------------------

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._mute

    def set_mute(self, value: bool, *, persist: bool = True) -> None:
        with self._lock:
            self._mute = bool(value)
        # 没有配置持久化路径时**不要**当成错误：静音本身仍然生效，
        # 只是这一次进程内有效。用户按一下静音按钮不该看到断言。
        if persist and self.state_path is not None:
            self._save()

    def toggle_mute(self) -> bool:
        self.set_mute(not self.muted)
        return self.muted

    def _load(self) -> None:
        if self.state_path is None:
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._mute = bool(data.get("mute", False))
        except Exception:  # noqa: BLE001 - 配置文件坏了就当没配过
            self._mute = False

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"mute": self._mute, "saved_at": time.time()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    # -- 决策 ---------------------------------------------------------------

    def consider(self, snapshot: AgentSnapshot, *, now: float | None = None) -> Notification | None:
        """看一眼这个快照，决定要不要提醒。返回提醒内容或 ``None``。"""
        ts = time.time() if now is None else now
        status = snapshot.status
        if not status.is_alert:
            return None

        # 去重键必须包含"具体在等什么"。
        # 只用 (agent, status, 变化时刻) 会有一个真实的漏报：Agent 在 WAITING_INPUT
        # 状态下问了**另一个**问题（状态没变、status_changed_at 也不变），
        # 用户就永远收不到第二次提醒 —— 而那正是他最需要被打扰的时刻。
        # 加上 activity 文案后：同一句话重复上报不重复提醒，换了问题则提醒。
        activity_key = (snapshot.activity.one_line or "")[:200]
        changed_at = snapshot.status_changed_at or snapshot.updated_at
        key = (snapshot.agent.id, status.value, round(float(changed_at), 3), activity_key)
        with self._lock:
            if key in self._seen:
                return None
            self._remember(key)
            if self._mute:
                self._suppressed += 1
                return None
            last = self._last_by_agent.get(snapshot.agent.id, 0.0)
            last_status = self._last_status_by_agent.get(snapshot.agent.id)
            # 最小间隔只用来压"终态类状态反复抖动"（DONE/ERROR 抽搐），
            # **不**拦 WAITING_INPUT / WAITING_APPROVAL / BLOCKED：
            # 那三种正是"必须立刻知道"的时刻，而且每一次都对应一个不同的问题
            # （去重键里带了具体文案，所以同一句话仍然只提醒一次）。
            if (not status.needs_human and last_status == status.value
                    and ts - last < self.min_interval_s):
                self._suppressed += 1
                return None
            self._last_by_agent[snapshot.agent.id] = ts
            self._last_status_by_agent[snapshot.agent.id] = status.value

        note = Notification(
            agent_id=snapshot.agent.id,
            agent_name=snapshot.agent.name,
            status=status,
            title=_TITLE.get(status, status.value),
            message=self._message(snapshot),
            severity=_SEVERITY.get(status, "info"),
        )
        self._dispatch(note)
        with self._lock:
            self._sent.append(note)
            del self._sent[:-200]
        return note

    @staticmethod
    def _message(snapshot: AgentSnapshot) -> str:
        bits: list[str] = []
        task = snapshot.task
        if task.phase or task.name:
            bits.append(" · ".join(b for b in (task.phase, task.name) if b))
        if task.stage:
            bits.append(task.stage)
        if snapshot.activity.one_line:
            bits.append(snapshot.activity.one_line)
        if snapshot.activity.detail:
            bits.append(snapshot.activity.detail)
        return " | ".join(bits) or f"状态变为 {snapshot.status.value}"

    def _dispatch(self, note: Notification) -> None:
        for sink in self.sinks:
            if not getattr(sink, "available", True):
                continue
            try:
                sink.send(note)
            except Exception:  # noqa: BLE001 - 一个通道坏了不能挡住别的
                pass

    def _remember(self, key: tuple) -> None:
        self._seen[key] = None
        while len(self._seen) > self._memory:
            self._seen.pop(next(iter(self._seen)))

    def reset_memory(self) -> None:
        """清空去重记忆（测试用；也允许"我把它静音了，现在想重新收到提醒"）。"""
        with self._lock:
            self._seen.clear()
            self._last_by_agent.clear()
            self._last_status_by_agent.clear()

    # -- 观察 ---------------------------------------------------------------

    def sent(self) -> list[Notification]:
        with self._lock:
            return list(self._sent)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "muted": self._mute,
                "notified": len(self._sent),
                "suppressed": self._suppressed,
                "dedupe_keys": len(self._seen),
                "min_interval_s": self.min_interval_s,
                "state_path": str(self.state_path) if self.state_path else None,
                "sinks": [
                    {"name": s.name, "available": bool(getattr(s, "available", True)),
                     "error": getattr(s, "last_error", None)}
                    for s in self.sinks
                ],
            }


def add_sinks(notifier: Notifier, *sinks: Sink) -> None:
    notifier.sinks.extend(sinks)


__all__ = [
    "ALERT_STATUSES",
    "Notification",
    "Sink",
    "CollectSink",
    "SoundSink",
    "ToastSink",
    "HudFlashSink",
    "Notifier",
    "add_sinks",
]
