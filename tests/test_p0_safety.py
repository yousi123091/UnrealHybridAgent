"""P0 Computer Use safety offline + gate tests."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.safety.controller import (  # noqa: E402
    InputOwner,
    SafetyController,
    SafetyState,
    external_gate_allows_input,
    reset_safety_controller_for_tests,
)


def _ctrl(tmp: Path) -> SafetyController:
    c = SafetyController(state_path=tmp / "safety_gate.json", hotkey="ctrl+alt+f12")
    # This suite exercises the gate, not banner rendering: declare that a working
    # banner exists. See test_banner_availability_is_required for the fail-closed path.
    c.set_banner_available(True)
    return c


def test_banner_availability_is_required() -> None:
    """P0.1 §7: `banner_visible(ok=True)` must not be a boolean the agent writes to
    itself. With no banner declared, the gate must stay shut."""
    import tempfile

    c = SafetyController(state_path=Path(tempfile.mkdtemp()) / "g.json")
    c.request_control(task="t")
    c.banner_visible(ok=True)               # no banner exists -> must not take effect
    assert c.snapshot().banner_visible is False
    assert c.banner_available is False
    try:
        c.grant_control()
        raise AssertionError("grant must be refused when no banner can be shown")
    except RuntimeError as e:
        assert "banner" in str(e).lower(), str(e)
    assert c.allow_input is False
    # and the selfcheck must refuse to enable Computer Use
    c.set_hotkey_registered(True)
    assert c.startup_selfcheck()["computer_use_allowed"] is False


def test_estop_closes_gate_immediately() -> None:
    import tempfile

    c = _ctrl(Path(tempfile.mkdtemp()))
    c.request_control(task="t")
    c.banner_visible(ok=True)
    c.grant_control()
    assert c.allow_input is True
    t0 = time.perf_counter()
    res = c.emergency_stop(reason="test", source="unit")
    dt = (time.perf_counter() - t0) * 1000
    assert c.allow_input is False
    assert c.state == SafetyState.EMERGENCY_STOP
    assert c.snapshot().input_owner == InputOwner.USER
    assert c.snapshot().agent_has_input_control is False
    assert res["latency_ms"] < 50 or dt < 200
    # no auto resume
    try:
        c.grant_control()
        raise AssertionError("grant after estop must fail")
    except RuntimeError:
        pass
    assert c.allow_input is False


def test_banner_required_before_grant() -> None:
    import tempfile

    c = _ctrl(Path(tempfile.mkdtemp()))
    c.request_control(task="t")
    try:
        c.grant_control()
        raise AssertionError("grant without banner must fail")
    except RuntimeError as e:
        assert "banner" in str(e).lower()
    assert c.allow_input is False


def test_explicit_resume_required() -> None:
    import tempfile

    c = _ctrl(Path(tempfile.mkdtemp()))
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    c.emergency_stop(reason="x")
    try:
        c.resume_safety(explicit=False)
        raise AssertionError("non-explicit resume must fail")
    except RuntimeError:
        pass
    assert c.state == SafetyState.EMERGENCY_STOP
    c.resume_safety(explicit=True)
    assert c.state == SafetyState.IDLE
    assert c.allow_input is False  # still no control until new grant
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    assert c.allow_input is True


def test_heartbeat_timeout_fail_closed() -> None:
    import tempfile

    c = _ctrl(Path(tempfile.mkdtemp()))
    c.heartbeat_timeout_s = 0.05
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    time.sleep(0.08)
    ok = c.check_heartbeat()
    assert ok is False
    assert c.allow_input is False
    assert c.state == SafetyState.EMERGENCY_STOP


def test_external_gate_file() -> None:
    import tempfile
    import json

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "safety_gate.json"
    path.write_text(json.dumps({"state": "ACTIVE", "allow_input": True, "agent_has_input_control": True}), encoding="utf-8")
    assert external_gate_allows_input(path) is True
    path.write_text(json.dumps({"state": "EMERGENCY_STOP", "allow_input": False}), encoding="utf-8")
    assert external_gate_allows_input(path) is False
    path.write_text("not-json", encoding="utf-8")
    assert external_gate_allows_input(path) is False


def test_banner_text_shows_hotkey() -> None:
    from src.safety.banner import BANNER_TEXT

    assert "EMERGENCY STOP" in BANNER_TEXT["active"]
    assert "Ctrl" in BANNER_TEXT["active"] or "Ctrl" in BANNER_TEXT["active"]
    assert "USER" in BANNER_TEXT["emergency"].upper() or "User" in BANNER_TEXT["emergency"]


def test_safety_outweighs_running_agent_state() -> None:
    import tempfile
    from src.core.session_control import SessionController

    c = _ctrl(Path(tempfile.mkdtemp()))
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    c.emergency_stop(reason="x")
    agent = SessionController()
    agent.begin_task("task")
    # agent cannot override safety
    assert c.allow_input is False
    try:
        c.grant_control()
    except RuntimeError:
        pass
    assert c.state == SafetyState.EMERGENCY_STOP


def test_startup_selfcheck_gate() -> None:
    import tempfile

    c = _ctrl(Path(tempfile.mkdtemp()))
    c.set_hotkey_registered(False)
    sc = c.startup_selfcheck()
    assert sc["computer_use_allowed"] is False
    c.set_hotkey_registered(True)
    sc2 = c.startup_selfcheck()
    assert sc2["computer_use_allowed"] is True


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
    print(f"test_p0_safety: {len(tests) - failed}/{len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
