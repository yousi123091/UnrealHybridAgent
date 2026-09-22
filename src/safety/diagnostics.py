"""Safety diagnostics — uha safety status."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .controller import get_safety_controller, default_state_path, read_external_gate_state


def collect_safety_status() -> dict[str, Any]:
    from . import controller as controller_module
    from .controller import SafetySnapshot, SafetyState, CuLifecycle, external_gate_allows_input
    sc = controller_module._CONTROLLER
    gate = read_external_gate_state()
    snap = sc.snapshot() if sc is not None else SafetySnapshot()
    if sc is None:
        for key in ("banner_state", "banner_visible", "hotkey", "human_override"):
            if key in gate: setattr(snap, key, gate[key])
        try: snap.state = SafetyState(gate.get("state"))
        except (ValueError, TypeError): pass
        try: snap.lifecycle = CuLifecycle(gate.get("lifecycle"))
        except (ValueError, TypeError): pass
        snap.allow_input = external_gate_allows_input()
    controller_stats = sc.stats() if sc is not None else {"observed_from_file": True}

    # CU worker
    cu_health = None
    cu_error = None
    try:
        with urllib.request.urlopen("http://127.0.0.1:8788/health", timeout=2) as r:
            cu_health = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        cu_error = f"{type(exc).__name__}: {exc}"[:200]

    # Windows OS-level blocking checks (we never enable these)
    os_checks = {
        "user_input_blocking": False,
        "user_input_suppression": False,
        "exclusive_mouse_grab": False,
        "exclusive_keyboard_grab": False,
        "clipcursor_note": "ClipCursor cleared by force_cleanup if ever set; UHA does not confine cursor",
        "blockinput_used": False,
        "low_level_hooks_consume_events": False,
    }

    detector_stats = {}
    det = getattr(sc, "human_override_detector", None)
    if det is not None:
        try:
            detector_stats = det.stats()
        except Exception:
            detector_stats = {}
    # Banner processes
    banner_procs = []
    try:
        import subprocess

        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process python* -ErrorAction SilentlyContinue | Select-Object Id,MainWindowTitle | ConvertTo-Json -Compress"],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        data = json.loads(out.decode() or "[]")
        if isinstance(data, dict):
            data = [data]
        for row in data:
            title = str(row.get("MainWindowTitle") or "")
            if "Safety" in title or "UHA" in title:
                banner_procs.append(row)
    except Exception:
        pass

    status = {
        "safety_state": snap.state.value,
        "terminal_state": snap.state.value in ("EMERGENCY_STOP", "HUMAN_OVERRIDE"),
        "terminal_states": ["EMERGENCY_STOP", "HUMAN_OVERRIDE"],
        "human_override": snap.state.value == "HUMAN_OVERRIDE" or snap.state.value == "EMERGENCY_STOP",
        "agent_injection_permission": "OPEN" if snap.allow_input else "CLOSED",
        "attribution_mode": detector_stats.get("attribution", "unknown"),
        "hook_alive": detector_stats.get("hook_alive"),
        "input_owner_concept": {
            "physical_input_owner": "USER_ALWAYS",
            "agent_has_exclusive_ownership": False,
            "agent_has_injection_permission": bool(snap.allow_input),
            "note": "UHA never owns physical input; only temporary injection permission",
        },
        "lifecycle": snap.lifecycle.value,
        "banner_state": snap.banner_state,
        "banner_visible_flag": snap.banner_visible,
        "banner_processes": banner_procs,
        "hotkey": snap.hotkey,
        "hotkey_registered": controller_stats.get("hotkey_registered"),
        "cu_health": cu_health,
        "cu_error": cu_error,
        "cu_lock": (cu_health or {}).get("lock") if isinstance(cu_health, dict) else None,
        "gate_file": str(default_state_path()),
        "gate_file_state": gate,
        "gate_file_can_only_close": True,
        "os_blocking_zero_policy": os_checks,
        "human_override_detector": detector_stats,
        "controller_stats": controller_stats,
        "human_physical_input_blocked_by_uha": False,
        "user_input_blocking": False,
        "user_input_suppression": False,
        "exclusive_mouse_grab": False,
        "exclusive_keyboard_grab": False,
        # P0.1 §32 — never let this report be read as a physical verification.
        "user_control_verified": False,
        "pending_human_validation": True,
    }
    return status


def format_safety_status(status: dict[str, Any]) -> str:
    lines = [
        "=== UHA Safety Status (P0.1) ===",
        f"safety_state: {status.get('safety_state')}  terminal={status.get('terminal_state')}",
        f"human_override: {status.get('human_override')}",
        f"agent_injection_permission: {status.get('agent_injection_permission')}",
        f"physical_input_owner: {status.get('input_owner_concept', {}).get('physical_input_owner')}",
        f"agent_exclusive_ownership: {status.get('input_owner_concept', {}).get('agent_has_exclusive_ownership')}",
        f"attribution_mode: {status.get('attribution_mode')}  hook_alive={status.get('hook_alive')}",
        f"lifecycle: {status.get('lifecycle')}",
        f"banner_state: {status.get('banner_state')} processes={status.get('banner_processes')}",
        f"hotkey: {status.get('hotkey')} registered={status.get('hotkey_registered')}",
        f"cu_lock: {json.dumps(status.get('cu_lock'), ensure_ascii=False)}",
        f"user_input_blocking: {status.get('user_input_blocking')}",
        f"user_input_suppression: {status.get('user_input_suppression')}",
        f"exclusive_mouse_grab: {status.get('exclusive_mouse_grab')}",
        f"exclusive_keyboard_grab: {status.get('exclusive_keyboard_grab')}",
        f"detector: {json.dumps(status.get('human_override_detector'), ensure_ascii=False)}",
        "",
        "Physical takeover is not proven by diagnostics; mid-action backend cancellation remains unverified.",
        "Agent only has injection permission — never exclusive ownership.",
        f"user_control_verified: {status.get('user_control_verified')} "
        "(-> physical observation is PENDING HUMAN VALIDATION, P0.1 §32)",
    ]
    return "\n".join(lines)


__all__ = ["collect_safety_status", "format_safety_status"]
