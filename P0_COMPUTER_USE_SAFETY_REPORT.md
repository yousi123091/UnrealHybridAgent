# P0 Computer Use Safety Report

**Project**: UnrealHybridAgent · Branch `p0-cu-safety` (from `phase4b` @ `949de97`)  
**Date**: 2026-09-21  
**Status**: P4B feature development **PAUSED** until gates reviewed below.

---

## Root Cause

Phase 4A/4B Computer Use paths could inject mouse/keyboard through Agent-TARS while:

1. Emergency Stop lived mainly **inside** the UHA agent/controller path — if agent/event-loop blocked, EStop degraded.
2. Input APIs (`move_mouse`, `click`, `type_text`, …) called CU **without a unified injection gate**.
3. Overlay/HUD was small and task-oriented; there was **no always-on-top screen banner** stating “Computer Use Active” + hotkey.
4. `acquire_control` did not enforce **banner-before-control** or **fail-closed** on safety layer failure.
5. Resume semantics were ambiguous — agent/task state could imply input ownership incorrectly.

Result: user could lose practical control of the machine with weak visual warning and incomplete interrupt guarantees.

---

## Architecture (delivered)

```
User
  │ Ctrl+Alt+F12 / Banner STOP
  ▼
SafetyController (independent, minimal, file-backed gate)
  │ allow_input=false FIRST
  │ cancel event
  │ release keys/buttons (hooks)
  │ release CU lock / locks (hooks)
  ▼
Input Safety Gate
  │ every CU mouse/keyboard tool
  ▼
Agent-TARS Computer Use → OS Input

Safety Banner (always-on-top, independent of agent HUD)
Watchdog helper: src/safety/watchdog.py (optional external process)
State file: .state/safety_gate.json (fail-closed when unknown/estop)
```

Priority implemented:

**USER CONTROL > CU SAFETY > TASK > P4B > UI**

---

## Emergency Stop

| Item | Value |
|---|---|
| Primary hotkey | **Ctrl+Alt+F12** (config `desktop.emergency_hotkey`) |
| Alternate | Banner **STOP** button |
| Implementation | `src/desktop/hotkey.py` RegisterHotKey, fallback **polling GetAsyncKeyState** |
| Observed mode this machine | **polling** (RegisterHotKey not available in test process) |
| Short path | hotkey → `SafetyController.emergency_stop` → gate closed → hooks |
| Gate close latency (in-process) | **1.03–1.53 ms** samples (plus optional SendInput release) |
| Independent estop after “hang” sim | **27.5 ms** sample |
| Auto-resume | **Forbidden** — only `resume_safety(explicit=True)` |
| Safety > agent state | Agent cannot `grant_control` over EMERGENCY_STOP |

### EStop sequence
1. `allow_input=False` immediately  
2. Input owner → **USER**; `agent_has_input_control=False`  
3. Action cancel event set (pending GUI actions raise)  
4. Hooks: release keys/buttons → CU `release_control(force)` → coordinator force release → session ABORTED  
5. Banner shows **EMERGENCY STOPPED / User control restored**

---

## Safety Banner

| State | Visual |
|---|---|
| Active | ⚠ UHA COMPUTER USE ACTIVE + hotkey + task |
| Waiting | Session active / waiting — **still visible** |
| Emergency | ⛔ EMERGENCY STOPPED / user control restored |
| Finished | ✓ finished then hide |

Rules:
- Always on top (`tkinter`, independent thread)
- **Banner visible before grant_control** (grant refused otherwise)
- Shows **Ctrl+Alt+F12 = Emergency Stop**
- STOP button on banner
- Does not register Ctrl+Alt+Delete / Win+L
- Banner crash must not disable EStop path (hotkey/controller independent) — tested

---

## Failure Handling (fail closed)

| Condition | Behavior |
|---|---|
| EMERGENCY_STOP | CU acquire/input denied |
| No banner before grant | `grant_control` raises; CU not allowed |
| Heartbeat timeout | auto EMERGENCY_STOP, input disabled |
| External gate file `state=EMERGENCY_STOP` / unknown JSON | `external_gate_allows_input=False` |
| Safety init/selfcheck fail | **Computer Use disabled** at UHA startup |
| Worker crash / unknown | default deny input |
| Agent running ≠ owns mouse | ownership is `InputOwner` / `agent_has_input_control` |

---

## Tests (real machine + offline)

### Offline
`tests/test_p0_safety.py` **8/8 PASS**
(estop gate, banner-before-grant, explicit resume, heartbeat, external gate, hotkey text, safety>agent, selfcheck)

### Real machine (`tools/p0_cu_safety_test.py`)
Evidence: `logs/runs/p0_cu_safety_20260921-195348.json`

| Test | Result |
|---|---|
| T1 live mouse then EStop | **PASS** — 12 moves OK, then 0 moves; blocked `state=EMERGENCY_STOP` |
| T3 action queue cancel | **PASS** — `Emergency Stop cancelled pending GUI actions` |
| T5 heartbeat timeout | **PASS** — fail closed |
| T7 external gate fail closed | **PASS** |
| T8 banner before control | **PASS** — `allow_input=false` until grant |
| T9 no auto resume | **PASS** |
| T10 OS escape not blocked | **PASS** — only ctrl+alt+f12 registered/used |
| User control restored | **PASS** |
| CU acquire | **PASS** — holder `uha-p0-safety-test` |

Computer Use service was live on `http://127.0.0.1:8788` for the live move test.

---

## Latency

| Measurement | Value |
|---|---|
| Gate close (controller only) | **~1.0–1.5 ms** |
| Independent estop sample | **~27 ms** |
| Note | These are **in-process gate** times; OS key/button release via SendInput is additional. Not fabricated “0 ms”. |

---

## Input Ownership

- `InputOwner.USER | AGENT | TRANSITION | UNKNOWN`
- `agent_has_input_control` explicit flag
- Banner driven by **CU/input ownership**, not by “Agent RUNNING”
- Restored message: **USER CONTROL RESTORED**

---

## Lifecycle

`REQUESTED → BANNER_VISIBLE → CONTROL_ACQUIRED → ACTIVE → RELEASING → RELEASED`  
EStop from any acquired/active/releasing stage → `EMERGENCY_STOP` → control released to user.

---

## Known Limitations

1. **RegisterHotKey failed in this process**; hotkey works via **polling** (~50 ms poll). Still global enough for EStop purposes but not OS-hotkey native. May improve with dedicated watchdog process / elevated registration.
2. Banner thread teardown can print `Tcl_AsyncDelete` warning on some Python builds — non-blocking.
3. Live typing-interrupt test (T2) not fully exercised with long CU type stream (server available; queue cancel + gate covered).
4. Watchdog process exists (`src/safety/watchdog.py`) but full dual-process hard-kill of a wedged main agent is only partially simulated.
5. Banner cannot guarantee click-through in all Tk setups; STOP button remains clickable; does not block Ctrl+Alt+Delete.

---

## P4B Readiness Gate

| Gate | Result |
|---|---|
| Global emergency hotkey works | **PASS** (polling mode observed) |
| Input gate works | **PASS** |
| Current action can be interrupted | **PASS** |
| Pending actions cancelled | **PASS** |
| Safety Banner appears before CU control | **PASS** |
| Emergency Stop cannot auto-resume | **PASS** |
| User control restoration verified | **PASS** |
| Safety subsystem failure disables CU | **PASS** |
| Basic failure injection tests passed | **PASS** |

**P4B remains blocked until you explicitly authorize resume.**  
All listed gates are **PASS** in this report; per process instruction P4B is **not auto-resumed**.

---

## Files

- `src/safety/controller.py` — SafetyController + input gate + state file  
- `src/safety/banner.py` — always-on-top safety banner  
- `src/safety/watchdog.py` — optional standalone watchdog  
- `src/adapters/computer_use.py` — unified input gate on all CU input tools  
- `uha.py` — safety bootstrap; fail-closed CU disable  
- `tests/test_p0_safety.py`, `tools/p0_cu_safety_test.py`  
- Evidence: `logs/runs/p0_cu_safety_20260921-195348.json`

---

## Final Principle

> 用户是否永远拥有最终控制权。  
> If task continuation conflicts with returning the computer to the user → **always return control**.
