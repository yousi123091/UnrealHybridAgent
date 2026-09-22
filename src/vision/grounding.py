"""把"要操作的东西"落到屏幕坐标上。

三条路，按可靠性排序（**不用模型**是默认路线）：

1. ``WorldProjectionGrounder``  —— 世界坐标 → UE 视口像素（``util.world_to_screen``）。
   只要 UE 开着，这是**精确**投影，比任何模型猜的都准。
2. ``ManualGrounder``          —— 调用方直接给像素坐标（最原始但永不失效）。
3. ``VLMGrounder``            —— 可选：接 VLM 做"截图里那个漂浮的方块"这类语义定位。
   当前 Computer Use 服务 ``vlmConfigured=false``，所以它默认不可用，
   并且**明确报错**而不是悄悄返回一个瞎猜的坐标。

设计原则：宁可 fail loud，也不 fail silent —— 一个错位的点击会造成
不可预期的编辑器状态变更，比直接报错危险得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..core.errors import NotSupported, PreconditionFailed
from .geometry import Rect, ScreenGeometry


@dataclass
class Grounding:
    """一次定位结果。"""

    screen_xy: tuple[int, int]
    rect: Rect | None = None
    confidence: float = 1.0
    source: str = "unknown"
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "screen_xy": list(self.screen_xy),
            "rect": self.rect.as_dict() if self.rect else None,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "detail": self.detail or {},
        }


class Grounder(Protocol):
    def available(self) -> bool: ...

    def locate(self, target: str, **kwargs: Any) -> Grounding: ...


class WorldProjectionGrounder:
    """用 UE 的 ``world_to_screen`` 把世界坐标投影到视口像素。

    这是混合链路的"精确一端"：不需要视觉模型，也不需要猜。
    """

    name = "world_projection"

    def __init__(self, backend: Any):
        self.backend = backend

    def available(self) -> bool:
        try:
            return bool(getattr(self.backend, "has_native", lambda _op: False)("world_to_screen"))
        except Exception:
            return False

    def locate(
        self,
        target: str,
        *,
        world_location: tuple[float, float, float] | None = None,
        screen: ScreenGeometry | None = None,
        **_: Any,
    ) -> Grounding:
        if world_location is None:
            raise PreconditionFailed("WorldProjectionGrounder 需要 world_location")
        if not self.available():
            raise NotSupported(f"{getattr(self.backend, 'name', '?')} 不提供 world_to_screen")
        res = self.backend.call_native("world_to_screen", location=list(world_location))
        xy = _extract_xy(res)
        if xy is None:
            raise PreconditionFailed(f"world_to_screen 未返回像素坐标: {res}")
        x, y = xy
        if screen is not None:
            x, y = screen.clamp_to_screen(x, y)
        return Grounding(
            screen_xy=(int(x), int(y)),
            confidence=0.95,
            source=self.name,
            detail={"world_location": list(world_location), "raw": _small(res)},
        )


class ManualGrounder:
    """调用方直接给坐标。"""

    name = "manual"

    def __init__(self, xy: tuple[int, int] | None = None):
        self.xy = xy

    def available(self) -> bool:
        return self.xy is not None

    def locate(self, target: str, *, xy: tuple[int, int] | None = None, **_: Any) -> Grounding:
        point = xy or self.xy
        if point is None:
            raise PreconditionFailed(f"ManualGrounder 没有 {target} 的坐标")
        return Grounding(screen_xy=(int(point[0]), int(point[1])), confidence=1.0, source=self.name)


class VLMGrounder:
    """可选：让 VLM 在截图里找目标。

    没有配置 VLM 时**必须**抛错。悄悄降级成"返回屏幕中心"是最糟的选择：
    调用方会以为定位成功，然后点错地方。
    """

    name = "vlm"

    def __init__(self, computer_use: Any = None, *, configured: bool | None = None):
        self.computer_use = computer_use
        self._configured = configured

    def configured(self) -> bool:
        if self._configured is not None:
            return self._configured
        try:
            health = self.computer_use.health() if self.computer_use else {}
        except Exception:
            return False
        return bool(health.get("vlmConfigured"))

    def available(self) -> bool:
        return self.configured()

    def locate(self, target: str, **_: Any) -> Grounding:
        if not self.available():
            raise NotSupported(
                "VLM 未配置（Agent-TARS health.vlmConfigured=false）："
                "无法做语义屏幕定位，请改用 world_to_screen 投影或显式坐标"
            )
        raise NotSupported("VLM 定位通道尚未接入（预留接口）")


def resolve_grounder(
    *,
    backend: Any = None,
    world_location: tuple[float, float, float] | None = None,
    xy: tuple[int, int] | None = None,
    computer_use: Any = None,
    allow_vlm: bool = False,
) -> Grounding | None:
    """按可靠性顺序挑一个能用的 grounder 并立即定位。

    全部不可用时返回 ``None``（调用方据此改走别的链路，而不是报错崩掉）。
    """
    candidates: list[Any] = []
    if backend is not None and world_location is not None:
        candidates.append(WorldProjectionGrounder(backend))
    if xy is not None:
        candidates.append(ManualGrounder(xy))
    if allow_vlm:
        candidates.append(VLMGrounder(computer_use))

    last_error: Exception | None = None
    for g in candidates:
        try:
            if not g.available():
                continue
            if isinstance(g, WorldProjectionGrounder):
                return g.locate("actor", world_location=world_location)
            return g.locate("actor")
        except Exception as exc:  # noqa: BLE001 - 逐个降级
            last_error = exc
            continue
    _ = last_error
    return None


def _extract_xy(res: Any) -> tuple[int, int] | None:
    """从各种返回形态里捞出 (x, y)。"""
    if not isinstance(res, dict):
        return None
    for container in (res, res.get("result"), res.get("data"), res.get("location")):
        if not isinstance(container, dict):
            continue
        if "x" in container and "y" in container:
            try:
                return int(round(float(container["x"]))), int(round(float(container["y"])))
            except (TypeError, ValueError):
                continue
        for key in ("screen", "pixel", "screen_position", "viewport"):
            val = container.get(key)
            if isinstance(val, (list, tuple)) and len(val) >= 2:
                try:
                    return int(round(float(val[0]))), int(round(float(val[1])))
                except (TypeError, ValueError):
                    continue
    return None


def _small(obj: Any, limit: int = 400) -> Any:
    text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "Grounding",
    "Grounder",
    "WorldProjectionGrounder",
    "ManualGrounder",
    "VLMGrounder",
    "resolve_grounder",
]
