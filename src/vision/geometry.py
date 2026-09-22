"""屏幕几何：逻辑像素 / 物理像素、矩形与相交计算。

Agent-TARS 的 Computer Use 暴露的是**逻辑像素**坐标（例如 1707×1067 @150% 缩放），
而 UE 的 ``world_to_screen`` 返回的也是**逻辑像素**（编辑器视口像素，
它等于 UE 的 viewport 尺寸，通常就是逻辑分辨率减去窗口装饰）。

两边都在"逻辑像素"这一层，所以混合链路上不需要做 DPI 换算——
但必须显式记录 scale factor，一旦上游换了实现，这里能立刻发现不一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import PreconditionFailed


@dataclass(frozen=True)
class Rect:
    """整数像素矩形（左上原点）。"""

    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def expand(self, pad: int) -> "Rect":
        return Rect(self.x - pad, self.y - pad, self.w + 2 * pad, self.h + 2 * pad)

    def clamp(self, width: int, height: int) -> "Rect":
        x = max(0, min(self.x, width))
        y = max(0, min(self.y, height))
        w = max(0, min(self.w, width - x))
        h = max(0, min(self.h, height - y))
        return Rect(x, y, w, h)

    def iou(self, other: "Rect") -> float:
        ix = max(self.x, other.x)
        iy = max(self.y, other.y)
        ir = min(self.right, other.right)
        ib = min(self.bottom, other.bottom)
        inter = max(0, ir - ix) * max(0, ib - iy)
        union = self.area + other.area - inter
        return float(inter / union) if union else 0.0

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_any(cls, value: object) -> "Rect":
        """接受 ``[x,y,w,h]`` / ``{"x":..}`` 两种形态。"""
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            return cls(int(value[0]), int(value[1]), int(value[2]), int(value[3]))
        if isinstance(value, dict):
            return cls(
                int(value.get("x", 0)), int(value.get("y", 0)),
                int(value.get("w", 0)), int(value.get("h", 0)),
            )
        raise PreconditionFailed(f"无法解析矩形: {value!r}")


@dataclass
class ScreenGeometry:
    """屏幕坐标系描述。"""

    width: int
    height: int
    scale_factor: float = 1.0

    @property
    def physical_width(self) -> int:
        return int(round(self.width * self.scale_factor))

    @property
    def physical_height(self) -> int:
        return int(round(self.height * self.scale_factor))

    def clamp_to_screen(self, x: int, y: int, *, margin: int = 2) -> tuple[int, int]:
        if self.width <= 0 or self.height <= 0:
            raise PreconditionFailed(f"屏幕尺寸非法: {self.width}x{self.height}")
        return (
            max(margin, min(int(x), self.width - 1 - margin)),
            max(margin, min(int(y), self.height - 1 - margin)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "logical": [self.width, self.height],
            "physical": [self.physical_width, self.physical_height],
            "scale_factor": self.scale_factor,
        }


def grid_offsets(radius: int, step: int) -> list[tuple[int, int]]:
    """以中心为原点生成螺旋偏移，用于"点偏了就往旁边挪一格"的容错点击。"""
    offsets: list[tuple[int, int]] = [(0, 0)]
    r = step
    while r <= radius:
        for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r), (r, r), (-r, -r), (r, -r), (-r, r)):
            offsets.append((dx, dy))
        r += step
    return offsets


__all__ = ["Rect", "ScreenGeometry", "grid_offsets"]
