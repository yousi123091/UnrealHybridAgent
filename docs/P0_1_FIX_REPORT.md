# UHA P0.1 — Fix Report

**Project**: UnrealHybridAgent · **Date**: 2026-09-21
**Companion to**: `docs/P0_1_ROOT_CAUSE.md` (the diagnosis)
**Status**: P4B still **BLOCKED** — all automated gates pass and **§23 (user keypress →
takeover) is `HUMAN VERIFIED`** by a real physical key event; §22 and §24–§31 are
**PENDING HUMAN VALIDATION**.

---

## 1. What was wrong, in one line

> The user's mouse was never blocked. UHA kept **taking the input back** — the agent's own
> routine `release_control()` erased `HUMAN_OVERRIDE` milliseconds after it fired, and the
> retry loop then re-opened the gate. On top of that, every takeover injected 38 synthetic
> key-up/button-up events into the user's own input stream.

`tests/test_p01_override_survival.py` reproduced it: **1/5 pass** before, 4 of the 5
failures being the real defects and 1 being the control that proved the asymmetry (the old
suite only ever exercised `EMERGENCY_STOP`, which *was* protected).

---

## 2. Fixes applied

Causality order. File:line is where the change landed.

| # | RC | Fix | Files |
|---|---|---|---|
| F1 | RC-1 | `HUMAN_OVERRIDE` and `EMERGENCY_STOP` are now a single `TERMINAL_STATES` set. `release_control()` **re-asserts "no injection" and leaves the state alone** instead of resetting it to `RELEASED`. | `src/safety/controller.py` |
| F2 | RC-1 | `request_control()` refuses for **any** terminal state, not just `EMERGENCY_STOP`. This is what kills the executor retry loop's auto-resume. | `src/safety/controller.py` |
| F3 | RC-5 | The gate is now `live_allow AND file_allow`, never `OR`. The on-disk gate file can only ever **close** the gate. Plus a whitelist: only `CONTROL_ACQUIRED`/`ACTIVE` count as injection-active, everything else (including unknown/corrupt/`HUMAN_OVERRIDE`) denies. | `src/adapters/computer_use.py`, `src/safety/controller.py` |
| F4 | — | Human Override / Emergency Stop now have an explicit **`Resume Computer Use`** control on the banner. Resume clears the queue but does *not* re-enable injection (§11). | `src/safety/banner.py`, `src/safety/banner_win32.py` |
| F5 | RC-4 | Attribution is now the **event's own injected flag** (`LLMHF_INJECTED` / `LLKHF_INJECTED`). The cursor-delta heuristic — which made the agent cancel itself on its own long moves — is **deleted**. | `src/safety/human_override.py` |
| F6 | RC-6 | The low-level observer is installed **and pumped on one dedicated thread** (the documented Windows requirement), `restype` is set (the old code truncated the 64-bit `HHOOK`, so unhooking could never have worked), the callback stays minimal and always chains, and `stop()` **is now called** — from `force_cleanup_input()`, from `reset_safety_controller_for_tests()`, and from an `atexit` hook in `uha.py`. | `src/safety/human_override.py`, `src/safety/cleanup.py`, `src/safety/controller.py`, `uha.py` |
| F7 | RC-3 | `release_all_keys_and_buttons()` is now **ledger-driven**: it releases only the keys/buttons UHA actually pressed since it last released them, and checks `GetAsyncKeyState` before emitting an UP. The 38-event blanket is gone (kept as a hand-operated escape hatch `release_everything_now(explicit=True)`). | `src/desktop/hotkey.py` |
| F8 | RC-7 | The forced release posted `{"tool":…, "arguments":…}` but the server destructures `{tool, args}` — so it had **never worked**. Fixed, and the response body is now parsed so a failure cannot pass as success. | `src/safety/cleanup.py`, `src/safety/watchdog.py` |
| F9 | — | `tools/p0_safety_daemon.py` no longer forces `EMERGENCY_STOP` into the shared state file at startup and every second. It was a second safety authority racing the real controller. Now it is a banner + hotkey only. | `tools/p0_safety_daemon.py` |
| F10 | §32 | `release_control()` **and** `force_cleanup_input()` now carry an explicit note that they prove nothing about physical user control, and report `user_control_verified: False`. | `src/adapters/computer_use.py`, `src/safety/cleanup.py` |

---

## 3. Three extra defects found *while fixing* (D-8 … D-10)

These were not in the original diagnosis. They surfaced because the demo was run for real.

### D-8 — "banner before control" was guaranteed on paper and absent in practice

`grant_control()` refused unless `snapshot().banner_visible`, and `banner_visible` was a
boolean the agent wrote to itself:

```python
# old ComputerUseAdapter.control()
if not safety.snapshot().banner_visible:
    safety.banner_visible(ok=True)      # self-assertion
```

Meanwhile the banner **could not be shown at all**: `import tkinter` fails on the project
`.venv` and on the managed Python 3.13 — only the system Python 3.12 ships `_tkinter`.
So the guarantee held while no banner existed.

Fixed by making it real:
* `SafetyController.set_banner_available()` — an explicit declaration, default **False**.
* `banner_visible(ok=True)` is a no-op unless availability was declared.
* `grant_control()` refuses with a clear message; `startup_selfcheck()` reports
  `no_safety_banner_available` and disables Computer Use (fail-closed).
* **New `src/safety/banner_win32.py`** — a pure `ctypes` banner (needs only
  `user32`/`gdi32`), selected automatically by `build_safety_banner()`. Shape:
  * a **click-through strip** (`WS_EX_LAYERED | WS_EX_TRANSPARENT`) for the status text — it
    is physically incapable of eating the user's clicks;
  * a small **buttons window** (~366×42, docked right, only shown in a terminal state)
    holding `Resume Computer Use` and `STOP (secondary)`.
* `tools/p0_safety_banner_win32.py` was reduced to a shim (see D-9).

### D-9 — the old pure-Win32 banner had never run

It referenced `wt.HCURSOR`, which does not exist in `ctypes.wintypes`, so it raised
`AttributeError` at import. It also read state from the file rather than the controller, had
no Resume control, and being `WS_EX_TRANSPARENT` its "click anywhere = STOP" handler could
never fire. Replaced by `src/safety/banner_win32.py`.

### D-10 — third-party escape hatch documented but not implemented

`@computer-use/…/config/default.json` style key `failSafeCorner` ("moving the pointer to
(0,0) aborts the running action") is declared in `E:\MCP\Agent-TARS\server\config.js:33` and
referenced **nowhere**. Not wired — recorded so nobody trusts it.

### D-11 — any CU step slower than 8 s EStopped the task in the middle of its own work

Found because the `--human` demo waits 30 s for a real keypress: the run came back
`state=EMERGENCY_STOP` with **zero** physical events. Cause: `ComputerUseAdapter._gate()`
called `safety.check_heartbeat()` but never refreshed it, and `wait()` did not refresh
either. So a task whose steps are more than `heartbeat_timeout_s` (default **8 s**) apart —
a `wait(5000)` settle, a slow drag, a VLM round-trip — tripped the watchdog and stopped
itself. The watchdog is right to stop a *dead* worker; it must not stop a *working* one.

Fixed:
* `_gate()` now **refreshes liveness first** (`safety.heartbeat()`), then uses its verdict as
  the gate. Injecting implies being alive.
* `wait()` is chunked (≤2 s per call) and heartbeats between chunks, so a long settle cannot
  look like a dead worker; it also aborts immediately on a terminal state instead of
  sleeping through it.
* The watchdog's cross-process protection is unchanged: when the agent really stops acting,
  the heartbeat still goes stale.

---

## 4. Evidence

### 4.1 Offline — `tests/test_p01_override_survival.py` (was 1/5)

```
PASS test_human_override_survives_release_control
PASS test_no_auto_regrant_after_override
PASS test_release_is_still_normal_when_not_terminal
PASS test_emergency_stop_survives_release_control
PASS test_only_explicit_resume_leaves_terminal
PASS test_flag_based_attribution_ignores_injected_motion
PASS test_hook_callback_never_swallows_input
PASS test_detector_stop_is_idempotent_and_unhooks
PASS test_reset_controller_stops_detector
PASS test_external_file_cannot_open_unknown_states
PASS test_gate_is_fail_closed_in_terminal_state
PASS test_release_only_touches_tracked_input
PASS test_release_everything_requires_explicit_flag
PASS test_unresolvable_key_is_reported_not_silently_dropped
PASS test_force_release_posts_the_key_the_server_reads
PASS test_cleanup_never_claims_user_control
test_p01_override_survival: 16/16
```

### 4.2 Full regression — 571 checks, 0 failures

| Suite | Result |
|---|---|
| `tests/test_offline.py` | 248 / 248 |
| `uah/tests/test_uah_phase1.py` | 152 / 152 |
| `tests/test_phase2.py` | 85 / 85 |
| `tests/test_router.py` | 25 / 25 |
| `tests/test_p01_override_survival.py` | 16 / 16 |
| `tests/test_phase4.py` | 13 / 13 |
| `tests/test_phase3.py` | 11 / 11 |
| `tests/test_p0_safety.py` | **9 / 9** (was 7/8 before this pass; the 8th was a pre-existing failure — see below) |
| `tests/test_p01_human_override.py` | 6 / 6 |
| `tests/test_phase4b.py` | 6 / 6 |
| **total** | **571 / 571** |

Pre-existing failure fixed along the way: `test_banner_text_shows_hotkey` asserted
`"EMERGENCY STOP"` and `"User"` in the banner text; the uncommitted P0 banner rewrite had
lowercased the first and reworded the second away. Banner text normalised (also matches
P0.1 §17/§8 wording).

### 4.3 Live — `tools/p01_override_demo.py` → **27/27 AUTO PASS + 1/1 SIMULATED, 0 failures**

Final run of this pass: `logs/runs/p01_override_demo_20260921-210849.json` (28 checks).
It drives the **real** Agent-TARS Computer Use service on `127.0.0.1:8788`, the **real**
Win32 banner, the **real** low-level observer and the **real** `SafetyController`.

```
=== 1. Offline: the mechanisms themselves
  [PASS] nothing tracked -> zero key/button UP events injected
         scope=uha_tracked_only injected_up_events=0 (old code always injected 38)
  [PASS] only UHA-pressed keys are recorded
         {"keys": ["0x10", "0x11", "0x41"], "buttons": [], "unresolved": []}
  [PASS] HUMAN_OVERRIDE survives release_control()
         before_override_allow_input=True after_release_state=HUMAN_OVERRIDE allow_input=False
  [PASS] override cannot be re-opened by request/grant cycle
  [PASS] explicit resume leaves a closed gate (no instant re-enable)
  [PASS] no declared banner -> grant refused (banner-first is real, not a boolean)
         banner_available=False banner_visible=False allow_input=False
  [PASS] interpreter of events: injected=ours, unflagged=human
  [PASS] a permissive/stale gate file cannot re-open a live-closed gate
  [PASS] forced release is addressed the way the server reads it
         posted_body={'tool': 'computer_release_control', 'args': {'clientId': ..., 'force': True}}
  [PASS] cleanup never claims the user has control
         user_control_verified=False human_physical_input_blocked_by_uha=False

=== 2. Live: real injection through the real Computer Use service
  [PASS] Computer Use service reachable on 127.0.0.1:8788
  [PASS] safety banner really exists and is visible BEFORE any injection grant
         backend=win32_ctypes available=True started=True visible=True flag=True
  [PASS] injection permission granted (lease, not ownership)
         state=CONTROL_ACQUIRED allow_input=True attribution=injected_flag hook_alive=True
  [PASS] agent's own injected motion no longer self-cancels the agent
         real_mouse_moves_ok=8/8 err=None (old build aborted at move 1 with state=HUMAN_OVERRIDE)
  [PASS] a physical event triggers HUMAN OVERRIDE (real callback + real state machine)
         state=HUMAN_OVERRIDE allow_input=False kind=mouse_move
  [PASS] agent injection refused the moment the human takes over
         Safety gate blocked agent injection (move_mouse): state=HUMAN_OVERRIDE; live safety gate closed
  [PASS] release_control() does not revive the override
  [PASS] 3 simulated executor retries all refused (P0.1 §10 no auto-resume)
         ["attempt0: refused", "attempt1: refused", "attempt2: refused"]
  [PASS] injection still blocked after the retries
  [PASS] explicit resume -> IDLE with injection still closed (P0.1 §11)
  [PASS] after an explicit resume, Computer Use works again (fix is not a lock-out)
         state=ACTIVE allow_input=True
  [PASS] cleanup closed the gate, injected 0 spurious UP events
  [PASS] forced release of the CU desktop lock actually succeeded
  [PASS] low-level observer unhooked by cleanup

=== 3. Post-run: the OS is still entirely the user's
  [PASS] zero-policy: no blocking / suppression / grab
  [PASS] no injected key/mouse button left down      [PASS] no cursor constraint held
  [PASS] input chain healthy (no foreign hook stalling dispatch)
         median=2.53 ms max=4.39 ms callbacks=12

  AUTO PASS                : 27/27
  SIMULATED PHYSICAL EVENT : 1/1
  FAILURES                 : 0
```

The cursor was restored to its original position `(309, 810)`; the run used a **temporary**
safety-state file, so the real `.state/safety_gate.json` was not driven by the demo.

### 4.4 Live with a real human event — `python -m tools.p01_override_demo --human`

**27/27 AUTO PASS, 1/1 HUMAN VERIFIED, 0 failures, 0 pending.**
Evidence: `logs/runs/p01_override_demo_20260921-210358.json`

```
>>> MOVE YOUR MOUSE NOW (or press a key) — waiting up to 30 s ...
[PASS] a REAL human movement/keypress triggers HUMAN OVERRIDE  (HUMAN VERIFIED)
       real_physical_events=2 state=HUMAN_OVERRIDE kind=keyboard
[PASS] agent injection refused the moment the human takes over
       Safety gate blocked agent injection (move_mouse): state=HUMAN_OVERRIDE; live safety gate closed
[PASS] release_control() does not revive the override
[PASS] 3 simulated executor retries all refused (P0.1 §10 no auto-resume)
[PASS] explicit resume -> IDLE with injection still closed (P0.1 §11)
[PASS] after an explicit resume, Computer Use works again (fix is not a lock-out)
[PASS] cleanup closed the gate, injected 0 spurious UP events
[PASS] no injected key/mouse button left down
[PASS] no cursor constraint held
[PASS] input chain healthy  (median 2.92 ms)
```

This is the run that matters. A **genuine, OS-delivered physical key event** (not injected —
unflagged, which is why the hook classified it as human) arrived while the agent held
injection permission, and the whole chain behaved as P0.1 requires: override latched,
injection refused, `release_control()` did not revive it, three retries did not revive it,
and only an explicit resume brought Computer Use back — after which it still worked.

Caveats, stated plainly:
* The observed `kind` was **`keyboard`**, not `mouse_move` — so §22 (mouse takeover) is still
  only auto-verified. The final run of this pass exercised the **mouse** path as a SIMULATED
  physical event (`kind=mouse_move`) through the same `WM_MOUSEMOVE` + `LLMHF_INJECTED`
  check, but no human hand produced it. Re-run `--human` and move the mouse (not press a
  key) to close §22.
* §24–§31 (CU finished / worker crash / agent crash / safety-controller crash / drag / held
  key / 10× start-finish regression) are still unperformed.

---

### 4.5 Live — §33 diagnostics command

```
$ python -m uha safety --status
=== UHA Safety Status (P0.1) ===
safety_state: IDLE  terminal=False
human_override: False
agent_injection_permission: CLOSED
physical_input_owner: USER_ALWAYS
agent_exclusive_ownership: False
attribution_mode: injected_flag  hook_alive=True
lifecycle: RELEASED
banner_state: hidden processes=[]
hotkey: ctrl+alt+f12 registered=False
cu_lock: {"holder": null, ...}
user_input_blocking: False
user_input_suppression: False
exclusive_mouse_grab: False
exclusive_keyboard_grab: False
detector: {"hook_mode": "ll_flag_based", "hook_alive": true, "attribution": "injected_flag",
           "physical_events": 0, "injected_events": 0, "human_override_count": 0,
           "blocking_hooks": false, "hooks_consume_events": false, "swallows_user_input": false}

HUMAN INPUT ALWAYS WINS.
Agent only has injection permission — never exclusive ownership.
user_control_verified: False (-> physical observation is PENDING HUMAN VALIDATION, P0.1 §32)
```

Note the last line: the diagnostics command itself refuses to claim the user has control.
`hotkey … registered=False` is honest — it is still polling mode (see §5.2).

---

## 5. What is still NOT verified

Per P0.1 §21/§32, stated plainly.

### 5.1 What IS human-verified

Exactly one item, and it is the one that used to fail:

* **§23 — a real human keypress takes over from the agent.** Verified by an OS-delivered
  physical key event (unflagged → classified human by the hook), evidence
  `logs/runs/p01_override_demo_20260921-210358.json`, recorded as `HUMAN VERIFIED 1/1`.
  Full chain observed: override latched → injection refused → `release_control()` did not
  revive it → 3 executor retries refused → explicit resume restored Computer Use
  (with the gate still closed, per §11) → cleanup injected 0 spurious UP events.

### 5.2 What is NOT verified

* **§22 — mouse-move takeover.** The verified run recorded `kind=keyboard`. The mouse path
  shares the same `WM_MOUSEMOVE` + `LLMHF_INJECTED` check and is covered by
  `test_flag_based_attribution_ignores_injected_motion` plus the SIMULATED check, but no
  human hand moved a mouse during a run. Close it with:

  ```bash
  E:\UnrealHybridAgent\.venv\Scripts\python.exe -m tools.p01_override_demo --human
  ```

  It prints a prompt and waits up to 30 s. **Move the mouse** (do not press a key). That
  check is then recorded `HUMAN VERIFIED` for the mouse path. (If no physical event arrives
  within 30 s the run now reports that as `PENDING HUMAN VALIDATION` and separately asserts
  the reverse property — that 30 s of inactivity produced **no false override**.)
* **§24–§31** (CU finished / worker crash / agent crash / safety-controller crash / drag /
  held key / mid-action takeover / 10× start-finish regression) need a live CU task of yours
  plus process kills. Say the word and I will build that harness; the sign-off stays yours.
* **§22's "you must not have to fight the agent for the cursor"** is RC-2 and is *mitigated*,
  not designed away: nut-js still drives the pointer at ~3600 positions/second during a move
  (`@ui-tars/operator-nut-js` sets `mouseSpeed = 3600`), reaching the OS through
  `SetCursorPos`, which **bypasses the hook chain entirely** (measured: 0 low-level events).
  So while the agent is mid-move your physical movement is still overwritten. What changed is
  that you no longer have to *win*: the next physical event you produce is flagged as human
  and takes the pointer permanently, and it stays taken. Making "human wins even mid-move"
  true means changing the Computer Use backend, not the safety layer.
* **§32 remains in force.** No line in this report or in the code claims
  `user_control_verified: True`. `release_control()` and `force_cleanup_input()` both report
  `user_control_verified: False` by design.

---

## 6. Files touched

**Changed**
`src/safety/controller.py` · `src/safety/human_override.py` · `src/safety/cleanup.py` ·
`src/safety/banner.py` · `src/safety/diagnostics.py` · `src/safety/watchdog.py` ·
`src/adapters/computer_use.py` · `src/desktop/hotkey.py` · `uha.py` ·
`tools/p0_safety_daemon.py` · `tools/p0_cu_safety_test.py` · `tools/p0_safety_banner_win32.py` ·
`tests/test_p0_safety.py` · `tests/test_p01_human_override.py` · `tests/test_p01_override_survival.py`

**New**
`src/safety/banner_win32.py` · `tools/p01_input_forensics.py` ·
`tools/p01_injection_attribution_probe.py` · `tools/p01_override_demo.py` ·
`tests/test_p01_override_survival.py` · `docs/P0_1_ROOT_CAUSE.md` · `docs/P0_1_FIX_REPORT.md`

**Not touched (deliberately)**
`E:\MCP\Agent-TARS\**` — the injected-flag facts are recorded in
`tools/p01_injection_attribution_probe.py`; if the operator ever switches from
`SetCursorPos` to `SendInput`, flag-based attribution keeps working (measured 5/5 flagged).
