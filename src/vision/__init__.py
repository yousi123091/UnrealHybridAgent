"""视觉子系统：几何、图像差异、屏幕定位、场景判断。

设计原则（贯穿本包）：
    * 能用算术得出确定结论的，绝不交给概率模型；
    * 视觉模型是**可选增强**，缺了它核心链路照样跑通；
    * 定位失败必须**报错**，不能悄悄返回一个猜出来的坐标。
"""

from .geometry import Rect, ScreenGeometry, grid_offsets
from .grounding import (
    Grounder,
    Grounding,
    ManualGrounder,
    VLMGrounder,
    WorldProjectionGrounder,
    resolve_grounder,
)
from .image import (
    DiffResult,
    PIL_AVAILABLE,
    decode_b64_png,
    diff,
    load_rgb,
    save_b64_png,
    save_rgb,
)
from .judge import (
    DEFAULT_GROUND_TOLERANCE,
    FloatingCandidate,
    Observation,
    judge_by_center,
    judge_floating,
    observations_from_actors,
    summarise,
)

__all__ = [
    "Rect",
    "ScreenGeometry",
    "grid_offsets",
    "Grounding",
    "Grounder",
    "WorldProjectionGrounder",
    "ManualGrounder",
    "VLMGrounder",
    "resolve_grounder",
    "DiffResult",
    "PIL_AVAILABLE",
    "decode_b64_png",
    "save_b64_png",
    "load_rgb",
    "save_rgb",
    "diff",
    "Observation",
    "FloatingCandidate",
    "DEFAULT_GROUND_TOLERANCE",
    "judge_floating",
    "judge_by_center",
    "observations_from_actors",
    "summarise",
]
