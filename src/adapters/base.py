"""适配器公共契约。

统一"UE 侧操作词汇表"（op vocabulary）。任何后端——不管是 Unreal MCP、
UE Python、Remote Control HTTP——都要实现同一组 op，名字和返回结构一致。
这样 Router 换后端时，上层 Skill 完全不需要改代码。

返回结构统一为 `{..., "ok": bool}`；失败抛 `core.errors.UHAError` 子类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# UE 侧统一操作名 —— 括号里是对应的 UE Python API，便于人工对照
OPS: dict[str, str] = {
    "get_actors": "列出 Level 中的 Actor（editor_actor_subsystem.get_all_level_actors）",
    "find_actor": "按名字/标签查找单个 Actor（get_actor_reference）",
    "get_actor_transform": "读取 transform（actor.get_actor_location / rotation / scale）",
    "set_actor_location": "设置位置（actor.set_actor_location）",
    "set_actor_rotation": "设置旋转（actor.set_actor_rotation）",
    "set_actor_scale": "设置缩放（actor.set_actor_scale3d）",
    "save_level": "保存当前 Level（editor_level_lib.save_current_level）",
    "execute_ue_python": "执行任意 UE Python（逃生舱）",
    "level_dirty_state": "查询 Level / 包是否有未保存修改（is_dirty 系列）",
    "level_file_stat": "当前 Level 磁盘文件的 path/mtime（保存落盘的进程外证据）",
    "current_level": "当前 Level 的包路径",
    "viewport_screenshot": "从 UE 内部抓视口截图（结构化截图，非桌面截图）",
}


@dataclass
class ActorRef:
    """一个 Actor 的最小标识。"""

    name: str
    label: str = ""
    class_name: str = ""
    path: str = ""
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActorRef":
        def vec(key: str, default: float) -> tuple[float, float, float]:
            raw = payload.get(key)
            if isinstance(raw, Mapping):
                return (
                    float(raw.get("x", default)),
                    float(raw.get("y", default)),
                    float(raw.get("z", default)),
                )
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                return (float(raw[0]), float(raw[1]), float(raw[2]))
            # 平铺字段形式 location_x / location_y / location_z
            if f"{key}_x" in payload:
                return (
                    float(payload.get(f"{key}_x", default)),
                    float(payload.get(f"{key}_y", default)),
                    float(payload.get(f"{key}_z", default)),
                )
            return (default, default, default)

        # 真实契约里 actor 的标识叫 `actor_label`（GenOrca v2.2.0 的 actor 域），
        # `label` 在 get_all_details 里只用于显示；两者都当成"名字"的可信来源。
        display = (
            payload.get("label")
            or payload.get("actor_label")
            or payload.get("name")
            or payload.get("actor_name")
            or ""
        )
        return cls(
            name=str(
                payload.get("name")
                or payload.get("actor_name")
                or payload.get("label")
                or payload.get("actor_label")
                or ""
            ),
            label=str(display),
            class_name=str(payload.get("class_name") or payload.get("class") or payload.get("type") or ""),
            path=str(payload.get("path") or payload.get("actor_path") or ""),
            location=vec("location", 0.0),
            rotation=vec("rotation", 0.0),
            scale=vec("scale", 1.0),
            extra={k: v for k, v in payload.items() if k not in {"name", "label", "class_name", "location", "rotation", "scale"}},
        )

    @property
    def z(self) -> float:
        return self.location[2]

    def as_dict(self, *, include_extra: bool = False) -> dict[str, Any]:
        """序列化。

        ``include_extra=True`` 会把 ``extra`` 里的原始字段（例如
        ``world_bounds_origin`` / ``world_bounds_extent``）合并出来。
        视觉判断需要包围盒才能算"底边离地高度"，默认丢掉会让它退化到
        精度更低的"中心点近似"——所以要用的时候必须显式要回来。
        """
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "class_name": self.class_name,
            "path": self.path,
            "location": list(self.location),
            "rotation": list(self.rotation),
            "scale": list(self.scale),
        }
        if include_extra:
            for key, value in self.extra.items():
                out.setdefault(key, value)
        return out


class UnrealBackend(ABC):
    """UE 侧后端抽象基类。"""

    #: 后端标识，会出现在日志的 `selected_method` 字段里
    name: str = "unreal_backend"

    #: 该后端声称支持的 op 集合；实际能力以 `capabilities()` 为准
    supported_ops: tuple[str, ...] = ()

    def __init__(self, *, timeout: float = 60.0, dry_run: bool = False):
        self.timeout = timeout
        self.dry_run = dry_run
        self._checks: dict[str, bool] = {}

    # -- 能力与可用性 ---------------------------------------------------------

    @abstractmethod
    def available(self) -> bool:
        """后端此刻能不能用（进程在吗、端口通吗、插件加载了吗）。

        必须**快速且无副作用**，会被 Router 频繁调用。
        """

    def capabilities(self) -> set[str]:
        """实际支持的 op。默认等于 `supported_ops`，子类可做动态探测。"""
        return set(self.supported_ops)

    def supports(self, op: str) -> bool:
        return op in self.capabilities()

    def require(self, op: str) -> None:
        if not self.supports(op):
            from ..core.errors import NotSupported

            raise NotSupported(f"{self.name} 不支持 {op}", details={"backend": self.name, "op": op})

    def diagnostics(self) -> dict[str, Any]:
        """给 `uha doctor` 用的自检信息。"""
        return {"backend": self.name, "available": self.available(), "capabilities": sorted(self.capabilities())}

    # -- 统一 op 接口（子类实现） --------------------------------------------

    def get_actors(self, *, name_like: str | None = None, limit: int = 500) -> list[ActorRef]:
        self.require("get_actors")
        raise NotImplementedError

    def find_actor(self, name: str) -> ActorRef | None:
        self.require("find_actor")
        raise NotImplementedError

    def get_actor_transform(self, name: str) -> ActorRef:
        self.require("get_actor_transform")
        raise NotImplementedError

    def set_actor_location(self, name: str, location: Iterable[float]) -> dict[str, Any]:
        self.require("set_actor_location")
        raise NotImplementedError

    def set_actor_rotation(self, name: str, rotation: Iterable[float]) -> dict[str, Any]:
        self.require("set_actor_rotation")
        raise NotImplementedError

    def set_actor_scale(self, name: str, scale: Iterable[float]) -> dict[str, Any]:
        self.require("set_actor_scale")
        raise NotImplementedError

    def save_level(self) -> dict[str, Any]:
        self.require("save_level")
        raise NotImplementedError

    def execute_ue_python(self, code: str) -> dict[str, Any]:
        self.require("execute_ue_python")
        raise NotImplementedError

    def level_dirty_state(self) -> dict[str, Any]:
        self.require("level_dirty_state")
        raise NotImplementedError

    def level_file_stat(self) -> dict[str, Any]:
        """当前 Level 的磁盘文件快照（``path`` / ``mtime`` / ``size``）。

        用来给"保存是否真的落盘"提供**进程外证据**。
        不支持的后端抛 `NotSupported`，验证器会如实报 skipped。
        """
        from ..core.errors import NotSupported

        raise NotSupported(f"{self.name} 不提供 Level 文件快照", details={"backend": self.name})

    def current_level(self) -> str:
        self.require("current_level")
        raise NotImplementedError

    def viewport_screenshot(
        self, *, width: int = 1280, height: int = 720, fov: float = 90.0
    ) -> dict[str, Any]:
        """UE 编辑器内部视口截图（区别于桌面截图）。

        返回 ``{"ok": bool, "images": [{"data": <base64>, "mimeType": "image/png"}]}``。
        不支持的后端应抛 `NotSupported`，让 Router 改走桌面截图。
        """
        from ..core.errors import NotSupported

        raise NotSupported(f"{self.name} 不支持 UE 内部视口截图", details={"backend": self.name})

    def call_native(self, op: str, **params: Any) -> dict[str, Any]:
        """调用后端独有的、不在统一 op 词汇表里的动作。

        例如 Unreal MCP 的 ``world_to_screen`` / ``screen_to_world``。
        默认不支持——这是**可选**能力，Router 不会依赖它。
        """
        from ..core.errors import NotSupported

        raise NotSupported(f"{self.name} 没有额外动作 {op}", details={"backend": self.name})

    def has_native(self, op: str) -> bool:
        return False

    def close(self) -> None:
        """释放资源。默认无操作。"""

    # -- 便利方法 ------------------------------------------------------------

    @staticmethod
    def _as_xyz(location: Iterable[float] | Mapping[str, float] | float, y: float | None = None, z: float | None = None) -> tuple[float, float, float]:
        """容忍多种入参形态：`(x,y,z)` / `[x,y,z]` / `{'x':..}` / `(x,y,z)` 拆开传。"""
        if isinstance(location, Mapping):
            return (float(location.get("x", 0.0)), float(location.get("y", 0.0)), float(location.get("z", 0.0)))
        if isinstance(location, (int, float)):
            return (float(location), float(y or 0.0), float(z or 0.0))
        seq = list(location)
        return (float(seq[0]), float(seq[1]), float(seq[2]))


__all__ = ["UnrealBackend", "ActorRef", "OPS"]
