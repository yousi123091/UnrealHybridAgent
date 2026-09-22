"""UHA P0.1 — live demonstration that Human Override now actually holds.

What this proves, and how honestly it proves it, are two different things, so the
output separates them:

  AUTO PASS                  machine-verified here, reproducibly
  SIMULATED PHYSICAL EVENT   a physical event delivered through the REAL hook callback
                             and REAL state machine, but not produced by a human hand
  PENDING HUMAN VALIDATION   needs your hand on the mouse/keyboard

Run either way:

    python -m tools.p01_override_demo            # automated, ~15 s
    python -m tools.p01_override_demo --human    # waits for YOU to move the mouse

The ``--human`` run is the one that can turn the remaining PENDING items into
HUMAN VERIFIED evidence.

Notes
-----
* It moves the real cursor (small offsets near its current position, then restores it).
* It uses a temporary safety-state file, so your real ``.state/safety_gate.json`` is
  left alone.
* It needs the Agent-TARS Computer Use service on 127.0.0.1:8788 for the live section;
  if it is down the live section is reported SKIPPED, never faked.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: object = "", *, kind: str = "AUTO PASS") -> bool:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail, "kind": kind})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}  ({kind})")
    if detail not in ("", None):
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, default=str)
        print(f"         {text[:400]}")
    return bool(ok)


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 62 - len(title)))


def _cursor() -> tuple[int, int]:
    u = ctypes.WinDLL("user32")
    pt = wt.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _screen() -> tuple[int, int]:
    u = ctypes.WinDLL("user32")
    return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))


# ---------------------------------------------------------------- offline part
def part_offline() -> None:
    section("1. Offline: the mechanisms themselves")

    # 1a. only release what UHA pressed
    from src.desktop.hotkey import (
        clear_tracking,
        release_all_keys_and_buttons,
        track_key_press,
        tracked_input,
    )

    clear_tracking()
    out = release_all_keys_and_buttons()
    injected = len(out["keys_released"]) + len(out["buttons_released"])
    check(
        "nothing tracked -> zero key/button UP events injected",
        injected == 0 and out["scope"] == "uha_tracked_only",
        f"scope={out['scope']} injected_up_events={injected} (old code always injected 38)",
    )
    track_key_press("ctrl shift a")
    check(
        "only UHA-pressed keys are recorded",
        set(tracked_input()["keys"]) == {"0x11", "0x10", "0x41"},
        tracked_input(),
    )
    release_all_keys_and_buttons()
    clear_tracking()

    # 1b. override survives the adapter's routine release
    from src.safety.controller import SafetyController, SafetyState

    c = SafetyController(state_path=Path(tempfile.mkdtemp()) / "g.json", injection_lease_s=30.0)
    c.set_banner_available(True)
    c.request_control(); c.banner_visible(ok=True); c.grant_control()
    opened = c.allow_input
    c.trigger_human_override(kind="demo")
    c.release_control(reason="cu_release")          # exactly what control()'s finally does
    survived = c.state == SafetyState.HUMAN_OVERRIDE and c.allow_input is False
    check(
        "HUMAN_OVERRIDE survives release_control()",
        opened and survived,
        f"before_override_allow_input={opened} after_release_state={c.state.value} allow_input={c.allow_input}",
    )
    try:
        c.request_control(task="executor_retry"); c.banner_visible(ok=True); c.grant_control()
        reopened = True
    except Exception:
        reopened = False
    check(
        "override cannot be re-opened by request/grant cycle",
        reopened is False and c.allow_input is False,
        f"state={c.state.value} allow_input={c.allow_input}",
    )
    c.resume_safety(explicit=True)
    check(
        "explicit resume leaves a closed gate (no instant re-enable)",
        c.state == SafetyState.IDLE and c.allow_input is False,
        f"state={c.state.value} allow_input={c.allow_input}",
    )

    # 1b2. banner-first must be a real affordance, not a self-written boolean
    c2 = SafetyController(state_path=Path(tempfile.mkdtemp()) / "g2.json")
    c2.request_control(task="no_banner")
    c2.banner_visible(ok=True)                     # nobody declared a banner exists
    try:
        c2.grant_control()
        refused = False
    except RuntimeError:
        refused = True
    check(
        "no declared banner -> grant refused (banner-first is real, not a boolean)",
        refused and c2.snapshot().banner_visible is False and c2.allow_input is False,
        f"banner_available={c2.banner_available} banner_visible={c2.snapshot().banner_visible} "
        f"allow_input={c2.allow_input}",
    )

    # 1c. injected vs physical attribution, through the production callback
    from src.safety.human_override import (
        HC_ACTION, LLMHF_INJECTED, WM_MOUSEMOVE, HumanOverrideDetector, MSLLHOOKSTRUCT,
    )

    seen: list[str] = []
    det = HumanOverrideDetector(on_physical_input=seen.append, move_throttle_s=0.0)

    def fire(flags: int) -> None:
        info = MSLLHOOKSTRUCT()
        info.pt.x, info.pt.y = 10, 10
        info.flags = flags
        det._mouse_proc(HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(info))

    fire(LLMHF_INJECTED)
    no_self_cancel = seen == []
    fire(0)
    detected = seen == ["mouse_move"]
    check(
        "interpreter of events: injected=ours, unflagged=human",
        no_self_cancel and detected,
        f"after_injected={seen[:1] or '[] (correct)'} injected_events={det.injected_events} physical_events={det.physical_events}",
    )

    # 1d. a stale gate file cannot open a closed gate
    from src.adapters.computer_use import ComputerUseAdapter
    from src.safety.controller import (
        external_gate_allows_input,
        get_safety_controller,
        reset_safety_controller_for_tests,
    )

    reset_safety_controller_for_tests()
    gate = Path(tempfile.mkdtemp()) / "gate.json"
    from src.safety import controller as controller_module
    sc = SafetyController(state_path=gate)
    controller_module._CONTROLLER = sc  # isolated offline fixture, no real input hook
    sc.set_banner_available(True)
    sc.request_control(); sc.banner_visible(ok=True); sc.grant_control()
    cu = ComputerUseAdapter(base_url="http://127.0.0.1:1", client_id="p01-demo", dry_run=True)
    cu._gate("move_mouse")
    sc.trigger_human_override(kind="demo")
    gate.write_text(json.dumps({"state": "ACTIVE", "allow_input": True, "agent_has_input_control": True}), encoding="utf-8")
    try:
        cu._gate("move_mouse")
        blocked = False
    except PermissionError:
        blocked = True
    check(
        "a permissive/stale gate file cannot re-open a live-closed gate",
        blocked and external_gate_allows_input(gate) is True,
        "live controller says HUMAN_OVERRIDE -> _gate raises even though the file says ACTIVE",
    )

    # 1e. forced release actually reaches the server with the right body
    import urllib.request

    captured: dict = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"ok": true, "result": {"released": true}}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    real = urllib.request.urlopen

    def fake(req, timeout=None):  # noqa: ANN001
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    urllib.request.urlopen = fake  # type: ignore[assignment]
    try:
        from src.safety.cleanup import force_cleanup_input

        cl = force_cleanup_input(sc)
    finally:
        urllib.request.urlopen = real  # type: ignore[assignment]
    body = captured.get("body") or {}
    check(
        "forced release is addressed the way the server reads it",
        "args" in body and body.get("args", {}).get("force") is True,
        f"posted_body={body}",
    )
    check(
        "cleanup never claims the user has control",
        cl.get("user_control_verified") is False and cl.get("human_physical_input_blocked_by_uha") is False,
        f"user_control_verified={cl.get('user_control_verified')} "
        f"human_physical_input_blocked_by_uha={cl.get('human_physical_input_blocked_by_uha')}",
    )
    reset_safety_controller_for_tests()


# ------------------------------------------------------------------- live part
def part_live(with_human: bool) -> None:
    section("2. Live: real injection through the real Computer Use service")
    from src.adapters.computer_use import ComputerUseAdapter
    from src.safety.banner import build_safety_banner
    from src.safety.controller import SafetyState, get_safety_controller, reset_safety_controller_for_tests

    banner = None
    cu = None
    origin = _cursor()
    sw, sh = _screen()
    print(f"  cursor origin = {origin}; screen = {sw}x{sh}")
    print("  note: the cursor will be moved a few pixels and restored afterwards.")

    reset_safety_controller_for_tests()
    gate = Path(tempfile.mkdtemp()) / "gate.json"
    sc = get_safety_controller(state_path=gate)

    try:
        cu = ComputerUseAdapter(base_url="http://127.0.0.1:8788", client_id="p01-demo", timeout=30)
        try:
            cu.connect(required=True)
        except Exception as exc:  # noqa: BLE001
            check(
                "Computer Use service reachable on 127.0.0.1:8788",
                False,
                f"{type(exc).__name__}: {exc} -> LIVE SECTION SKIPPED (not faked)",
            )
            return
        check("Computer Use service reachable on 127.0.0.1:8788", True)

        banner = build_safety_banner(sc, geometry="1200x84+40+8")
        sc.set_banner_available(bool(banner.available))   # declare BEFORE any claim
        banner_ok = banner.start()
        sc.request_control(task="p01_demo")
        sc.banner_visible(ok=True)
        check(
            "safety banner really exists and is visible BEFORE any injection grant",
            bool(banner_ok and banner.visible and sc.snapshot().banner_visible),
            f"backend={getattr(banner, 'backend', 'tkinter')} available={banner.available} "
            f"started={banner_ok} visible={banner.visible} flag={sc.snapshot().banner_visible}",
        )

        cu.acquire_control(wait=True)
        det = sc.human_override_detector
        check(
            "injection permission granted (lease, not ownership)",
            sc.allow_input is True and sc.snapshot().input_owner.value == "USER",
            f"state={sc.state.value} allow_input={sc.allow_input} "
            f"attribution={det.stats()['attribution']} hook_alive={det.hook_alive}",
        )

        # -- L3: the regression that started all this. The old detector cancelled the
        #        agent on its own first move; this must now run clean.
        moves = 0
        err = None
        physical_during_moves = []
        before_phys = det.physical_events
        for i in range(1, 9):
            if not sc.allow_input:
                physical_during_moves.append(i)
                break
            try:
                cu.move_mouse(
                    min(max(1, origin[0] + i * 9), sw - 2),
                    min(max(1, origin[1] + i * 6), sh - 2),
                )
                moves += 1
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                break
            sc.heartbeat()
            time.sleep(0.06)
        check(
            "agent's own injected motion no longer self-cancels the agent",
            moves == 8 and err is None,
            f"real_mouse_moves_ok={moves}/8 err={err} "
            f"(old build aborted at move 1 with state=HUMAN_OVERRIDE)",
        )
        if physical_during_moves:
            print(f"  note: a REAL physical event was detected during the moves "
                  f"(check {physical_during_moves[0]}) — that is the feature working.")

        # -- L4: human takes over
        from src.safety.human_override import HC_ACTION, WM_MOUSEMOVE, MSLLHOOKSTRUCT

        human_verified = False
        if sc.allow_input:
            if with_human and not physical_during_moves:
                print("\n  >>> MOVE YOUR MOUSE NOW (or press a key) — waiting up to 30 s ...")
                t0 = time.time()
                while time.time() - t0 < 30 and det.physical_events <= before_phys:
                    sc.heartbeat()          # an active agent keeps its lease alive
                    time.sleep(0.05)
                got = det.physical_events > before_phys
                if got:
                    human_verified = True
                    check(
                        "a REAL human movement/keypress triggers HUMAN OVERRIDE",
                        sc.state == SafetyState.HUMAN_OVERRIDE,
                        f"real_physical_events={det.physical_events - before_phys} "
                        f"state={sc.state.value} kind={det.last_physical_kind}",
                        kind="HUMAN VERIFIED",
                    )
                else:
                    check(
                        "30 s of human inactivity produced no false override",
                        sc.allow_input is True and sc.state != SafetyState.HUMAN_OVERRIDE,
                        f"physical_events={det.physical_events - before_phys} "
                        f"state={sc.state.value} allow_input={sc.allow_input}",
                    )
                    check(
                        "a REAL human movement/keypress triggers HUMAN OVERRIDE",
                        False,
                        "no physical event arrived within 30 s — not verified by this run; "
                        "re-run when you can move the mouse",
                        kind="PENDING HUMAN VALIDATION",
                    )

            if not human_verified:
                # keep exercising the rest of the chain: deliver a physical-flagged event
                # through the REAL callback and the REAL state machine
                info = MSLLHOOKSTRUCT()
                info.pt.x, info.pt.y = origin
                info.flags = 0        # <- "not injected" == physical
                det._mouse_proc(HC_ACTION, WM_MOUSEMOVE, ctypes.addressof(info))
                time.sleep(0.05)
                check(
                    "a physical event triggers HUMAN OVERRIDE (real callback + real state machine)",
                    sc.state == SafetyState.HUMAN_OVERRIDE and sc.allow_input is False,
                    f"state={sc.state.value} allow_input={sc.allow_input} kind={det.last_physical_kind}",
                    kind="SIMULATED PHYSICAL EVENT",
                )

        # -- L5: agent injection is refused immediately
        refused = None
        try:
            cu.move_mouse(origin[0], origin[1])
            refused = False
        except PermissionError as exc:
            refused = str(exc)
        check(
            "agent injection refused the moment the human takes over",
            bool(refused),
            refused or "move_mouse was NOT refused",
        )

        # -- L6: routine release must not revive the override
        r = cu.release_control(force=True)
        check(
            "release_control() does not revive the override",
            sc.state == SafetyState.HUMAN_OVERRIDE and sc.allow_input is False,
            f"state={sc.state.value} allow_input={sc.allow_input} "
            f"release_note={r.get('note', '')[:60]}...",
        )

        # -- L7: the retry loop cannot grind its way back in
        attempts = []
        for i in range(3):
            try:
                sc.request_control(task=f"retry-{i}"); sc.banner_visible(ok=True); sc.grant_control()
                attempts.append(f"attempt{i}: RE-OPENED")
            except Exception:
                attempts.append(f"attempt{i}: refused")
        check(
            "3 simulated executor retries all refused (P0.1 §10 no auto-resume)",
            all("refused" in a for a in attempts) and sc.allow_input is False,
            attempts,
        )
        try:
            cu.move_mouse(origin[0] + 1, origin[1] + 1)
            still_blocked = False
        except PermissionError:
            still_blocked = True
        check("injection still blocked after the retries", still_blocked)

        if with_human or physical_during_moves or det.physical_events > before_phys + 1:
            check("Resume after physical input requires the user", False,
                  "No automatic resume or cursor restoration after real input", kind="PENDING HUMAN VALIDATION")
            return

        # -- L8: simulated test resume, then normal operation resumes
        sc.resume_safety(explicit=True)
        check(
            "explicit resume -> IDLE with injection still closed (P0.1 §11)",
            sc.state == SafetyState.IDLE and sc.allow_input is False,
            f"state={sc.state.value} allow_input={sc.allow_input}",
        )
        sc.request_control(task="after_resume")
        sc.banner_visible(ok=True)
        cu.acquire_control(wait=True)
        resumed_ok = False
        try:
            cu.move_mouse(origin[0] + 3, origin[1] + 3)
            resumed_ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"         post-resume move failed: {type(exc).__name__}: {exc}")
        check(
            "after an explicit resume, Computer Use works again (fix is not a lock-out)",
            resumed_ok and sc.allow_input is True,
            f"state={sc.state.value} allow_input={sc.allow_input}",
        )

        # -- L9: cleanup + honest reporting
        from src.safety.cleanup import force_cleanup_input

        cl = force_cleanup_input(sc, computer_use=cu)
        rel = next(s for s in cl["steps"] if s["step"] == "release_injected_keys_buttons")
        check(
            "cleanup closed the gate, injected 0 spurious UP events",
            cl["safety_state"] == "EMERGENCY_STOP"
            and rel["keys_released"] == [] and rel["buttons_released"] == [],
            f"state={cl['safety_state']} pending_before={rel['pending_before']} "
            f"released={rel['keys_released']}+{rel['buttons_released']} already_up={rel['already_up']}",
        )
        tars = next(s for s in cl["steps"] if s["step"] == "agent_tars_force_release")
        check(
            "forced release of the CU desktop lock actually succeeded",
            tars["ok"] is True,
            f"{tars}",
        )
        detsto = next((s for s in cl["steps"] if s["step"] == "detector_stopped"), None)
        check(
            "low-level observer unhooked by cleanup",
            bool(detsto and detsto.get("ok")),
            f"{detsto}",
        )
        try:
            banner.stop()
        except Exception:
            pass
        cu.close()
    finally:
        # Never inject a cursor restoration after an actual human takeover.
        sc.release_control(reason="demo_finally")
        if cu is not None:
            try: cu.release_control()
            except Exception: pass
            cu.close()
        if banner is not None: banner.stop()
        reset_safety_controller_for_tests()
        print(f"\n  Cursor left at {_cursor()}; no post-takeover repositioning")


# ---------------------------------------------------------------- post-checks
def part_post() -> None:
    section("3. Post-run: the OS is still entirely the user's")

    from src.safety.diagnostics import collect_safety_status

    st = collect_safety_status()
    pol = st["os_blocking_zero_policy"]
    check(
        "zero-policy: no blocking / suppression / grab",
        not any([st["user_input_blocking"], st["user_input_suppression"],
                 st["exclusive_mouse_grab"], st["exclusive_keyboard_grab"]]),
        pol,
    )

    u = ctypes.WinDLL("user32")
    u.GetAsyncKeyState.restype = ctypes.c_short
    stuck = [n for n, vk in (("L", 0x01), ("R", 0x02), ("M", 0x04),
                             ("shift", 0x10), ("ctrl", 0x11), ("alt", 0x12), ("win", 0x5B))
             if u.GetAsyncKeyState(vk) & 0x8000]
    check("no injected key/mouse button left down", stuck == [], f"down={stuck}")

    rect = wt.RECT()
    u.GetClipCursor(ctypes.byref(rect))
    sw, sh = _screen()
    confined = not (rect.left <= 0 and rect.top <= 0 and rect.right >= sw and rect.bottom >= sh)
    check("no cursor constraint held", not confined, f"clip={[rect.left, rect.top, rect.right, rect.bottom]} screen={sw}x{sh}")

    # input-chain health: a correctly-pumped observe-only hook measures dispatch latency
    from tools.p01_input_forensics import section_hook_latency

    lat = section_hook_latency(samples=6)
    check(
        "input chain healthy (no foreign hook stalling dispatch)",
        bool(lat.get("hook_installed")) and (lat.get("max_ms") or 9999) < 50,
        f"median={lat.get('median_ms')} ms max={lat.get('max_ms')} ms callbacks={lat.get('callbacks_received')}",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--human", action="store_true",
                    help="wait for a REAL mouse movement / keypress instead of simulating it")
    args = ap.parse_args()

    print("UHA P0.1 — Human Override demonstration")
    print("=" * 70)
    print("AUTO PASS = machine-verified   SIMULATED = real code path, synthetic event")
    print("PENDING HUMAN VALIDATION = needs your hand on the mouse")

    try:
        part_offline()
        part_live(with_human=args.human)
        part_post()
    except KeyboardInterrupt:
        print("\ninterrupted")

    section("Summary")
    auto = [r for r in RESULTS if r["kind"] == "AUTO PASS"]
    sim = [r for r in RESULTS if r["kind"] == "SIMULATED PHYSICAL EVENT"]
    hum = [r for r in RESULTS if r["kind"] == "HUMAN VERIFIED"]
    pend = [r for r in RESULTS if r["kind"] == "PENDING HUMAN VALIDATION"]
    for r in RESULTS:
        print(f"  {'PASS' if r['ok'] else 'FAIL' if r['kind'] != 'PENDING HUMAN VALIDATION' else 'PEND'}"
              f"  {r['check']}")
    print()
    print(f"  AUTO PASS                  : {sum(1 for r in auto if r['ok'])}/{len(auto)}")
    if sim:
        print(f"  SIMULATED PHYSICAL EVENT   : {sum(1 for r in sim if r['ok'])}/{len(sim)}")
    print(f"  HUMAN VERIFIED             : {sum(1 for r in hum if r['ok'])}/{len(hum)}")
    print(f"  PENDING HUMAN VALIDATION   : {len(pend)}")
    if not hum:
        print("     (run with --human and move the mouse when prompted to convert this)")
    # a PENDING item is not a failure — it is an unperformed physical test (P0.1 §21)
    failed = [r for r in RESULTS if not r["ok"] and r["kind"] != "PENDING HUMAN VALIDATION"]
    print(f"  FAILURES                   : {len(failed)}")
    out = Path("logs/runs") / f"p01_override_demo_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "human_mode": args.human, "results": RESULTS,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  evidence: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
