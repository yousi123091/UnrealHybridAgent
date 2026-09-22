"""Idempotent force cleanup of UHA-established injection state only (P0.1 §14).

Rules:
  * Only release state UHA itself established. Never blanket-release the user's keys.
  * Never "guessing-restore" by simulating extra user input.
  * Report honestly: this function cannot verify that the user has physical control.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import Any

from ..desktop.hotkey import release_all_keys_and_buttons, tracked_input
from .controller import SafetyController, get_safety_controller


def release_response_ok(payload: dict) -> bool:
    """The REST envelope can be ok even when its MCP result reports a tool error."""
    import json
    if payload.get("ok") is not True:
        return False
    result = payload.get("result", {})
    if not isinstance(result, dict) or result.get("isError") or result.get("ok") is False:
        return False
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                body = json.loads(item.get("text", ""))
            except (TypeError, ValueError):
                return False
            if isinstance(body, dict) and body.get("ok") is False:
                return False
    return True


def _release_clipcursor_if_any() -> dict[str, Any]:
    """Release a cursor constraint **only if one is actually held**.

    UHA never calls ClipCursor(rect), but another automation layer on the same
    desktop might have. Checking first means we do not emit pointless API calls,
    and we can report truthfully whether a constraint was present.
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetClipCursor.restype = ctypes.c_int
        user32.GetClipCursor.argtypes = [ctypes.c_void_p]
        user32.ClipCursor.restype = ctypes.c_int
        user32.ClipCursor.argtypes = [ctypes.c_void_p]

        rect = wt.RECT()
        ok = user32.GetClipCursor(ctypes.byref(rect))
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        confined = bool(ok) and not (
            rect.left <= 0 and rect.top <= 0 and rect.right >= sw and rect.bottom >= sh
        )
        if not confined:
            return {"step": "clipcursor_clear", "ok": True, "action": "noop_no_constraint"}
        user32.ClipCursor(None)
        return {
            "step": "clipcursor_clear",
            "ok": True,
            "action": "released",
            "was": [rect.left, rect.top, rect.right, rect.bottom],
        }
    except Exception as exc:  # noqa: BLE001
        return {"step": "clipcursor_clear", "ok": False, "error": str(exc)[:120]}


def force_cleanup_input(
    controller: SafetyController | None = None,
    *,
    computer_use: Any | None = None,
    coordinator: Any | None = None,
    reason: str = "force_cleanup",
) -> dict[str, Any]:
    """Clean up UHA-owned injection artifacts. Safe to call repeatedly."""
    sc = controller or get_safety_controller()
    out: dict[str, Any] = {"reason": reason, "steps": []}

    # 1. close the agent injection gate first — everything else is tidying up
    try:
        sc.emergency_stop(reason=reason, source="force_cleanup")
        out["steps"].append({"step": "gate_closed", "ok": True})
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "gate_closed", "ok": False, "error": str(exc)[:160]})

    # 2. stop the low-level hook observer and unhook it for real
    det = getattr(sc, "human_override_detector", None)
    if det is not None:
        try:
            before = det.stats().get("hook_mode")
            det.stop()
            out["steps"].append({"step": "detector_stopped", "ok": True, "hook_mode_before": before})
        except Exception as exc:  # noqa: BLE001
            out["steps"].append({"step": "detector_stopped", "ok": False, "error": str(exc)[:160]})

    # 3. release ONLY the keys/buttons UHA actually pressed
    try:
        pending_before = tracked_input()
        rel = release_all_keys_and_buttons()
        out["steps"].append({
            "step": "release_injected_keys_buttons",
            "ok": True,
            "pending_before": pending_before,
            **rel,
        })
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({
            "step": "release_injected_keys_buttons", "ok": False, "error": str(exc)[:160],
        })

    # 4. cursor constraint (only if one is actually held)
    out["steps"].append(_release_clipcursor_if_any())

    # 5. give the desktop back to the CU service
    if computer_use is not None:
        try:
            if hasattr(computer_use, "release_control"):
                computer_use.release_control(force=True)
            out["steps"].append({"step": "cu_release_control", "ok": True})
        except Exception as exc:  # noqa: BLE001
            out["steps"].append({"step": "cu_release_control", "ok": False, "error": str(exc)[:160]})

    # 6. application-level session locks
    if coordinator is not None:
        try:
            if hasattr(coordinator, "force_release_all"):
                coordinator.force_release_all()
            out["steps"].append({"step": "session_locks_force_release", "ok": True})
        except Exception as exc:  # noqa: BLE001
            out["steps"].append({
                "step": "session_locks_force_release", "ok": False, "error": str(exc)[:160],
            })

    # 7. Agent-TARS desktop lock, via its REST convenience endpoint.
    #    The body must use the key the server actually reads ("args"); the earlier
    #    "arguments" spelling made every forced release a silent no-op (P0.1 RC-7).
    try:
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            "http://127.0.0.1:8788/call",
            data=_json.dumps({
                "tool": "computer_release_control",
                "args": {"clientId": "unreal-hybrid-agent", "force": True},
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as r:
            payload = _json.loads(r.read().decode("utf-8", "replace"))
        body_ok = release_response_ok(payload)
        out["steps"].append({
            "step": "agent_tars_force_release",
            "ok": body_ok,
            "response": payload.get("result", {}) if body_ok else payload.get("error"),
        })
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({
            "step": "agent_tars_force_release", "ok": False, "error": str(exc)[:160],
        })

    out["agent_injection_allowed"] = sc.allow_input
    out["safety_state"] = sc.state.value
    out["human_physical_input_blocked_by_uha"] = False  # by architecture
    out["user_input_blocking"] = False
    out["exclusive_mouse_grab"] = False
    out["exclusive_keyboard_grab"] = False
    out["idempotent"] = True
    # P0.1 §32 — this function must never be read as proof of user control.
    out["user_control_verified"] = False
    out["user_control_note"] = (
        "cleanup closes future UHA dispatch; in-flight backend actions may continue. Whether the user can actually "
        "use mouse/keyboard is a PHYSICAL observation and is PENDING HUMAN VALIDATION."
    )
    return out


__all__ = ["force_cleanup_input"]
