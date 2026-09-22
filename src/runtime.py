"""Runtime 装配：把配置变成一个"可用的后端集合"。

这一层只做三件事：
    1. 按配置把各个后端实例化（惰性——不连、不拉进程）；
    2. 提供 `BackendBundle.unreal_for(method)`：把 Router 选出的**执行方式**
       翻译成**具体的后端对象**；
    3. 统一释放资源。

它不参与决策。决策在 `router`，执意在 `scheduler`，验证在 `validation`。
分开的好处是：任何一层都可以单独测，换后端也不用改 Router。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .adapters.base import UnrealBackend
from .core.config import Config, load_config
from .core.errors import BackendUnavailable, NotSupported

#: 结构化后端（能读写 UE 内部状态）—— GUI 方式不在其中
STRUCTURED_METHODS = ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET")
#: GUI 方式（必须经过桌面）
GUI_METHODS = ("MOUSE", "KEYBOARD")
#: 全部方法，顺序照 Router 的枚举来
ALL_METHODS = ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "KEYBOARD", "MOUSE", "VISION", "HYBRID")


@dataclass
class BackendBundle:
    """一组已装配好的后端。"""

    unreal: dict[str, UnrealBackend] = field(default_factory=dict)
    computer_use: Any | None = None
    config: Config | None = None
    #: 惰性构造器，避免"一上来就连所有后端"
    _lazy: dict[str, Any] = field(default_factory=dict)
    gateway: Any | None = None
    assembly_errors: dict[str, str] = field(default_factory=dict)

    # -- 取用 ----------------------------------------------------------------

    def unreal_for(self, method: str) -> UnrealBackend:
        """把执行方式翻译成结构化后端对象。

        VISION / HYBRID 本身不是结构化方式，它们需要一个结构化通道来落"改动"，
        这里优先选**当前真的可用**的那个（避免 Router 选了 HYBRID，
        却被一个挂着不响的 MCP 挡住）。
        """
        if method in ("VISION", "HYBRID"):
            picked: str | None = None
            for candidate in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
                backend = self.unreal.get(candidate)
                if backend is None:
                    continue
                if picked is None:
                    picked = candidate  # 记下第一个装配了的，作为兜底
                try:
                    if backend.available():
                        picked = candidate
                        break
                except Exception:
                    continue
            if picked is None:
                raise NotSupported(
                    "HYBRID/VISION 需要一个结构化后端，但一个都没装配",
                    details={"available": sorted(self.unreal)},
                )
            method = picked
        backend = self.unreal.get(method)
        if backend is None:
            raise NotSupported(
                f"{method} 不是结构化执行方式或未装配",
                details={"available": sorted(self.unreal)},
            )
        return backend

    def unreal_or_none(self, method: str) -> UnrealBackend | None:
        try:
            return self.unreal_for(method)
        except NotSupported:
            return None

    def structured_backends(self) -> dict[str, UnrealBackend]:
        return {k: v for k, v in self.unreal.items() if k in STRUCTURED_METHODS}

    def gui_available(self) -> bool:
        cu = self.computer_use
        if cu is None:
            return False
        try:
            # 注意：connect() 失败时返回的是 {"available": False, ...} —— 这是个
            # **非空字典**，直接 bool() 会得到 True，把"不可达"误报成"可用"。
            # 必须读 available 字段本身。
            if not cu.connect(required=False).get("available"):
                return False
            cu.status()
            return True
        except Exception:
            return False

    # -- 诊断 ----------------------------------------------------------------

    def availability(self) -> dict[str, bool]:
        avail: dict[str, bool] = {}
        for name, backend in self.unreal.items():
            try:
                avail[name] = bool(backend.available())
            except Exception:
                avail[name] = False
        gui = self.gui_available()
        avail["MOUSE"] = gui
        avail["KEYBOARD"] = gui
        avail["VISION"] = gui
        # HYBRID 只要求"有一个结构化通道"：它的视觉部分可以退化成
        # UE 内部视口截图（结构化通道自带），并不必须依赖桌面 Computer Use。
        # 所以桌面不可用时 HYBRID 仍然可用——这正是它作为降级路径的价值。
        avail["HYBRID"] = any(avail.get(m) for m in STRUCTURED_METHODS)
        return avail

    def describe(self) -> dict[str, Any]:
        return {
            "provider_gateway": self.gateway.doctor() if self.gateway else None,
            "assembly_errors": dict(self.assembly_errors),
            "unreal_backends": {k: v.diagnostics() for k, v in self.unreal.items()},
            "computer_use": self._cu_diagnostics(),
            "availability": self.availability(),
        }

    def _cu_diagnostics(self) -> dict[str, Any]:
        cu = self.computer_use
        if cu is None:
            return {"configured": False}
        info: dict[str, Any] = {"configured": True}
        try:
            info["health"] = cu.health()
        except Exception as exc:
            info["health_error"] = f"{type(exc).__name__}: {exc}"
        return info

    # -- 释放 ----------------------------------------------------------------

    def close(self) -> None:
        for backend in self.unreal.values():
            try:
                backend.close()
            except Exception:
                pass
        if self.computer_use is not None:
            try:
                self.computer_use.close()
            except Exception:
                pass


def build_bundle(config: Config | None = None, *, only: tuple[str, ...] | None = None) -> BackendBundle:
    """按配置装配后端。

    ``only`` 可以只装其中几个（测试用），例如 ``only=("UNREAL_MCP",)``。

    ``desktop.dry_run`` 会注入到各后端：dry-run 时后端应拒绝真实副作用，
    并在 available() 上保持可路由（否则 dry-run 无法演示选路）。
    """
    cfg = config or load_config()
    bundle = BackendBundle(config=cfg)
    want = set(only) if only else None
    dry = bool(cfg.get("desktop.dry_run", False))

    def wanted(method: str) -> bool:
        return want is None or method in want

    # --- Unreal MCP ---------------------------------------------------------
    um = cfg.section("mcp_servers.unreal_mcp")
    if um.get("enabled", True) and wanted("UNREAL_MCP"):
        try:
            from .adapters.unreal.unreal_mcp import build_unreal_mcp_backend

            bundle.unreal["UNREAL_MCP"] = build_unreal_mcp_backend(um, dry_run=dry)
        except Exception as exc:  # Keep other providers available, preserve the reason.
            bundle.assembly_errors["UNREAL_MCP"] = f"{type(exc).__name__}: {exc}"

    # --- UE Python（官方 Remote Execution） ---------------------------------
    ue = cfg.section("ue")
    if ue.get("remote_exec", {}).get("enabled", True) and wanted("UE_PYTHON"):
        try:
            from .adapters.unreal.ue_python import UEPythonBackend

            rec = ue.get("remote_exec") or {}
            bundle.unreal["UE_PYTHON"] = UEPythonBackend(
                engine_root=Path(ue["engine_root"]) if ue.get("engine_root") else None,
                remote_exec_python_path=ue.get("remote_exec_python_path") or None,
                editor_exe=ue.get("editor_exe") or None,
                multicast_group=str(rec.get("multicast_group", "239.0.0.1")),
                multicast_port=int(rec.get("multicast_port", 6766)),
                multicast_ttl=int(rec.get("multicast_ttl", 0)),
                bind_address=str(rec.get("bind_address", "127.0.0.1")),
                discovery_timeout_s=float(rec.get("discovery_timeout_s", 3.0)),
                command_timeout_s=float(rec.get("command_timeout_s", 30.0)),
                project_file=ue.get("project_file") or None,
                timeout=float(rec.get("command_timeout_s", 30.0)),
                dry_run=dry,
            )
        except Exception as exc:
            bundle.assembly_errors["UE_PYTHON"] = f"{type(exc).__name__}: {exc}"

    # --- UE Commandlet（离线，编辑器不开时用） ------------------------------
    if ue.get("commandlet", {}).get("enabled", True) and wanted("UE_COMMANDLET") and ue.get("project_file"):
        try:
            from .adapters.unreal.commandlet import UECommandletBackend

            cm = ue.get("commandlet") or {}
            bundle.unreal["UE_COMMANDLET"] = UECommandletBackend(
                editor_cmd_exe=str(ue.get("editor_cmd_exe") or ""),
                project_file=str(ue.get("project_file") or ""),
                timeout_s=float(cm.get("timeout_s", 900)),
                extra_args=cm.get("extra_args") or None,
                dry_run=dry,
            )
        except Exception as exc:
            bundle.assembly_errors["UE_COMMANDLET"] = f"{type(exc).__name__}: {exc}"

    # --- Computer Use（桌面操作 + 截图） ------------------------------------
    cu_cfg = cfg.section("mcp_servers.computer_use")
    if cu_cfg.get("enabled", True) and (want is None or any(m in want for m in GUI_METHODS + ("VISION", "HYBRID"))):
        try:
            from .adapters.computer_use import build_from_config

            bundle.computer_use = build_from_config(
                cu_cfg,
                client_id=str(cfg.get("desktop.client_id") or "unreal-hybrid-agent"),
                dry_run=dry,
            )
        except Exception as exc:
            bundle.computer_use = None
            bundle.assembly_errors["DESKTOP"] = f"{type(exc).__name__}: {exc}"

    from .providers.gateway import ProviderGateway
    bundle.gateway = ProviderGateway()
    bundle.gateway.assembly_errors = bundle.assembly_errors
    for name, adapter in list(bundle.unreal.items()):
        bundle.unreal[name] = bundle.gateway.register(name, adapter)
    if bundle.computer_use is not None:
        bundle.gateway.register("DESKTOP", bundle.computer_use, family="desktop")
    from .vision.provider import HeuristicVisionProvider
    bundle.gateway.register("VISION_HEURISTIC", HeuristicVisionProvider(), family="vision")
    return bundle


def require_unreal_backend(bundle: BackendBundle, method: str) -> UnrealBackend:
    backend = bundle.unreal_for(method)
    if not backend.available():
        raise BackendUnavailable(
            f"{method} 后端当前不可用", details=backend.diagnostics()
        )
    return backend


__all__ = [
    "BackendBundle",
    "build_bundle",
    "require_unreal_backend",
    "ALL_METHODS",
    "STRUCTURED_METHODS",
    "GUI_METHODS",
]
