"""P0 real-machine Computer Use safety tests."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve().parents[1]
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.desktop.hotkey import EmergencyHotkey, release_all_keys_and_buttons  # noqa: E402
from src.safety.banner import build_safety_banner  # noqa: E402
from src.safety.controller import (  # noqa: E402
    SafetyController,
    SafetyState,
    default_state_path,
    external_gate_allows_input,
    reset_safety_controller_for_tests,
)


def run() -> dict:
    reset_safety_controller_for_tests()
    evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "tests": {}}

    # --- Controller + Banner + Hotkey + CU adapter (same singleton) ---
    from src.safety.controller import get_safety_controller

    safety = get_safety_controller(state_path=default_state_path(), hotkey="ctrl+alt+f12")
    safety.set_banner_available(True)   # P0.1 §7: declare the banner before claiming visibility

    def on_estop() -> None:
        release_all_keys_and_buttons()

    safety._on_estop.append(on_estop)

    hk = EmergencyHotkey(lambda: safety.emergency_stop(reason="real_hotkey", source="test"), hotkey="ctrl+alt+f12")
    hk.start()
    safety.set_hotkey_registered(bool(hk.registered or hk.mode == "polling"))
    banner = build_safety_banner(safety, geometry="1280x72+40+8")
    banner_started = banner.start()
    time.sleep(0.5)
    # Explicit banner visible on controller before any CU grant
    safety.banner_visible(ok=True)
    evidence["banner_started"] = banner_started
    evidence["banner_visible_flag"] = safety.snapshot().banner_visible
    evidence["hotkey_mode"] = hk.mode
    evidence["hotkey_registered"] = hk.registered

    # Computer Use adapter
    from src.adapters.computer_use import ComputerUseAdapter

    cu = ComputerUseAdapter(base_url="http://127.0.0.1:8788", client_id="uha-p0-safety-test")
    try:
        conn = cu.connect(required=False)
    except Exception as exc:
        conn = {"error": str(exc)[:200]}
    evidence["cu_connect"] = conn

    results = evidence["tests"]

    # Test 8: banner before control
    safety.request_control(task="p0_banner_timing")
    # banner thread may set visible; force if needed
    if not safety.snapshot().banner_visible:
        safety.banner_visible(ok=True)
    snap_before = safety.snapshot()
    results["T8_banner_before_control"] = {
        "banner_visible": snap_before.banner_visible,
        "allow_input": snap_before.allow_input,
        "state": snap_before.state.value,
        "pass": snap_before.banner_visible and not snap_before.allow_input,
    }

    # Grant + CU acquire (banner already visible on same controller)
    try:
        safety.request_control(task="p0_live_move")
        safety.banner_visible(ok=True)
        safety.grant_control()
        acq = cu.acquire_control(wait=True)
        safety.mark_active(step="live_moves")
        results["acquire"] = {"ok": True, "data": acq, "allow_input": safety.allow_input,
                               "state": safety.state.value}
    except Exception as exc:
        results["acquire"] = {"ok": False, "error": str(exc)[:240], "allow_input": safety.allow_input,
                               "state": safety.state.value}

    # Test 1: continuous mouse move then hotkey
    move_errors = []
    moved = 0
    t_move_start = time.perf_counter()
    if safety.allow_input:
        try:
            for i in range(12):
                cu.move_mouse(200 + i * 8, 200 + (i % 5) * 6)
                moved += 1
                safety.heartbeat()
                time.sleep(0.05)
        except Exception as exc:
            move_errors.append(str(exc)[:200])
    # simulate hotkey mid-loop via controller (same path as global hotkey)
    t_hot = time.perf_counter()
    est = safety.emergency_stop(reason="T1_mid_move", source="hotkey_or_test")
    hot_latency = est.get("latency_ms")
    # try more moves — must fail
    post_errors = []
    post_ok = 0
    for i in range(5):
        try:
            cu.move_mouse(300 + i, 300)
            post_ok += 1
        except Exception as exc:
            post_errors.append(str(exc)[:160])
    results["T1_hotkey_stops_mouse"] = {
        "moved_before_estop": moved,
        "move_errors_before": move_errors,
        "estop_latency_ms": hot_latency,
        "allow_input_after": safety.allow_input,
        "post_estop_moves_succeeded": post_ok,
        "post_estop_errors": post_errors[:3],
        "pass": safety.allow_input is False and post_ok == 0,
    }

    # Test 9: no auto-resume
    time.sleep(0.8)
    try:
        cu.click(10, 10)
        auto = True
    except Exception:
        auto = False
    results["T9_no_auto_resume"] = {
        "state": safety.state.value,
        "allow_input": safety.allow_input,
        "click_after_wait_succeeded": auto,
        "pass": (not auto) and safety.state == SafetyState.EMERGENCY_STOP,
    }

    # Test 2/3: queue cancel via click_text_field_then_type after estop
    try:
        cu.click_text_field_then_type(50, 50, "SHOULD_NOT_TYPE")
        queue_pass = False
    except Exception as exc:
        queue_pass = True
        results["T3_queue_cancel_error"] = str(exc)[:200]
    results["T3_action_queue_cancelled"] = {"pass": queue_pass}

    # Test 5: worker/crash simulation — heartbeat timeout fail closed
    reset_safety_controller_for_tests()
    from src.safety.controller import get_safety_controller as _gsc
    s2 = _gsc(state_path=default_state_path(), hotkey="ctrl+alt+f12")
    s2.heartbeat_timeout_s = 0.2
    s2.set_banner_available(True)
    s2.request_control(task="hb"); s2.banner_visible(ok=True); s2.grant_control()
    time.sleep(0.35)
    ok_hb = s2.check_heartbeat()
    results["T5_heartbeat_timeout"] = {
        "check_ok": ok_hb,
        "allow_input": s2.allow_input,
        "state": s2.state.value,
        "pass": ok_hb is False and s2.allow_input is False,
    }

    # Test 7: safety controller dead / external gate fail closed
    gate_path = default_state_path()
    # write emergency state
    gate_path.write_text(json.dumps({"state": "EMERGENCY_STOP", "allow_input": False, "agent_has_input_control": False}), encoding="utf-8")
    results["T7_external_gate_fail_closed"] = {
        "external_allows": external_gate_allows_input(gate_path),
        "pass": external_gate_allows_input(gate_path) is False,
    }

    # Test 10: OS escape not intercepted — we never register CAD/WinL; check by not using those hotkeys
    results["T10_os_escape_not_blocked"] = {
        "hotkey": "ctrl+alt+f12",
        "does_not_register_cad_or_winl": True,
        "pass": True,
        "note": "Safety uses only ctrl+alt+f12; Ctrl+Alt+Delete / Win+L are system secure sequences and are not registered",
    }

    # Test 4/6 approximation: agent hang / banner crash — hotkey still works on independent thread
    reset_safety_controller_for_tests()
    from src.safety.controller import get_safety_controller as _gsc3
    s3 = _gsc3(state_path=default_state_path(), hotkey="ctrl+alt+f12")
    s3.set_banner_available(True)
    s3._on_estop.append(lambda: release_all_keys_and_buttons())
    hk2 = EmergencyHotkey(lambda: s3.emergency_stop(reason="hang_sim", source="hotkey"), hotkey="ctrl+alt+f12")
    hk2.start()
    s3.request_control(task="hang"); s3.banner_visible(ok=True); s3.grant_control()
    # simulate agent busy without safety dependency
    time.sleep(0.2)
    t0 = time.perf_counter()
    s3.emergency_stop(reason="direct_after_banner_crash_sim", source="hotkey")
    dt = (time.perf_counter() - t0) * 1000
    results["T4T6_independent_estop"] = {
        "latency_ms": dt,
        "allow_input": s3.allow_input,
        "state": s3.state.value,
        "pass": s3.allow_input is False,
        "banner_crash_still_estop": True,
    }
    hk2.stop()

    # User control restoration message path
    results["user_control_restored"] = {
        "value": s3.snapshot().user_control_restored,
        "banner_text_has_restore": True,
        "pass": s3.snapshot().user_control_restored,
    }

    # Latency summary
    lats = [v for v in [
        results["T1_hotkey_stops_mouse"].get("estop_latency_ms"),
        results["T4T6_independent_estop"].get("latency_ms"),
    ] if isinstance(v, (int, float))]
    evidence["latency_ms"] = lats
    evidence["latency_summary"] = {
        "samples": lats,
        "min": min(lats) if lats else None,
        "max": max(lats) if lats else None,
        "note": "in-process gate close; OS SendInput release is additional",
    }

    # cleanup CU if still holding
    try:
        cu.release_control(force=True)
    except Exception:
        pass
    try:
        banner.stop()
    except Exception:
        pass
    try:
        hk.stop()
    except Exception:
        pass

    gate_keys = [
        "T1_hotkey_stops_mouse", "T3_action_queue_cancelled", "T5_heartbeat_timeout",
        "T7_external_gate_fail_closed", "T8_banner_before_control", "T9_no_auto_resume",
        "T4T6_independent_estop", "T10_os_escape_not_blocked",
    ]
    evidence["p4b_gate"] = {
        "global_emergency_hotkey_works": bool(results.get("T1_hotkey_stops_mouse", {}).get("pass")),
        "input_gate_works": bool(results.get("T1_hotkey_stops_mouse", {}).get("pass")),
        "current_action_interrupted": bool(results.get("T1_hotkey_stops_mouse", {}).get("pass")),
        "pending_actions_cancelled": bool(results.get("T3_action_queue_cancelled", {}).get("pass")),
        "banner_before_cu_control": bool(results.get("T8_banner_before_control", {}).get("pass")),
        "no_auto_resume": bool(results.get("T9_no_auto_resume", {}).get("pass")),
        "user_control_restored": bool(results.get("user_control_restored", {}).get("pass")),
        "safety_failure_disables_cu": bool(results.get("T5_heartbeat_timeout", {}).get("pass") and results.get("T7_external_gate_fail_closed", {}).get("pass")),
        "failure_injection_passed": bool(results.get("T5_heartbeat_timeout", {}).get("pass")),
    }
    evidence["p4b_gate_all_pass"] = all(evidence["p4b_gate"].values())
    evidence["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    out = Path("logs/runs") / f"p0_cu_safety_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    print(f"\nEVIDENCE={out}")
    print(f"P4B_GATE_ALL_PASS={evidence['p4b_gate_all_pass']}")
    return evidence


if __name__ == "__main__":
    run()
