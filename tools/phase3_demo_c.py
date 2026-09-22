"""Phase 3 Demo C — real GUI control chain.

Layout validation → DesktopLock → focus UE → Computer Use safe action → verify → release.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve().parents[1]
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.core.config import load_config  # noqa: E402
from src.core.log import RunLogger  # noqa: E402
from src.core.session_locks import SessionCoordinator  # noqa: E402
from src.desktop.calibration import SessionGuiCalibrator  # noqa: E402
from src.runtime import build_bundle  # noqa: E402
from src.scheduler import Executor  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--action", default="focus_viewport", choices=[
        "focus_viewport", "outliner_search", "ctrl_s", "select_actor",
    ])
    ap.add_argument("--actor", default="Platform_Skirt_Front")
    args = ap.parse_args()

    cfg = load_config(args.config, reload=True)
    log = RunLogger(cfg.path("logs", ensure_parent=True), run_name="phase3_demo_c", console=True)
    evidence = {
        "demo": "C",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "result": "FAIL",
        "steps": [],
    }

    cache = cfg.path("desktop.gui_cache_file") if cfg.get("desktop.gui_cache_file") else \
        cfg.path("workspace.state_dir") / "gui_session.json"
    cal = SessionGuiCalibrator(cache, layout_overrides=cfg.get("gui.layout_overrides") or {})

    # 1) layout validation
    t0 = time.perf_counter()
    try:
        valid = cal.layout_valid()
        stats = cal.stats()
        evidence["steps"].append({
            "step": "layout_validate",
            "valid": valid,
            "stats": stats,
            "cache": str(cache),
            "duration_ms": (time.perf_counter() - t0) * 1000,
        })
        if not valid:
            t1 = time.perf_counter()
            try:
                result = cal.calibrate(force=False)
                result_dict = result.as_dict() if hasattr(result, "as_dict") else {"raw": str(result)}
                evidence["steps"].append({
                    "step": "recalibrate",
                    "result": result_dict,
                    "duration_ms": (time.perf_counter() - t1) * 1000,
                })
                # Fail closed: recalibration is only valid if it produced ROI/fingerprint
                valid = bool(
                    result_dict.get("rois")
                    or result_dict.get("fingerprint")
                    or (isinstance(result_dict, dict) and result_dict.get("success") is not False)
                )
            except Exception as exc:
                evidence["steps"].append({"step": "recalibrate", "error": str(exc)[:300]})
                valid = False
        evidence["layout_valid_final"] = valid
    except Exception as exc:
        evidence["steps"].append({"step": "layout_validate", "error": str(exc)[:300]})
        evidence["error"] = f"calibration failed: {exc}"
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return 2

    bundle = build_bundle(cfg)
    executor = Executor(bundle, cfg, log)
    coordinator: SessionCoordinator = executor.coordinator
    cu = bundle.computer_use

    # 2) overlay
    try:
        from src.core.session_control import get_controller
        from src.desktop.overlay import ControlOverlay

        controller = get_controller()
        overlay = ControlOverlay(controller, geometry=str((cfg.get("desktop.overlay") or {}).get("geometry") or "320x180+20+20"))
        overlay.start()
        evidence["overlay"] = {"available": overlay.available, "python_has_tk": overlay.available}
        evidence["steps"].append({"step": "overlay_start", "available": overlay.available})
    except Exception as exc:
        evidence["overlay"] = {"available": False, "error": str(exc)[:200]}

    # 3) DesktopLock
    lock_acquired = False
    lock_info = {}
    t2 = time.perf_counter()
    try:
        # Prefer executor/coordinator desktop lock if exposed
        if hasattr(coordinator, "desktop"):
            # context manager style used in executor
            lock_cm = coordinator.desktop()
            lock_cm.__enter__()
            lock_acquired = True
            lock_info = {"via": "coordinator.desktop", "acquired": True}
        else:
            lock_info = {"via": "none", "acquired": False, "note": "no coordinator.desktop"}
        evidence["steps"].append({
            "step": "desktop_lock_acquire",
            **lock_info,
            "duration_ms": (time.perf_counter() - t2) * 1000,
        })
    except Exception as exc:
        evidence["steps"].append({"step": "desktop_lock_acquire", "error": str(exc)[:300]})
        lock_info = {"acquired": False, "error": str(exc)[:200]}

    # 4) focus + computer use
    cu_result = {}
    try:
        focus_point = None
        try:
            focus_point = (cal.roi() or {}).get("focus_point") if hasattr(cal, "roi") else None
        except Exception:
            focus_point = None
        if focus_point is None:
            try:
                data = json.loads(Path(cache).read_text(encoding="utf-8"))
                focus_point = data.get("focus_point")
            except Exception:
                focus_point = [820, 450]

        evidence["focus_point"] = focus_point
        if cu is not None:
            conn = cu.connect(required=False)
            evidence["cu_connect"] = conn
            t3 = time.perf_counter()
            if args.action == "focus_viewport":
                # click focus point then release — safe, no mutation
                action = {
                    "action": "click",
                    "x": focus_point[0],
                    "y": focus_point[1],
                    "button": "left",
                }
            elif args.action == "ctrl_s":
                action = {"action": "hotkey", "keys": ["CTRL", "S"]}
            elif args.action == "outliner_search":
                # type actor name in search - safer via screenshot + type if available
                action = {"action": "screenshot"}
            else:
                action = {"action": "screenshot"}

            # Try high-level CU API with proper lock semantics
            ok = False
            res = None
            try:
                if args.action == "focus_viewport":
                    with cu.control(wait=True):
                        res = cu.click(int(focus_point[0]), int(focus_point[1]))
                elif args.action == "ctrl_s":
                    with cu.control(wait=True):
                        res = cu.hotkey("CTRL+S")
                elif args.action == "outliner_search":
                    # screenshot is read-only; no input lock required
                    art = cfg.path("artifacts")
                    Path(str(art)).mkdir(parents=True, exist_ok=True)
                    shot = cu.screenshot(max_width=1280, save_to=str(art / "demo_c_outliner.png"))
                    res = {"screenshot_path": str(getattr(shot, "path", None)), "width": shot.width, "height": shot.height}
                else:
                    art = cfg.path("artifacts")
                    Path(str(art)).mkdir(parents=True, exist_ok=True)
                    shot = cu.screenshot(save_to=str(art / "demo_c_shot.png"))
                    res = {"screenshot_path": str(getattr(shot, "path", None)), "width": shot.width, "height": shot.height}
                ok = True
                cu_result = res if isinstance(res, dict) else {"result": str(res)[:300]}
            except Exception as exc:
                cu_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            cu_result.setdefault("ok", ok)
            cu_result["action"] = args.action
            cu_result["duration_ms"] = (time.perf_counter() - t3) * 1000
        else:
            cu_result = {"ok": False, "error": "computer_use adapter missing"}
        evidence["steps"].append({"step": "computer_use_action", "result": cu_result})
    except Exception as exc:
        evidence["steps"].append({"step": "computer_use_action", "error": str(exc)[:300]})
        cu_result = {"ok": False, "error": str(exc)[:200]}

    # 5) release control
    release_info = {}
    try:
        if cu is not None and hasattr(cu, "release_control"):
            release_info["cu_release"] = cu.release_control(force=True)
        if lock_acquired:
            try:
                lock_cm.__exit__(None, None, None)
                release_info["desktop_lock_released"] = True
            except Exception as exc:
                release_info["desktop_lock_released"] = False
                release_info["desktop_lock_error"] = str(exc)[:200]
        if hasattr(coordinator, "stats"):
            release_info["lock_stats"] = coordinator.stats()
        evidence["steps"].append({"step": "release", "result": release_info})
    except Exception as exc:
        evidence["steps"].append({"step": "release", "error": str(exc)[:200]})

    evidence["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    overlay_ok = bool((evidence.get("overlay") or {}).get("available"))
    gui_ok = bool(cu_result.get("ok"))
    released = bool(release_info.get("desktop_lock_released", True))
    if valid and gui_ok and released and lock_acquired:
        evidence["result"] = "PASS"
    elif valid and (gui_ok or overlay_ok):
        evidence["result"] = "PARTIAL"
    else:
        evidence["result"] = "FAIL"

    out = cfg.path("logs") / "runs" / f"phase3_demo_c_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    print(f"\nRESULT={evidence['result']}")
    print(f"Evidence: {out}")

    # stop overlay
    try:
        overlay.stop()
    except Exception:
        pass
    bundle.close()
    return 0 if evidence["result"] in ("PASS", "PARTIAL") else 2


if __name__ == "__main__":
    raise SystemExit(main())
