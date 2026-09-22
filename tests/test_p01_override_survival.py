"""P0.1 — executable spec for Human Override survival and injection attribution.

These are OFFLINE and deterministic: no mouse movement, no network, no desktop lock.
A failure here is a real defect.

Run:  python -m tests.test_p01_override_survival
"""

from __future__ import annotations

import ctypes
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.safety.controller import (  # noqa: E402
    SafetyController,
    SafetyState,
    external_gate_allows_input,
    reset_safety_controller_for_tests,
)


def _c() -> SafetyController:
    d = Path(tempfile.mkdtemp())
    c = SafetyController(state_path=d / "gate.json", injection_lease_s=30.0)
    c.set_banner_available(True)      # these tests assume a working banner exists
    return c


def _grant(c: SafetyController) -> None:
    c.request_control(task="t")
    c.banner_visible(ok=True)
    c.grant_control()


# ------------------------------------------------ RC-1: override must be terminal
def test_human_override_survives_release_control() -> None:
    """P0.1 §10: nothing but an explicit user resume may clear HUMAN_OVERRIDE.

    The CU adapter calls ``release_control()`` in the ``finally`` of every
    ``control()`` block. If release cleared the override, the agent would silently
    regain injection permission right after the human took over.
    """
    c = _c()
    _grant(c)
    assert c.allow_input is True
    c.trigger_human_override(kind="mouse")
    assert c.state == SafetyState.HUMAN_OVERRIDE

    c.release_control(reason="cu_release")          # <- what the adapter does

    assert c.state == SafetyState.HUMAN_OVERRIDE, f"release wiped override -> {c.state.value}"
    assert c.allow_input is False, "injection re-enabled without user resume"
    assert c.snapshot().human_override is True


def test_no_auto_regrant_after_override() -> None:
    """P0.1 §10/§11: the per-attempt request/grant cycle must not re-open the gate."""
    c = _c()
    _grant(c)
    c.trigger_human_override(kind="mouse")
    c.release_control(reason="cu_release")

    # Exactly what Executor._attempt / ComputerUseAdapter.control() do next.
    try:
        c.request_control(task="next_attempt")
        c.banner_visible(ok=True)
        c.grant_control()
    except Exception:
        assert c.allow_input is False
        return
    raise AssertionError(
        "auto-resume: request_control + grant_control re-opened injection after "
        f"HUMAN_OVERRIDE (state={c.state.value}, allow_input={c.allow_input})"
    )


def test_release_is_still_normal_when_not_terminal() -> None:
    """The guard must not break the ordinary path."""
    c = _c()
    _grant(c)
    c.release_control(reason="normal")
    assert c.state == SafetyState.RELEASED
    assert c.allow_input is False
    _grant(c)                       # a fresh cycle is allowed again
    assert c.allow_input is True


def test_emergency_stop_survives_release_control() -> None:
    """Control case: EMERGENCY_STOP was already protected; keeping both symmetric
    is exactly the blind spot the old suite hid."""
    c = _c()
    _grant(c)
    c.emergency_stop(reason="t")
    c.release_control(reason="cu_release")
    assert c.state == SafetyState.EMERGENCY_STOP
    assert c.allow_input is False


def test_only_explicit_resume_leaves_terminal() -> None:
    c = _c()
    _grant(c)
    c.trigger_human_override(kind="keyboard")
    try:
        c.resume_safety(explicit=False)
        raise AssertionError("non-explicit resume must fail")
    except RuntimeError:
        pass
    assert c.state == SafetyState.HUMAN_OVERRIDE
    c.resume_safety(explicit=True)
    assert c.state == SafetyState.IDLE
    assert c.allow_input is False        # §11: resume does not immediately re-enable


# --------------------------------------------- RC-4: injected events must be tagged
def test_flag_based_attribution_ignores_injected_motion() -> None:
    """P0.1 §5: an event carrying LLMHF_INJECTED is ours and must never trigger override.

    Drives the real hook callback with a real MSLLHOOKSTRUCT, so this exercises the
    production attribution code rather than a re-implementation of it.
    """
    from src.safety.human_override import (
        HC_ACTION,
        LLMHF_INJECTED,
        WM_MOUSEMOVE,
        HumanOverrideDetector,
        MSLLHOOKSTRUCT,
    )

    seen: list[str] = []
    det = HumanOverrideDetector(on_physical_input=seen.append, move_throttle_s=0.0)

    def fire(flags: int) -> None:
        info = MSLLHOOKSTRUCT()
        info.pt.x, info.pt.y = 100, 100
        info.flags = flags
        det._mouse_proc(HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(info))

    fire(LLMHF_INJECTED)          # the agent's own injected move
    assert seen == [], f"injected motion was classified as human: {seen}"
    fire(0)                       # a genuinely physical move
    assert seen == ["mouse_move"], f"physical motion was not detected: {seen}"
    assert det.physical_events == 1
    assert det.injected_events == 1


def test_hook_callback_never_swallows_input() -> None:
    """P0.1 §3/§5: the callback must return an int (whatever CallNextHookEx gives)."""
    from src.safety.human_override import HC_ACTION, WM_MOUSEMOVE, HumanOverrideDetector, MSLLHOOKSTRUCT

    det = HumanOverrideDetector(on_physical_input=None, move_throttle_s=0.0)
    info = MSLLHOOKSTRUCT()
    info.flags = 0
    rc = det._mouse_proc(HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(info))
    assert isinstance(rc, int), f"hook callback returned {type(rc).__name__}, not an int"


def test_detector_stop_is_idempotent_and_unhooks() -> None:
    from src.safety.human_override import HumanOverrideDetector

    det = HumanOverrideDetector(on_physical_input=None)
    det.start()
    mode = det.hook_mode
    assert mode in ("ll_flag_based", "polling_fallback"), mode
    if mode == "ll_flag_based":
        assert det.hook_alive is True
    det.stop()
    assert det.hook_mode == "stopped"
    assert det.hook_alive is False
    det.stop()          # must be safe to call again
    assert det.hook_mode == "stopped"


def test_reset_controller_stops_detector() -> None:
    """otherwise every test run leaks a live low-level hook."""
    from src.safety.controller import get_safety_controller

    reset_safety_controller_for_tests()
    sc = get_safety_controller(state_path=Path(tempfile.mkdtemp()) / "g.json")
    det = getattr(sc, "human_override_detector", None)
    assert det is not None
    reset_safety_controller_for_tests()
    assert det.hook_mode == "stopped"


# ------------------------------------------- RC-5: an external file may only close
def test_external_file_cannot_open_unknown_states() -> None:
    d = Path(tempfile.mkdtemp())
    p = d / "gate.json"
    p.write_text(
        json.dumps({"state": "ACTIVE", "allow_input": True, "agent_has_input_control": True}),
        encoding="utf-8",
    )
    assert external_gate_allows_input(p) is True
    for state in ("PAUSED", "HUMAN_OVERRIDE", "RELEASED", "IDLE", "CONTROL_ACQUIRED_", "garbage", ""):
        p.write_text(
            json.dumps({"state": state, "allow_input": True, "agent_has_input_control": True}),
            encoding="utf-8",
        )
        assert external_gate_allows_input(p) is False, f"state {state!r} opened the gate"


def test_gate_is_fail_closed_in_terminal_state() -> None:
    """Even a *permissive* on-disk file must not open a gate the live controller closed."""
    from src.adapters.computer_use import ComputerUseAdapter
    from src.safety.controller import get_safety_controller

    reset_safety_controller_for_tests()
    gate = Path(tempfile.mkdtemp()) / "gate.json"
    from src.safety import controller as controller_module
    sc = SafetyController(state_path=gate)
    controller_module._CONTROLLER = sc  # isolated offline fixture, no real input hook
    assert sc.state_path == gate
    sc.set_banner_available(True)

    _grant(sc)
    assert sc.allow_input is True

    cu = ComputerUseAdapter(base_url="http://127.0.0.1:1", client_id="spec", dry_run=True)
    cu._gate("move_mouse")           # allowed while the gate is genuinely open

    sc.trigger_human_override(kind="mouse")
    # a stale/foreign writer leaves a permissive file behind
    gate.write_text(
        json.dumps({"state": "ACTIVE", "allow_input": True, "agent_has_input_control": True}),
        encoding="utf-8",
    )

    try:
        cu._gate("move_mouse")
    except PermissionError as exc:
        assert "state=HUMAN_OVERRIDE" in str(exc), str(exc)
    else:
        raise AssertionError("stale gate file re-opened agent injection")
    finally:
        reset_safety_controller_for_tests()


# ------------------------------------------ RC-3: only release what UHA pressed
def test_release_only_touches_tracked_input() -> None:
    from src.desktop.hotkey import (
        clear_tracking,
        release_all_keys_and_buttons,
        track_button_press,
        track_key_press,
        tracked_input,
    )

    clear_tracking()
    # nothing tracked: the old implementation injected 38 UP events here. Now: zero.
    out = release_all_keys_and_buttons()
    assert out["keys_released"] == [] and out["buttons_released"] == []
    assert out["scope"] == "uha_tracked_only"

    track_key_press("ctrl a")
    assert set(tracked_input()["keys"]) == {"0x11", "0x41"}
    track_button_press("left")
    assert tracked_input()["buttons"] == ["left"]

    # those VKs are not actually held, so nothing gets injected — and the ledger clears
    out2 = release_all_keys_and_buttons()
    assert out2["keys_released"] == [] and out2["buttons_released"] == []
    assert tracked_input() == {"keys": [], "buttons": [], "unresolved": []}


def test_release_everything_requires_explicit_flag() -> None:
    from src.desktop.hotkey import release_everything_now

    try:
        release_everything_now()
        raise AssertionError("manual escape hatch must require explicit=True")
    except RuntimeError:
        pass


def test_unresolvable_key_is_reported_not_silently_dropped() -> None:
    from src.desktop.hotkey import clear_tracking, release_all_keys_and_buttons, track_key_press

    clear_tracking()
    track_key_press("hyperz")           # not a key we know
    out = release_all_keys_and_buttons()
    assert out["unresolved"] == ["hyperz"], out


# ------------------------------------------- RC-7: forced release must really work
def test_force_release_posts_the_key_the_server_reads() -> None:
    """``http.js`` destructures ``{tool, args}``; posting ``arguments`` was a no-op."""
    import urllib.request

    captured: dict = {}
    real_urlopen = urllib.request.urlopen

    class _Resp:
        def read(self) -> bytes:
            return b'{"ok": true, "result": {"released": true}}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        from src.safety.cleanup import force_cleanup_input

        out = force_cleanup_input(_c())
    finally:
        urllib.request.urlopen = real_urlopen  # type: ignore[assignment]

    body = captured.get("body")
    assert body, "force cleanup never issued the forced-release call"
    assert "args" in body, f"wrong body key (server reads 'args'): {body}"
    assert "arguments" not in body
    assert body["args"]["force"] is True

    step = next(s for s in out["steps"] if s["step"] == "agent_tars_force_release")
    assert step["ok"] is True, step


def test_cleanup_never_claims_user_control() -> None:
    """P0.1 §32."""
    from src.safety.cleanup import force_cleanup_input

    out = force_cleanup_input(_c())
    assert out["user_control_verified"] is False
    assert out["human_physical_input_blocked_by_uha"] is False
    assert out["idempotent"] is True
    out2 = force_cleanup_input(_c())
    assert out2["idempotent"] is True


def main() -> int:
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_p01_override_survival: {len(tests) - failed}/{len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
