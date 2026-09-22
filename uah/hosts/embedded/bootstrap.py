"""UHA 接入引导 —— ``uha.py`` 只需要调用两个函数。

``uah_begin_boot(cfg, log)``  在装配后端**之前**调用 → 发真实的 STARTING
``uah_attach(cfg, log, executor, boot)`` 在 Executor 建好**之后**调用 → 挂观察者

为什么要分成两次：UHA 的 ``_setup()`` 里 ``build_bundle()`` 会去连 MCP /
Computer Use，那几秒是**真的**在启动。把它报成 STARTING 是诚实的，
也是 Test 1（IDLE → STARTING → RUNNING）能真实观察到的唯一办法。

**这两次调用都不可能抛异常。** 最外层都有 try/except，失败只往日志写一行。
需求 §十五：UAH 的加入不能阻塞、不能破坏、不能改变 UHA 的现有行为。
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...adapters.publisher import HttpPublisher, LocalPublisher, Publisher
from ...adapters.uha.adapter import UhaNativeAdapter, attach_approval_observer, attach_plan_progress
from ...core.models import ProjectRef
from ...core.protocol import PROTOCOL_VERSION, UAH_CORE_VERSION
from ...core.transport import DEFAULT_HUB_HOST, DEFAULT_HUB_PORT, ensure_hub, hub_url
from ...ui import notify as notify_mod
from ..desktop.hud import default_state_path  # noqa: F401 - 复用同一个提醒配置文件

#: 配置默认值。写在代码里，让"没有 uah 段"的旧配置也能用。
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "local_approval": {"enabled": False, "timeout_s": 60},
    "host": DEFAULT_HUB_HOST,
    "port": DEFAULT_HUB_PORT,
    "agent_id": "uha",
    "agent_name": "UHA",
    "agent_type": "uha",
    "phase_label": "Phase 4B",
    "heartbeat_s": 10.0,
    "auto_start_desktop_hud": False,
    "embedded": {"enabled": True, "console": False, "log": True},
    "notify": {"sound": True, "toast": False},
    "stale_after_s": 20.0,
}


@dataclass
class UahBoot:
    """UAH 侧的全部对象。谁都不想让它跑到模块全局去。"""

    enabled: bool = False
    reason: str = ""
    url: str = ""
    hub_server: Any = None
    store: Any = None
    publisher: Publisher | None = None
    adapter: UhaNativeAdapter | None = None
    embedded: Any = None
    notifier: Any = None
    info: dict[str, Any] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        out = {"enabled": self.enabled, "reason": self.reason, "url": self.url, **self.info}
        if self.adapter is not None:
            out["adapter"] = self.adapter.stats()
        if self.embedded is not None:
            out["embedded"] = self.embedded.stats()
        if self.hub_server is not None:
            out["hub"] = self.hub_server.health()
        return out


_BOOT_LOCK = threading.Lock()
_ACTIVE: UahBoot | None = None
UAH_RELEASE_ENABLED = False


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


def _section(cfg: Any) -> dict[str, Any]:
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    raw = None
    try:
        raw = cfg.get("uah") if cfg is not None else None
    except Exception:  # noqa: BLE001
        raw = None
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
    return merged


def _project_from_config(cfg: Any) -> ProjectRef:
    """项目名/路径取自 UHA 的真实配置，不猜。"""
    project_file = ""
    try:
        project_file = str(cfg.get("ue.project_file") or "")
    except Exception:  # noqa: BLE001
        project_file = ""
    if not project_file:
        return ProjectRef()
    path = Path(project_file)
    return ProjectRef(name=path.stem or None, path=str(path.parent))


# ---------------------------------------------------------------------------
# 第一阶段：装配前的 STARTING
# ---------------------------------------------------------------------------


def uah_begin_boot(cfg: Any, log: Any = None) -> UahBoot | None:
    """起 Hub / 适配器，并发出一条真实的 STARTING。失败返回 ``None``（永不抛）。"""
    try:
        return _begin_boot(cfg, log)
    except Exception as exc:  # noqa: BLE001
        _log(log, "uah_boot_error", error=f"{type(exc).__name__}: {exc}"[:300])
        return None


def _begin_boot(cfg: Any, log: Any) -> UahBoot | None:
    # Publication scope: UAH is deferred; configuration cannot re-enable it.
    if not UAH_RELEASE_ENABLED:
        return UahBoot(enabled=False, reason="UAH deferred to the next release")
    if env_disabled():
        return UahBoot(enabled=False, reason="环境变量 UAH_ENABLED=0")
    sect = _section(cfg)
    if not sect.get("enabled"):
        return UahBoot(enabled=False, reason="配置 uah.enabled=false")

    host = str(sect.get("host") or DEFAULT_HUB_HOST)
    port = int(sect.get("port") or DEFAULT_HUB_PORT)

    # 提醒器：与桌面宿主共用同一份静音配置
    notify_cfg = sect.get("notify") or {}
    notifier = notify_mod.Notifier(
        sinks=[
            notify_mod.SoundSink() if notify_cfg.get("sound", True) else _OffSink(),
            notify_mod.ToastSink(enabled=bool(notify_cfg.get("toast", False))),
        ],
        state_path=default_state_path(),
    )

    hub_server = None
    try:
        from ...core.daemon import ensure_persistent_hub
        url = ensure_persistent_hub(host=host, port=port)
    except OSError as exc:
        # 端口被别人占了：**不**降级切换端口（会让 HUD 连错地方），直接关掉 UAH
        return UahBoot(enabled=False, url=hub_url(host, port),
                       reason=f"hub 端口不可用：{exc}")

    if hub_server is not None:
        store = hub_server.store
        inner: Publisher = LocalPublisher(hub_server)
        topology = "in-process hub"
    else:
        from ...core.state import StateStore

        store = StateStore(stale_after_s=float(sect.get("stale_after_s") or 20.0))
        inner = HttpPublisher(url)
        topology = "client of external hub"

    adapter = UhaNativeAdapter(
        inner,
        agent_id=str(sect.get("agent_id") or "uha"),
        agent_name=str(sect.get("agent_name") or "UHA"),
        agent_type=str(sect.get("agent_type") or "uha"),
        project=_project_from_config(cfg),
        phase_label=str(sect.get("phase_label") or "") or None,
        heartbeat_s=float(sect.get("heartbeat_s") or 10.0),
    )

    boot = UahBoot(
        enabled=True, url=url, hub_server=hub_server, store=store,
        publisher=inner, adapter=adapter, notifier=notifier,
        info={"topology": topology, "protocol": PROTOCOL_VERSION, "core": UAH_CORE_VERSION},
    )

    # 内嵌宿主：包在发布链上，同一个状态机 + 同一套渲染
    emb_cfg = sect.get("embedded") or {}
    if emb_cfg.get("enabled", True):
        from .host import EmbeddedHud

        boot.embedded = EmbeddedHud(
            inner,
            store=store,
            logger=log if emb_cfg.get("log", True) else None,
            console=bool(emb_cfg.get("console", False)),
            notifier=notifier,
        )
        boot.adapter.publisher = boot.embedded

    # ★ 真实的 STARTING：从这一刻起 UHA 正在连通道
    boot.adapter.start()
    _log(log, "uah_boot", url=url, topology=topology,
         agent_id=boot.adapter.agent.id, embedded=boot.embedded is not None)

    with _BOOT_LOCK:
        global _ACTIVE
        _ACTIVE = boot
    return boot


# ---------------------------------------------------------------------------
# 第二阶段：挂观察者
# ---------------------------------------------------------------------------


def uah_attach(cfg: Any, log: Any, executor: Any, boot: UahBoot | None = None) -> UahBoot | None:
    """把 UAH 挂到 Executor 的观测点上。**不改任何 UHA 行为。**"""
    try:
        return _attach(cfg, log, executor, boot)
    except Exception as exc:  # noqa: BLE001
        _log(log, "uah_attach_error", error=f"{type(exc).__name__}: {exc}"[:300])
        return boot


def _attach(cfg: Any, log: Any, executor: Any, boot: UahBoot | None) -> UahBoot | None:
    if boot is None:
        boot = _ACTIVE
    if boot is None or not boot.enabled or boot.adapter is None:
        return boot

    adapter = boot.adapter
    sect = _section(cfg)

    # 1) 会话状态：订阅 UHA 已有的 on_change（事件驱动，不轮询）
    controller = getattr(executor, "controller", None)
    if controller is None:
        try:
            from src.core.session_control import get_controller  # type: ignore

            controller = get_controller()
        except Exception:  # noqa: BLE001
            controller = None
    if controller is not None:
        adapter.attach_controller(controller, emit_now=True)

    # 2) 计划进度（Step i / N）：旁路包裹，返回值原样透传
    plan_info = attach_plan_progress(adapter, executor)

    local_approval = sect.get("local_approval") or {}
    gate = getattr(executor, "approval", None)
    if local_approval.get("enabled") is True and gate is not None and gate._confirmer is None:
        from ...core.approval import make_confirmer
        gate._confirmer = make_confirmer(boot.url, adapter.agent.id, local_approval.get("timeout_s", 60))
        boot.info["local_approval"] = True

    # 3) 审批等待：旁路包裹，决策原样透传
    approval_info = attach_approval_observer(adapter, executor)

    boot.info["plan_hooks"] = plan_info
    boot.info["approval_hooks"] = approval_info
    boot.info["attached"] = True

    # 4) 进程退出时明确发一条 agent.stopped（否则 HUD 只能等心跳过期）
    atexit.register(_atexit_stop, boot)
    _install_signal_note(boot)

    # 5) 可选：自动拉起独立桌面 HUD（默认关，测试/CI 不希望冒窗口）
    if sect.get("auto_start_desktop_hud"):
        try:
            from ..desktop.launch import spawn_hud

            pid = spawn_hud(boot.url)
            boot.info["desktop_hud_pid"] = pid
        except Exception as exc:  # noqa: BLE001
            boot.info["desktop_hud_error"] = f"{type(exc).__name__}: {exc}"[:200]

    _log(log, "uah_attach", **{k: v for k, v in boot.info.items() if k != "attached"})
    return boot


def uah_shutdown(boot: UahBoot | None = None, *, reason: str = "shutdown") -> None:
    """优雅收尾：发 agent.stopped、排空队列、停掉自建的 Hub。"""
    target = boot or _ACTIVE
    if target is None or not target.enabled:
        return
    adapter = target.adapter
    if adapter is not None:
        try:
            adapter.stop(reason=reason)
            adapter.flush(1.0)
        except Exception:  # noqa: BLE001
            pass
    server = target.hub_server
    if server is not None:
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass


def active_boot() -> UahBoot | None:
    return _ACTIVE


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


class _OffSink(notify_mod.Sink):
    name = "off"
    available = False

    def send(self, note: Any) -> None:
        return None


_STOPPED = threading.Event()


def _atexit_stop(boot: UahBoot) -> None:
    if _STOPPED.is_set():
        return
    _STOPPED.set()
    uah_shutdown(boot, reason="process exit")


def _install_signal_note(boot: UahBoot) -> None:
    """Ctrl+C 时也把 agent.stopped 发出去（atexit 在 SIGINT 下不一定跑到）。"""
    try:
        import signal

        previous = signal.getsignal(signal.SIGINT)

        def handler(signum: int, frame: Any) -> None:
            uah_shutdown(boot, reason=f"signal {signum}")
            if callable(previous):
                previous(signum, frame)
            else:
                raise KeyboardInterrupt

        signal.signal(signal.SIGINT, handler)
    except Exception:  # noqa: BLE001 - 非主线程 / 不支持时不装，不影响主流程
        pass


def _log(log: Any, kind: str, **fields: Any) -> None:
    if log is None:
        return
    try:
        log.event(kind, **fields)
    except Exception:  # noqa: BLE001
        pass
    try:
        if kind.endswith("error"):
            log.warn(f"[UAH] {kind}: {fields}")
    except Exception:  # noqa: BLE001
        pass


def env_disabled() -> bool:
    """``UAH_ENABLED=0`` 可在一处彻底关掉（排障用）。"""
    return str(os.environ.get("UAH_ENABLED", "1")).strip().lower() in ("0", "false", "no", "off")


__all__ = [
    "UahBoot",
    "uah_begin_boot",
    "uah_attach",
    "uah_shutdown",
    "active_boot",
    "DEFAULTS",
    "env_disabled",
]
