"""Phase 3 regression tests — pause must not be wiped by Executor.begin_task."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.session_control import (  # noqa: E402
    ControlCommand,
    SessionController,
    TaskPhase,
    get_controller,
)


def _fresh() -> SessionController:
    # Use a new controller rather than process singleton for isolation
    return SessionController()


def test_begin_task_preserves_pause() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.pause()
    assert c.snapshot().phase == TaskPhase.PAUSED
    c.begin_task("t2")
    snap = c.snapshot()
    assert snap.command == ControlCommand.PAUSE, snap
    assert snap.phase == TaskPhase.PAUSED, snap
    assert snap.task == "t2"


def test_begin_task_preserves_estop() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.emergency_stop(reason="unit")
    c.begin_task("t2")
    snap = c.snapshot()
    assert snap.phase == TaskPhase.ABORTED
    assert snap.command == ControlCommand.EMERGENCY_STOP
    assert c.should_abort()


def test_resume_after_pause_allows_begin_task() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.pause()
    c.resume()
    c.begin_task("t2")
    snap = c.snapshot()
    assert snap.command == ControlCommand.NONE
    assert snap.phase == TaskPhase.ANALYZING


def test_blocks_writes_on_pause() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.pause()
    assert c.blocks_writes() is True
    try:
        c.ensure_writable("probe")
    except Exception as exc:
        assert "PAUSED" in str(exc) or "拒绝" in str(exc)
    else:
        raise AssertionError("ensure_writable should raise while paused")


def test_end_task_does_not_wipe_pause() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.pause()
    c.end_task(ok=True)
    snap = c.snapshot()
    assert snap.command == ControlCommand.PAUSE, snap
    assert snap.phase == TaskPhase.PAUSED, snap
    assert c.blocks_writes() is True


def test_end_task_does_not_wipe_estop() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.emergency_stop(reason="unit")
    c.end_task(ok=False)
    snap = c.snapshot()
    assert snap.phase == TaskPhase.ABORTED
    assert snap.command == ControlCommand.EMERGENCY_STOP
    assert c.should_abort()
    assert c.blocks_writes()


def test_end_task_does_not_wipe_stop() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.stop()
    c.end_task(ok=False)
    snap = c.snapshot()
    assert snap.command == ControlCommand.STOP
    assert c.should_abort()


def test_acknowledge_control_clears() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.emergency_stop(reason="unit")
    c.acknowledge_control()
    snap = c.snapshot()
    assert snap.phase == TaskPhase.IDLE
    assert snap.command == ControlCommand.NONE
    assert not c.blocks_writes()


def test_failed_phase_blocks_writes() -> None:
    c = _fresh()
    c.begin_task("t1")
    # Force FAILED without control command via direct update path: end_task after none
    c.end_task(ok=False)
    assert c.snapshot().phase == TaskPhase.FAILED
    # FAILED alone does not keep abort, but blocks_writes includes FAILED
    assert c.blocks_writes() is True


def test_wait_if_paused_timeout_false() -> None:
    c = _fresh()
    c.begin_task("t1")
    c.pause()
    assert c.wait_if_paused(timeout=0.05) is False


def test_doctor_layered_signals_importable() -> None:
    from tools.phase3_doctor import collect_doctor, format_report

    assert callable(collect_doctor)
    assert callable(format_report)


def main() -> int:
    tests = [
        test_begin_task_preserves_pause,
        test_begin_task_preserves_estop,
        test_resume_after_pause_allows_begin_task,
        test_blocks_writes_on_pause,
        test_end_task_does_not_wipe_pause,
        test_end_task_does_not_wipe_estop,
        test_end_task_does_not_wipe_stop,
        test_acknowledge_control_clears,
        test_failed_phase_blocks_writes,
        test_wait_if_paused_timeout_false,
        test_doctor_layered_signals_importable,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_phase3: {len(tests) - failed}/{len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
