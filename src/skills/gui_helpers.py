"""GUI 降级链路的原语。

结构化通道（Unreal MCP / UE Python）挂掉时，只剩鼠标键盘。
这里把"点哪儿、敲什么"封成可复用原语，并贯彻两条纪律：

1. **ROI 必须在配置里显式标定**（``vision.roi.world_outliner`` 等）。
   没有标定就直接 ``NotSupported`` —— 猜坐标点下去会改坏工程，
   报错只损失一次尝试。所有 ROI 用到的场合都会顺手截一张图存进 artifacts，
   方便对着图把 ROI 标出来。
2. **坐标一律来自配置或投影计算**，绝不用"屏幕正中""大概右上角"这种默认值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core.errors import NotSupported, PreconditionFailed
from ..vision.geometry import Rect
from ..vision.image import diff, load_rgb

#: 明确需要标定的面板名
REQUIRED_ROIS = ("world_outliner", "details_panel", "viewport", "toolbar")


def resolve_roi(config: Any, panel: str) -> Rect:
    """从配置取面板 ROI；缺失时尝试 Session GUI Calibration；仍缺失则拒绝执行。"""
    raw = config.get(f"vision.roi.{panel}")
    if raw:
        rect = Rect.from_any(raw)
        if rect.w > 0 and rect.h > 0:
            return rect
        raise PreconditionFailed(f"vision.roi.{panel} 尺寸非法: {rect}")

    # Session Calibration：一次完整标定，多次布局校验复用（不写死分辨率）
    try:
        from ..desktop.calibration import SessionGuiCalibrator
        from pathlib import Path as _P

        cache = config.get("desktop.gui_cache_file") or ".state/gui_session.json"
        cache_path = config.path("desktop.gui_cache_file") if config.get("desktop.gui_cache_file") \
            else _P(str(cache))
        if not getattr(cache_path, "is_absolute", lambda: False)():
            cache_path = _P(str(cache_path))
        cal = SessionGuiCalibrator(
            cache_path,
            layout_overrides=config.get("gui.layout_overrides") or {},
        )
        roi = cal.roi(panel)
        if roi.w > 0 and roi.h > 0:
            return roi
    except Exception:
        pass

    raise NotSupported(
        f"未标定 {panel} 面板的 ROI（vision.roi.{panel}）——"
        "GUI 降级链路需要显式坐标或 Session Calibration。"
        "请运行 `uha calibrate` 或对着 `uha roi-shot` 截图手标后再用。",
        details={"panel": panel, "hint": "python uha.py calibrate  /  python uha.py roi-shot"},
    )


def desktop_screenshot(ctx: Any, name: str = "screen.png", *, max_width: int = 0) -> tuple[Any, Path]:
    """桌面截图（经 Computer Use）。返回 (Screenshot, 落盘路径)。"""
    cu = ctx.gui()
    shot = cu.screenshot(max_width=max_width) if max_width else cu.screenshot()
    path = ctx.save_screenshot(shot.image, name)
    return shot, path


def ctrl_s_save(ctx: Any) -> dict[str, Any]:
    """Ctrl+S 保存。必须能保证按键落在 UE 窗口。

    优先级：
      1. config `desktop.editor_focus_point`（人工标定）
      2. Session GUI Calibration 的 focus_point（viewport 中心）
      3. 都没有 -> 拒绝盲发，干净降级到保存 API
    """
    cu = ctx.gui()
    raw = ctx.config.get("desktop.editor_focus_point")
    focus_point: tuple[int, int] | None = None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        focus_point = (int(raw[0]), int(raw[1]))
    else:
        try:
            from ..desktop.calibration import SessionGuiCalibrator

            cache = ctx.config.get("desktop.gui_cache_file") or ".state/gui_session.json"
            cal = SessionGuiCalibrator(cache)
            focus_point = cal.focus_point()
        except Exception:
            focus_point = None

    if focus_point is None:
        raise NotSupported(
            "未标定 desktop.editor_focus_point / Session Calibration："
            "无法保证 Ctrl+S 落在 UE 窗口。拒绝把快捷键盲发给未知前台窗口。"
            "请先 `python uha.py calibrate`，或在配置里填 focus point；"
            "或改用结构化保存 API。"
        )
    from ..desktop.calibration import find_ue_window
    import ctypes
    win = find_ue_window()
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    if not win or user32.GetForegroundWindow() != win['hwnd']:
        raise PreconditionFailed('UE must be the actual foreground window before GUI save')
    rect = win['rect']
    if not (rect.x <= focus_point[0] < rect.x+rect.w and rect.y <= focus_point[1] < rect.y+rect.h):
        raise PreconditionFailed('Saved GUI focus point is outside the current UE window')
    previous = getattr(cu, 'expected_window', None)
    cu.expected_window = win['hwnd']
    try:
        with cu.control():
            res = cu.save_with_ctrl_s(focus_point=focus_point)
    finally:
        cu.expected_window = previous
    res.setdefault("ok", True)
    res["_gui"] = "ctrl+s"
    return res


def outliner_search(ctx: Any, name: str, *, clear_first: bool = True) -> dict[str, Any]:
    """在 World Outliner 搜索框里输入 Actor 名。

    步骤：点搜索框 → 清空 → 输入名字 → 截图留证。
    只负责"把列表筛出来"，点不点结果由调用方决定。
    """
    roi = resolve_roi(ctx.config, "world_outliner")
    cu = ctx.gui()
    # 搜索框通常贴在面板顶部，取面板顶边往下一小段作为搜索行
    search_y = roi.y + min(24, max(8, roi.h // 20))
    search_x = roi.x + roi.w // 2

    with cu.control():
        cu.click(search_x, search_y)
        if clear_first:
            cu.hotkey("ctrl a")
            cu.key_press("delete")
        cu.type_text(name)
        cu.wait(400)

    shot, path = desktop_screenshot(ctx, f"outliner_search_{_slug(name)}.png")
    return {
        "ok": True,
        "action": "outliner_search",
        "query": name,
        "clicked": [search_x, search_y],
        "roi": roi.as_dict(),
        "screenshot": str(path),
        "_screenshot": shot,
    }


def click_in_roi(ctx: Any, panel: str, *, rel_x: float, rel_y: float, name: str = "gui_click") -> dict[str, Any]:
    """按面板内相对位置点击（0~1 相对面板宽高）。"""
    roi = resolve_roi(ctx.config, panel)
    x = roi.x + int(roi.w * rel_x)
    y = roi.y + int(roi.h * rel_y)
    cu = ctx.gui()
    with cu.control():
        cu.click(x, y)
    return {"ok": True, "panel": panel, "xy": [x, y], "relative": [rel_x, rel_y], "_name": name}


def set_details_location_z(ctx: Any, value: float, *, field_rel: Mapping[str, float] | None = None) -> dict[str, Any]:
    """在 Details 面板的 Location Z 输入框里改值。

    ``field_rel`` 是 Z 输入框相对 Details 面板的**相对坐标**（0~1），
    默认取常见布局（Location 行第 3 个框）。这个布局随 UE 版本/面板宽度变化，
    所以做成可配置——标定一次即可长期复用。
    """
    roi = resolve_roi(ctx.config, "details_panel")
    rel = dict(field_rel or ctx.config.get("vision.details_location_z_rel") or {"x": 0.62, "y": 0.075})
    x = roi.x + int(roi.w * float(rel.get("x", 0.62)))
    y = roi.y + int(roi.h * float(rel.get("y", 0.075)))

    cu = ctx.gui()
    with cu.control():
        cu.click(x, y)
        cu.hotkey("ctrl a")
        cu.type_text(f"{float(value):.3f}")
        cu.key_press("enter")
        cu.wait(250)

    shot, path = desktop_screenshot(ctx, "details_z_set.png")
    return {
        "ok": True,
        "action": "details_location_z",
        "value": float(value),
        "clicked": [x, y],
        "roi": roi.as_dict(),
        "screenshot": str(path),
        "_screenshot": shot,
    }


def compare_screenshots(before_path: Path, after_path: Path, *, min_ratio: float = 0.002) -> dict[str, Any]:
    """比较两张已落盘截图（纯像素运算）。"""
    try:
        res = diff(load_rgb(before_path), load_rgb(after_path),
                   downscale_to_width=320, changed_pixel_threshold=12)
    except Exception as exc:  # noqa: BLE001 - 图像不可用时如实返回
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": res.changed_ratio >= min_ratio,
        "changed_ratio": round(res.changed_ratio, 6),
        "bbox": res.bbox.as_dict() if res.bbox else None,
        "min_ratio": min_ratio,
    }


def _slug(text: str, limit: int = 40) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)
    return out[:limit] or "x"


def describe_roi_status(config: Any) -> dict[str, Any]:
    """给 `uha doctor` 用：哪些面板标定过、哪些没标。"""
    status: dict[str, Any] = {}
    for panel in REQUIRED_ROIS:
        raw = config.get(f"vision.roi.{panel}")
        status[panel] = Rect.from_any(raw).as_dict() if raw else None
    return status


__all__ = [
    "REQUIRED_ROIS",
    "resolve_roi",
    "desktop_screenshot",
    "ctrl_s_save",
    "outliner_search",
    "click_in_roi",
    "set_details_location_z",
    "compare_screenshots",
    "describe_roi_status",
]
