"""Session GUI Calibration + Runtime Layout Validation。

原则：
  * **一次完整标定，多次低成本复用**——不是每点一次鼠标就重找整个 UE 界面。
  * 不写死分辨率/DPI/窗口位置；全部来自运行时探测 + session cache。
  * 布局验证失败（窗口移动/换显示器/DPI 变了）→ 自动重标定。

第三方：仅用 **pywin32**（`win32gui` / `win32api` / `win32con`），
本机 `.venv` 已装。不引入重型 UI-Automation 框架。
License: PSF-2.0 — https://github.com/mhammond/pywin32
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

try:  # pragma: no cover
    import win32api
    import win32con
    import win32gui
except ImportError:  # pragma: no cover
    win32api = None  # type: ignore
    win32con = None  # type: ignore
    win32gui = None  # type: ignore


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    @classmethod
    def from_any(cls, raw: Any) -> "Rect":
        if isinstance(raw, Rect):
            return raw
        if isinstance(raw, Mapping):
            return cls(int(raw["x"]), int(raw["y"]), int(raw["w"]), int(raw["h"]))
        if isinstance(raw, (list, tuple)) and len(raw) >= 4:
            return cls(int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
        return cls(0, 0, 0, 0)


@dataclass
class LayoutFingerprint:
    hwnd: int = 0
    window: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    dpi: float = 1.0
    monitor: str = ""
    foreground_is_ue: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "window": self.window.as_dict(),
            "dpi": self.dpi,
            "monitor": self.monitor,
            "foreground_is_ue": self.foreground_is_ue,
        }

    def same_layout(self, other: "LayoutFingerprint", *, pos_tol: int = 8, size_tol: int = 8) -> bool:
        if not self.hwnd or not other.hwnd:
            return False
        if self.hwnd != other.hwnd:
            return False
        if abs(self.window.x - other.window.x) > pos_tol or abs(self.window.y - other.window.y) > pos_tol:
            return False
        if abs(self.window.w - other.window.w) > size_tol or abs(self.window.h - other.window.h) > size_tol:
            return False
        if abs(float(self.dpi) - float(other.dpi)) > 0.05:
            return False
        if self.monitor and other.monitor and self.monitor != other.monitor:
            return False
        return True


@dataclass
class GuiCalibration:
    fingerprint: LayoutFingerprint
    rois: dict[str, Rect] = field(default_factory=dict)
    anchors: dict[str, tuple[int, int]] = field(default_factory=dict)
    focus_point: tuple[int, int] | None = None
    toolbar: Rect | None = None
    calibrated_at: float = 0.0
    source: str = "heuristic"
    cache_hits: int = 0
    recalibrations: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint.as_dict(),
            "rois": {k: v.as_dict() for k, v in self.rois.items()},
            "anchors": {k: list(v) for k, v in self.anchors.items()},
            "focus_point": list(self.focus_point) if self.focus_point else None,
            "toolbar": self.toolbar.as_dict() if self.toolbar else None,
            "calibrated_at": self.calibrated_at,
            "source": self.source,
            "cache_hits": self.cache_hits,
            "recalibrations": self.recalibrations,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GuiCalibration":
        fp_raw = raw.get("fingerprint") or {}
        win = Rect.from_any(fp_raw.get("window"))
        fp = LayoutFingerprint(
            hwnd=int(fp_raw.get("hwnd") or 0),
            window=win,
            dpi=float(fp_raw.get("dpi") or 1.0),
            monitor=str(fp_raw.get("monitor") or ""),
            foreground_is_ue=bool(fp_raw.get("foreground_is_ue")),
        )
        rois = {k: Rect.from_any(v) for k, v in (raw.get("rois") or {}).items()}
        anchors = {k: (int(v[0]), int(v[1])) for k, v in (raw.get("anchors") or {}).items() if v}
        fp_pt = raw.get("focus_point")
        return cls(
            fingerprint=fp,
            rois=rois,
            anchors=anchors,
            focus_point=(int(fp_pt[0]), int(fp_pt[1])) if fp_pt else None,
            toolbar=Rect.from_any(raw["toolbar"]) if raw.get("toolbar") else None,
            calibrated_at=float(raw.get("calibrated_at") or 0),
            source=str(raw.get("source") or "heuristic"),
            cache_hits=int(raw.get("cache_hits") or 0),
            recalibrations=int(raw.get("recalibrations") or 0),
        )


#: UE5 默认编辑器布局的**相对比例**（相对客户区）。随版本可能变，只作启发式初值；
#: 项目可通过 config `gui.layout_overrides` 覆盖，也可在标定后人工微调写回 session。
DEFAULT_UE_LAYOUT: dict[str, dict[str, float]] = {
    # x,y,w,h 均为 0~1 相对客户区
    "world_outliner": {"x": 0.01, "y": 0.08, "w": 0.16, "h": 0.55},
    "details_panel": {"x": 0.78, "y": 0.08, "w": 0.20, "h": 0.55},
    "viewport": {"x": 0.18, "y": 0.08, "w": 0.59, "h": 0.70},
    "toolbar": {"x": 0.0, "y": 0.02, "w": 1.0, "h": 0.05},
    "content_browser": {"x": 0.18, "y": 0.80, "w": 0.59, "h": 0.18},
}

UE_WINDOW_KEYWORDS = (
    "unreal editor", "unrealeditor", "ue5", "unreal",
    "虚幻引擎", "虚幻", "unreal engine",
)


def find_ue_window(title_keywords: tuple[str, ...] = UE_WINDOW_KEYWORDS) -> dict[str, Any] | None:
    """枚举顶层窗口，找到 UE Editor。返回 hwnd/title/rect。"""
    if win32gui is None:
        return None
    found: list[dict[str, Any]] = []

    def _cb(hwnd: int, _acc: Any) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd) or ""
            low = title.lower()
            if any(k in low for k in title_keywords):
                rect = win32gui.GetWindowRect(hwnd)
                found.append({
                    "hwnd": int(hwnd),
                    "title": title,
                    "rect": Rect(int(rect[0]), int(rect[1]), int(rect[2] - rect[0]), int(rect[3] - rect[1])),
                })
        except Exception:
            return

    win32gui.EnumWindows(_cb, None)
    if not found:
        return None
    # 优先标题含 "Unreal Editor" 的
    found.sort(key=lambda w: (0 if "unreal editor" in w["title"].lower() else 1, -w["rect"].w * w["rect"].h))
    return found[0]


def _client_rect(hwnd: int) -> Rect:
    if win32gui is None:
        return Rect(0, 0, 0, 0)
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    pt = win32gui.ClientToScreen(hwnd, (left, top))
    w = right - left
    h = bottom - top
    return Rect(int(pt[0]), int(pt[1]), int(w), int(h))


def _window_dpi(hwnd: int) -> float:
    try:
        if win32api is not None and hasattr(win32api, "GetDpiForWindow"):
            return float(win32api.GetDpiForWindow(hwnd)) / 96.0
    except Exception:
        pass
    try:
        if win32api is not None:
            hdc = win32gui.GetDC(hwnd)
            dpi = win32api.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            win32gui.ReleaseDC(hwnd, hdc)
            return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0


def _monitor_name(hwnd: int) -> str:
    try:
        if win32api is not None:
            hmon = win32api.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            info = win32api.GetMonitorInfo(hmon)
            return str(info.get("Device") or "")
    except Exception:
        pass
    return ""


def probe_layout(title_keywords: tuple[str, ...] = UE_WINDOW_KEYWORDS) -> LayoutFingerprint | None:
    win = find_ue_window(title_keywords)
    if not win:
        return None
    hwnd = win["hwnd"]
    fg = 0
    try:
        if win32gui is not None:
            fg = int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        fg = 0
    return LayoutFingerprint(
        hwnd=hwnd,
        window=win["rect"],
        dpi=_window_dpi(hwnd),
        monitor=_monitor_name(hwnd),
        foreground_is_ue=(fg == hwnd),
    )


def heuristic_rois(client: Rect, overrides: Mapping[str, Any] | None = None) -> dict[str, Rect]:
    """按 UE 默认布局比例从客户区推 ROI。可用 config 覆盖。"""
    layout = dict(DEFAULT_UE_LAYOUT)
    for k, v in (overrides or {}).items():
        if isinstance(v, Mapping) and {"x", "y", "w", "h"} <= set(v):
            layout[k] = {kk: float(v[kk]) for kk in ("x", "y", "w", "h")}
    out: dict[str, Rect] = {}
    for name, rel in layout.items():
        out[name] = Rect(
            x=client.x + int(client.w * float(rel["x"])),
            y=client.y + int(client.h * float(rel["y"])),
            w=max(1, int(client.w * float(rel["w"]))),
            h=max(1, int(client.h * float(rel["h"]))),
        )
    return out


class SessionGuiCalibrator:
    """会话级 GUI 标定缓存 + 布局验证。"""

    def __init__(
        self,
        cache_path: str | Path,
        *,
        title_keywords: tuple[str, ...] = UE_WINDOW_KEYWORDS,
        layout_overrides: Mapping[str, Any] | None = None,
        probe: Callable[[], LayoutFingerprint | None] | None = None,
    ):
        self.cache_path = Path(cache_path)
        self.title_keywords = title_keywords
        self.layout_overrides = dict(layout_overrides or {})
        self._probe = probe or probe_layout
        self.calibration: GuiCalibration | None = None
        self.metrics = {"cache_hits": 0, "recalibrations": 0, "layout_checks": 0}
        self._load_cache()

    def _load_cache(self) -> None:
        if self.cache_path.is_file():
            try:
                self.calibration = GuiCalibration.from_dict(
                    json.loads(self.cache_path.read_text(encoding="utf-8"))
                )
            except Exception:
                self.calibration = None

    def _save_cache(self) -> None:
        if self.calibration is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.calibration.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def layout_valid(self, fp: LayoutFingerprint | None = None) -> bool:
        """低成本验证：缓存标定是否仍适用当前窗口布局。"""
        self.metrics["layout_checks"] += 1
        if self.calibration is None:
            return False
        current = fp if fp is not None else self._probe()
        if current is None:
            return False
        return self.calibration.fingerprint.same_layout(current)

    def calibrate(self, *, force: bool = False, source: str = "heuristic") -> GuiCalibration:
        """完整标定。布局未变且非 force 时复用缓存。"""
        fp = self._probe()
        if fp is None:
            raise RuntimeError("未找到 UE Editor 窗口，无法标定（请先打开编辑器）")
        if not force and self.calibration is not None and self.calibration.fingerprint.same_layout(fp):
            self.calibration.cache_hits += 1
            self.metrics["cache_hits"] += 1
            self.calibration.fingerprint = fp  # 刷新 foreground 等
            return self.calibration

        client = fp.window
        if win32gui is not None:
            try:
                cand = _client_rect(fp.hwnd)
                if cand.w > 0 and cand.h > 0:
                    client = cand
            except Exception:
                client = fp.window
        if client.w <= 0 or client.h <= 0:
            client = fp.window
        rois = heuristic_rois(client, self.layout_overrides)
        focus = rois["viewport"].center if "viewport" in rois else client.center
        prev_hits = self.calibration.cache_hits if self.calibration else 0
        prev_recal = self.calibration.recalibrations if self.calibration else 0
        cal = GuiCalibration(
            fingerprint=fp,
            rois=rois,
            anchors={
                "viewport_center": focus,
                "outliner_search": (rois["world_outliner"].x + rois["world_outliner"].w // 2,
                                    rois["world_outliner"].y + min(24, rois["world_outliner"].h // 20)),
            },
            focus_point=focus,
            toolbar=rois.get("toolbar"),
            calibrated_at=time.time(),
            source=source,
            cache_hits=prev_hits,
            recalibrations=prev_recal + 1,
        )
        self.calibration = cal
        self.metrics["recalibrations"] += 1
        self._save_cache()
        return cal

    def ensure(self) -> GuiCalibration:
        """GUI 操作前调用：验证布局，失效则重标定。"""
        if self.calibration is not None and self.layout_valid():
            self.calibration.cache_hits += 1
            self.metrics["cache_hits"] += 1
            return self.calibration
        return self.calibrate(force=self.calibration is not None)

    def roi(self, panel: str) -> Rect:
        cal = self.ensure()
        if panel not in cal.rois:
            raise KeyError(f"未标定面板 ROI: {panel}")
        return cal.rois[panel]

    def focus_point(self) -> tuple[int, int]:
        cal = self.ensure()
        if not cal.focus_point:
            raise RuntimeError("标定结果缺少 focus_point")
        return cal.focus_point

    def stats(self) -> dict[str, Any]:
        return {
            **self.metrics,
            "has_cache": self.calibration is not None,
            "calibration": self.calibration.as_dict() if self.calibration else None,
        }


__all__ = [
    "Rect",
    "LayoutFingerprint",
    "GuiCalibration",
    "SessionGuiCalibrator",
    "find_ue_window",
    "probe_layout",
    "heuristic_rois",
    "DEFAULT_UE_LAYOUT",
]
