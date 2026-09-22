"""图像工具：base64 解码、保存、前后帧差异。

刻意**不引入**任何视觉模型：VLM 只是可选增强，核心验证必须能在
"没有模型"的情况下给出确定结论。这里的差异检测是纯像素运算，
结论可复现、可解释——这比"模型说好像变了"更适合做自动校验。
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from .geometry import Rect

try:  # pragma: no cover - 依赖环境
    from PIL import Image, ImageChops  # type: ignore

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageChops = None  # type: ignore[assignment]
    PIL_AVAILABLE = False


@dataclass
class DiffResult:
    """两帧之间的差异描述。"""

    changed_ratio: float          # 变化像素占全图比例
    bbox: Rect | None             # 变化区域外接矩形（无变化时为 None）
    size: tuple[int, int]         # 参与比较的（缩放后）尺寸
    max_delta: int                # 单通道最大差值

    @property
    def changed(self) -> bool:
        return self.changed_ratio > 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "changed_ratio": round(self.changed_ratio, 6),
            "bbox": self.bbox.as_dict() if self.bbox else None,
            "size": list(self.size),
            "max_delta": self.max_delta,
        }


def require_pillow() -> None:
    if not PIL_AVAILABLE:  # pragma: no cover
        from ..core.errors import PreconditionFailed

        raise PreconditionFailed("图像功能需要 pillow：pip install pillow")


def decode_b64_png(data: str) -> "Image.Image":
    """把 base64（可带 data URL 前缀）解成 PIL 图像。"""
    require_pillow()
    if "," in data[:64] and data[:64].lstrip().startswith("data:"):
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def save_b64_png(data: str, dest: str | Path) -> Path:
    """把 base64 PNG 落盘，返回绝对路径。"""
    require_pillow()
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if "," in data[:64] and data[:64].lstrip().startswith("data:"):
        data = data.split(",", 1)[1]
    path.write_bytes(base64.b64decode(data))
    return path.resolve()


def load_rgb(path: str | Path) -> "Image.Image":
    require_pillow()
    return Image.open(path).convert("RGB")


def save_rgb(image: "Image.Image", dest: str | Path) -> Path:
    require_pillow()
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path.resolve()


def diff(
    before: "Image.Image",
    after: "Image.Image",
    *,
    downscale_to_width: int = 320,
    changed_pixel_threshold: int = 12,
) -> DiffResult:
    """计算两帧差异。

    先等比缩小再逐像素比较——这样既快，又能天然忽略几个像素的抖动/抗锯齿噪声。
    """
    require_pillow()
    b = before.convert("RGB")
    a = after.convert("RGB")
    if a.size != b.size:
        a = a.resize(b.size)

    if downscale_to_width and b.width > downscale_to_width:
        scale = downscale_to_width / float(b.width)
        newsize = (max(1, int(b.width * scale)), max(1, int(b.height * scale)))
        b_small = b.resize(newsize)
        a_small = a.resize(newsize)
        # 记录缩放比，稍后把 bbox 映射回原图坐标
        ratio = b.width / float(newsize[0])
    else:
        b_small, a_small, ratio = b, a, 1.0

    gray_b = b_small.convert("L")
    gray_a = a_small.convert("L")
    delta = ImageChops.difference(gray_a, gray_b)

    px = delta.load()
    w, h = delta.size
    xs: list[int] = []
    ys: list[int] = []
    changed = 0
    max_delta = 0
    for y in range(h):
        for x in range(w):
            d = px[x, y]
            if d > max_delta:
                max_delta = d
            if d >= changed_pixel_threshold:
                changed += 1
                xs.append(x)
                ys.append(y)

    if not xs:
        return DiffResult(0.0, None, (w, h), max_delta)

    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    bbox = Rect(
        int(x0 * ratio), int(y0 * ratio),
        int((x1 - x0 + 1) * ratio), int((y1 - y0 + 1) * ratio),
    )
    return DiffResult(changed / float(w * h), bbox, (w, h), max_delta)


def crop(image: "Image.Image", rect: Rect) -> "Image.Image":
    require_pillow()
    box = (rect.x, rect.y, rect.right, rect.bottom)
    return image.crop(box)


__all__ = [
    "DiffResult",
    "PIL_AVAILABLE",
    "decode_b64_png",
    "save_b64_png",
    "load_rgb",
    "save_rgb",
    "diff",
    "crop",
]
