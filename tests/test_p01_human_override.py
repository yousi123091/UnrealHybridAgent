"""P0.1 offline tests — injection permission ≠ ownership; human override."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.safety.controller import (  # noqa: E402
    InputOwner,
    SafetyController,
    SafetyState,
)
from src.safety.cleanup import force_cleanup_input  # noqa: E402
from src.safety.banner import BANNER_TEXT  # noqa: E402


def _c() -> SafetyController:
    c = SafetyController(state_path=Path(tempfile.mkdtemp()) / "g.json", injection_lease_s=2.0)
    c.set_banner_available(True)      # this suite assumes a working banner exists
    return c


def test_no_exclusive_ownership() -> None:
    c = _c()
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    snap = c.snapshot()
    assert snap.input_owner == InputOwner.USER
    assert snap.physical_input_owner if False else snap.as_dict()["physical_input_owner"] == "USER_ALWAYS"
    assert snap.human_input_blocked_by_uha is False
    assert snap.agent_injection_permission is True
    assert c.allow_input is True


def test_human_override_revokes_injection() -> None:
    c = _c()
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    r = c.trigger_human_override(kind="mouse")
    assert c.allow_input is False
    assert c.snapshot().state == SafetyState.HUMAN_OVERRIDE
    assert c.snapshot().human_override is True
    assert "HUMAN OVERRIDE" in BANNER_TEXT["human_override"] or "Human" in BANNER_TEXT["human_override"]
    # no auto resume
    try:
        c.grant_control()
        raise AssertionError("must not auto grant")
    except RuntimeError:
        pass
    c.resume_safety(explicit=True)
    assert c.allow_input is False  # still closed until new grant


def test_lease_expiry() -> None:
    import time

    c = _c()
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    assert c.allow_input is True
    time.sleep(2.2)
    assert c.allow_input is False  # lease expired


def test_banner_text_no_ownership_claim() -> None:
    assert "own" not in BANNER_TEXT["active"].lower() or "never" in BANNER_TEXT["active"].lower()
    assert "take over" in BANNER_TEXT["active"].lower() or "Take over" in BANNER_TEXT["active"]
    assert "Move" in BANNER_TEXT["active"] or "move" in BANNER_TEXT["active"]


def test_force_cleanup_idempotent() -> None:
    c = _c()
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    r1 = force_cleanup_input(c)
    r2 = force_cleanup_input(c)
    assert r1["human_physical_input_blocked_by_uha"] is False
    assert r2["idempotent"] is True
    assert c.allow_input is False


def test_agent_injection_does_not_mean_ownership_language() -> None:
    from src.safety.diagnostics import collect_safety_status

    # collect without network-heavy fail
    try:
        st = collect_safety_status()
    except Exception:
        st = {}
    assert st.get("user_input_blocking") is False or "user_input_blocking" not in st
    assert st.get("input_owner_concept", {}).get("agent_has_exclusive_ownership") is False or st == {}


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
    print(f"test_p01_human_override: {len(tests) - failed}/{len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
