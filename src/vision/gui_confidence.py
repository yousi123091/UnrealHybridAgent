"""Heuristic vision provider + GUI confidence (Phase 4B)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class GuiConfidenceResult:
    ok: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)
    action: str = "proceed"  # proceed | recalibrate | require_confirmation | fail_closed
    window: dict[str, Any] = field(default_factory=dict)
    roi: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "action": self.action,
            "window": self.window,
            "roi": self.roi,
        }


class HeuristicVisionProvider:
    name = "heuristic"

    def describe(self) -> dict[str, Any]:
        return {"type": "heuristic", "vlm": None, "capabilities": ["pixel_diff", "roi_sanity"]}

    def assess_gui_confidence(self, **kwargs):
        return assess_gui_confidence(**kwargs)

    def verify_gui_action(self, *args, **kwargs):
        return verify_gui_action(*args, **kwargs)


def assess_gui_confidence(
    *,
    window: Mapping[str, Any] | None,
    roi: Mapping[str, Any] | None,
    focus_point: Mapping[str, Any] | list | None = None,
    screenshot_meta: Mapping[str, Any] | None = None,
    min_roi_confidence: float = 0.55,
    min_focus_confidence: float = 0.55,
) -> GuiConfidenceResult:
    reasons: list[str] = []
    conf = 1.0
    if not window:
        return GuiConfidenceResult(False, 0.0, ["no_window"], "recalibrate")
    w = window.get("window") if isinstance(window, Mapping) and "window" in window else window
    try:
        ww = float(w.get("w") or w.get("width") or 0)
        wh = float(w.get("h") or w.get("height") or 0)
    except Exception:
        ww = wh = 0
    if ww < 400 or wh < 300:
        conf -= 0.4
        reasons.append("window_too_small")
    if not roi:
        return GuiConfidenceResult(False, max(conf, 0.0), reasons + ["no_roi"], "recalibrate", window=dict(window or {}))
    # ROI should be inside window bounds approximately
    rx = float(roi.get("x") or 0)
    ry = float(roi.get("y") or 0)
    rw = float(roi.get("w") or roi.get("width") or 0)
    rh = float(roi.get("h") or roi.get("height") or 0)
    if rw <= 0 or rh <= 0:
        conf -= 0.5
        reasons.append("roi_degenerate")
    else:
        # allow negative origin for maximized windows slightly outside
        if rx + rw < 0 or ry + rh < 0:
            conf -= 0.4
            reasons.append("roi_outside_window")
        if ww > 0 and (rw / ww) > 0.95 and (rh / max(wh, 1)) > 0.95:
            # ROI covering almost entire window is suspicious for panel ROI
            conf -= 0.15
            reasons.append("roi_suspiciously_large")
    fp_conf = 1.0
    if focus_point is not None:
        try:
            if isinstance(focus_point, Mapping):
                fx, fy = float(focus_point.get("x", 0)), float(focus_point.get("y", 0))
            else:
                fx, fy = float(focus_point[0]), float(focus_point[1])
            if ww > 0 and wh > 0:
                if not (0 <= fx <= ww + 50 and 0 <= fy <= wh + 50):
                    fp_conf = 0.3
                    reasons.append("focus_point_outside_window")
        except Exception:
            fp_conf = 0.4
            reasons.append("focus_point_unparseable")
        conf = min(conf, fp_conf if fp_conf < 1 else conf)
    if screenshot_meta:
        sw = screenshot_meta.get("width")
        sh = screenshot_meta.get("height")
        if sw and sh and ww > 0:
            # extreme mismatch between screenshot and window can indicate wrong monitor
            ratio = float(sw) / max(ww, 1)
            if ratio < 0.4 or ratio > 3.0:
                conf -= 0.25
                reasons.append("screenshot_window_mismatch")
    conf = max(0.0, min(1.0, conf))
    action = "proceed"
    if conf < min_roi_confidence or "roi_outside_window" in reasons or "roi_degenerate" in reasons:
        action = "recalibrate"
        ok = False
    elif conf < max(min_roi_confidence, min_focus_confidence) + 0.15:
        action = "require_confirmation"
        ok = False
    else:
        ok = True
    if action == "recalibrate" and conf < 0.35:
        action = "fail_closed"
    return GuiConfidenceResult(ok=ok, confidence=conf, reasons=reasons, action=action,
                               window=dict(window or {}), roi=dict(roi or {}))


def verify_gui_action(kind: str, before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, Any]:
    """Post-action GUI verification. click success != task success."""
    kind = (kind or "").lower()
    before = dict(before or {})
    after = dict(after or {})
    if kind in ("focus_viewport", "click"):
        # cannot prove semantic focus from click alone
        return {
            "status": "UNKNOWN",
            "reason": "input_event_only",
            "expected": "editor_focus_or_selection_change",
            "observed": after,
            "recommended_next_action": "verify_structured_state_or_screenshot_diff",
        }
    if kind in ("ctrl_s", "save"):
        return {
            "status": "UNKNOWN",
            "reason": "keyboard_event_only",
            "expected": "level_dirty_cleared_or_mtime_advanced",
            "observed": after,
            "recommended_next_action": "verify_level_save_structured",
        }
    if kind == "tab_opened":
        changed = after.get("active_tab") != before.get("active_tab")
        return {"status": "PASS" if changed else "FAIL", "expected": "active_tab_changed",
                "observed": {"before": before.get("active_tab"), "after": after.get("active_tab")},
                "recommended_next_action": "" if changed else "recalibrate"}
    return {"status": "UNKNOWN", "reason": "no_postcondition_for_kind", "recommended_next_action": "escalate_to_llm"}


__all__ = ["HeuristicVisionProvider", "GuiConfidenceResult", "assess_gui_confidence", "verify_gui_action"]
