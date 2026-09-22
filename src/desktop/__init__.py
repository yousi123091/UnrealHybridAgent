"""Desktop / GUI 运行时能力包。

* calibration —— Session GUI Calibration + Layout Validation（pywin32）
* hotkey      —— Emergency Stop 热键 + 键鼠释放（ctypes，零第三方）
* overlay     —— Control Overlay（tkinter，可选；缺失时静默降级）
"""

from .calibration import (
    GuiCalibration,
    LayoutFingerprint,
    SessionGuiCalibrator,
    find_ue_window,
    heuristic_rois,
    probe_layout,
)
from .hotkey import EmergencyHotkey, build_emergency_handler, release_all_keys_and_buttons
from .overlay import ControlOverlay

__all__ = [
    "GuiCalibration",
    "LayoutFingerprint",
    "SessionGuiCalibrator",
    "find_ue_window",
    "heuristic_rois",
    "probe_layout",
    "EmergencyHotkey",
    "build_emergency_handler",
    "release_all_keys_and_buttons",
    "ControlOverlay",
]
