# UHA P0.1 — Human Override / Input Safety · Root Cause Report

**Project**: UnrealHybridAgent · **Date**: 2026-09-21 · **Status**: P4B **BLOCKED** (unchanged)
**Scope of this document**: only §13 of the P0.1 directive — *find the real reason the mouse
stays unusable after Computer Use ends*. No P4B work. No further Emergency-Stop patching.

---

## 0. TL;DR

**The user's mouse and keyboard were never blocked by the OS. Nothing in the entire stack
blocks physical input.** Both halves of that claim are now measured, not asserted:

| Probe | Result | Meaning |
|---|---|---|
| `libnut.node` import table (the actual injection library) | only `SendInput`, `GetCursorPos` | no `BlockInput`, no `SetWindowsHookEx`, no `ClipCursor`, no `SetCapture` |
| Live `GetClipCursor()` | `(0,0,1707,1067)` == screen → `cursor_confined=False` | nothing confines the pointer |
| Live `GetAsyncKeyState()` for L/R/M/X buttons + Shift/Ctrl/Alt/Win | every one `0x0` | no stuck injected button/key |
| Live topmost window census (21 visible windows) | 0 topmost windows covering the cursor; window at cursor = Chrome | no overlay eating clicks |
| Live low-level hook dispatch latency (own correctly-pumped LL mouse hook) | **1.38 / 2.02 / 4.51 ms** (min/median/max, n=12, 0 timeouts) | **the input chain is healthy — no foreign hook is stalling it** |

So the observed symptom is **not** "the OS stopped delivering my input". It is:

> **UHA keeps taking the input back.** The human override is *revoked by the agent itself*,
> milliseconds after it fires, through a code path that neither the agent nor the user
> can see — and it also injects **38** key-up/button-up events (35 keys + 3 buttons) into the
> user's own input stream.

The two mechanisms that do this are **proven by executable tests** (4/5 red, §4) — not by
reading alone — and both sit in code I was asked not to touch, which is why "release()"
always reported success while the user still had no mouse.

**Honest limitation (P0.1 §21/§32)**: which of the two mechanisms dominated in any *specific*
episode of "the mouse felt dead" can only be attributed with a human-in-the-loop
reproduction. Those items are marked **PENDING HUMAN VALIDATION** and are **not** called
PASS anywhere in this document.

---

## 1. Root cause

### RC-1 (primary) — `HUMAN_OVERRIDE` is erased by the agent's own `release_control()`

`SafetyController.release_control()` protects only `EMERGENCY_STOP`:

```python
# src/safety/controller.py:314
def release_control(self, *, reason: str = "released") -> SafetySnapshot:
    with self._lock:
        if self._snapshot.state == SafetyState.EMERGENCY_STOP:   # <-- ONLY this one
            ...
            return self.snapshot()
        self._snapshot.state = SafetyState.RELEASING
        ...
        self._snapshot.state = SafetyState.RELEASED               # <-- HUMAN_OVERRIDE erased
        self._write_state("release")
```

`ComputerUseAdapter.control()` calls exactly that, **unconditionally, in `finally`**:

```python
# src/adapters/computer_use.py:320-329
self.acquire_control(wait=wait)
try:
    yield
finally:
    if release:
        try:
            self.release_control()          # -> controller.release_control(...)
```

And `request_control()` only refuses when the state is `EMERGENCY_STOP` — an override in
progress does not stop it:

```python
# src/safety/controller.py:213
def request_control(self, *, task: str = "") -> SafetySnapshot:
    with self._lock:
        if self._snapshot.state == SafetyState.EMERGENCY_STOP:    # <-- ONLY this one
            raise RuntimeError(...)
        self._snapshot.state = SafetyState.REQUESTED              # <-- override state gone
```

**Composed with the executor's per-attempt re-acquisition** (`src/scheduler/executor.py:521-527`,
which fires on every `_attempt` for a MOUSE/KEYBOARD method, and
`max_attempts_per_method = 2` plus method fallback), this yields a loop:

```
user moves mouse
      │
      ▼
detector → trigger_human_override()          allow_input = False, state = HUMAN_OVERRIDE
      │
      ▼
control() exits → finally → release_control()   ← HUMAN_OVERRIDE silently erased, RELEASED
      │
      ▼
executor retries skill → _attempt → request_control() → banner_visible()
                                    → acquire_control() → grant_control()
      │
      ▼
      ALLOW_AGENT_INPUT = True again            ← nobody asked the user
      │
      ▼
nut-js moves the cursor again  ────────────►  back to the top
```

**This is precisely the failure the §2/§5/§10 requirements forbid**, and it explains all
five of the user's observations:

| Observation | Explanation |
|---|---|
| 1. User cannot use the mouse during CU | the agent's `SetCursorPos` conveyor (RC-2) outruns the physical device |
| 2. Stop Button is not a reliable safety mechanism | the cursor is being driven; you cannot steer to a button while it is being re-written every ~0.3 ms |
| 3. Agent reports it released control | `release_control()` **did** run — it just means "gate flag cleared", not "user can use the machine" |
| 4. Mouse still unusable after CU finished | the override was erased, so the next attempt/fallback re-granted and started injecting again |
| 5. `Agent thinks released ≠ OS input restored` | the gate is a **process-local, self-reopening boolean**, not a property of the OS |

### RC-2 (contributing) — the agent overwrites the cursor continuously while it acts

`@ui-tars/operator-nut-js` → `mouse.move(straightTo(target))` → nut-js `MouseClass.move()`:

```js
// @computer-use/nut-js/dist/lib/mouse.class.js:64
const timeSteps = calculateMovementTimesteps(pathSteps.length, this.config.mouseSpeed, movementType);
for (let idx = 0; idx < pathSteps.length; ++idx) {
    await busyWaitForNanoSeconds(timeSteps[idx]);
    await this.setPosition(node);          // SetCursorPos, once per Bresenham step
}
```

with `mouse.config.mouseSpeed = 3600` set per call (`@ui-tars/operator-nut-js/dist/index.js:98`)
and `calculateStepDuration = 1e9 / speed` → **one cursor re-write every ~0.278 ms along a
full pixel-by-pixel path**. A move across this 1707 px screen is ≈ 1700 steps. Any physical
mouse movement inside that window is undone before it can be observed — the cursor is
literally re-positioned ~3600 times per second. That is what "I cannot use my mouse" feels
like. It is *not* a block; it is a **race the human cannot win**, and §2 says the human must
never have to race.

Relevant chain length: `src/skills/gui_helpers.py:120,146,164` hold **4–6 chained CU actions
inside a single `cu.control()` block** (click → `ctrl a` → `delete` → type → wait 400 ms →
screenshot), so a single skill call can own the cursor for seconds; with 2 attempts/method
plus fallbacks, tens of seconds.

### RC-3 (contributing, direct user-input corruption) — UHA injects key-up/button-up events the user is physically holding

`trigger_human_override()` fires the same hook list as E-Stop:

```python
# src/safety/controller.py:403
self._notify_estop_hooks()
```

and `uha.py` registers a hook that blankets the user's input state:

```python
# uha.py:86-102  ->  src/desktop/hotkey.py:87
def release_all_keys_and_buttons():
    for vk in _RELEASE_VKS:     # Shift Ctrl Alt Win, A..Z, Enter Esc Space Tab
        SendInput(KEYUP)
    for flag in (LEFTUP, RIGHTUP, MIDDLEUP):
        SendInput(...)
```

**It releases keys and buttons UHA never pressed.** `_RELEASE_VKS` (`src/desktop/hotkey.py:36-42`)
includes all 26 letters plus Enter/Esc/Space/Tab, and it always injects
`MOUSEEVENTF_LEFTUP / RIGHTUP / MIDDLEUP`.

Consequence: **the instant the user grabs the mouse to take over, UHA fires 38 injected
UP events** (35 keyUPs + LEFTUP/RIGHTUP/MIDDLEUP) into that same input stream. If the user is
mid-drag → their drag is broken by a synthetic LEFTUP. If they are panning the UE viewport with held Space/RMB → the injected
Space-UP/RIGHTUP stops it. That is a *direct* mechanism for "I move the mouse and it goes
wrong", and it violates §14 ("only release state UHA itself established").

This also runs in `cleanup.force_cleanup_input()` (via `emergency_stop` → hooks) and in
`tools/p0_safety_banner_win32.py:100,137`.

### RC-4 (contributing) — Human Override also self-fires on the agent's *own* injected motion

`HumanOverrideDetector` decides "physical vs injected" by a cursor-delta heuristic, not by the
hook's injected flag:

```python
# src/safety/human_override.py:147-155
if time.time() - t < 0.25 and (abs(cur[0]-xy[0]) <= 3 and abs(cur[1]-xy[1]) <= 3):
    injected_like = True
if not injected_like:
    self._handle_physical("mouse_move")      # -> trigger_human_override
```

`_gate()` notes the **pre-action cursor position** (`computer_use.py:348`), then nut-js walks
the cursor to the target over 0.3–1.5 s. Every intermediate path sample is both >3 px away
and, once the note is >0.25 s old, stale → classified as **physical** → the agent cancels
itself. This is exactly the hazard §5 warns about, and it is distance-dependent: 12 short
(8 px) moves in the P0 evidence file did *not* trip it (`logs/runs/p0_cu_safety_20260921-195348.json`:
`moved_before_estop: 12, move_errors_before: []`), while a real cross-screen click will.

### RC-5 (contributing) — the on-disk gate file can *re-open* the gate (fail-open)

```python
# src/adapters/computer_use.py:333-343
def _gate(self, action: str) -> None:
    safety = get_safety_controller()
    ok = safety.allow_input
    if not ok:
        if external_gate_allows_input():   # reads .state/safety_gate.json
            ok = True                      # <-- live controller verdict discarded
```

`external_gate_allows_input()` (`controller.py:540`) returns True for any state that is not
`EMERGENCY_STOP/UNKNOWN` with `allow_input && agent_has_input_control`. A stale file (agent
crashed; second writer; the daemon's own writes) therefore **out-votes the live safety
controller**. An external signal must only ever be able to *close* the gate, never open it.

### RC-6 (latent, real defect, not yet the observed symptom) — hooks are installed the wrong way and never removed

```python
# src/safety/human_override.py:177-178
self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_cb, None, 0)
```

Three defects in two lines:

1. **No `restype`.** On 64-bit Windows `HHOOK` is 64-bit; ctypes' default `c_int` truncates it.
   `UnhookWindowsHookEx` is then given a garbage handle → unhooking fails.
2. **Wrong thread affinity.** Windows requires the *installing* thread to pump messages for a
   low-level hook to be dispatched. `start()` installs on the caller's thread, then starts
   `_msg_pump` on a **different** thread (`human_override.py:189-200`) — a pump that can never
   service that queue. In `uha.py` the installing thread is the CLI main thread, which never
   pumps.
3. **`stop()` is never called** — grep over `src/`, `uah/`, `tools/`, `tests/` shows no caller.
   The two hook handles and two threads live for the whole process lifetime.

Measured today this was **not** degrading the live input chain (2.0 ms median, §0) because no
UHA process with the detector active was running at probe time — but the defect is real,
it is the only mechanism in the codebase *capable* of a genuine system-wide input stall, and
it must go: §3/§34 forbid anything that can block input, and §5 requires hooks to be
observe-only **and correctly parked**. Recommended disposition: **REMOVE the low-level hooks**
and keep the polling detector (with RC-4 fixed). That is a deletion, not a patch.

### RC-7 — the emergency/forced release path has never worked

```python
# src/safety/cleanup.py:73-83  (identical shape in src/safety/watchdog.py:34-43)
data=_json.dumps({"tool": "computer_release_control",
                  "arguments": {"clientId": "unreal-hybrid-agent", "force": True}})
```

`server/http.js:57` destructures `const { tool, args } = req.body` → `args` is `undefined` →
`clientId` undefined → id becomes `"anonymous"`, `force` undefined → `DesktopLock.release`
throws `NOT_HOLDER` → HTTP 500 → swallowed by `except Exception: pass`. **The forced release
UHA relies on for CU-worker-crash recovery (P0.1 §15/§26) has never executed.** Note also
`/call` is a convenience endpoint, not the MCP endpoint.

---

## 2. §13 hypotheses A–I — verdict table

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| A | `BlockInput(TRUE)` not undone | **RULED OUT** | `libnut.node` imports only `SendInput`/`GetCursorPos`; `BlockInput` appears nowhere in any source; live cursor is unrestricted |
| B | Low-level hook still swallowing input | **RULED OUT** for the CU backend; **CODE DEFECT PRESENT** in UHA (`human_override.py`, RC-6). Measured input-chain latency 1.38–4.51 ms ⇒ no such hook was active at probe time | live probe §0; RC-6 |
| C | Worker/child process still alive holding input | **PARTIAL** | Agent-TARS server was **still running** (PID 58248, `node server/index.js --transport http`) after CU finished; its lock is a JS object only (holder = `null`) so it does not hold OS input. A leftover `python -m uah/tests/gui_smoke.py` (PID 67356) was also present. A live worker + a revoked override = the RC-1 loop is what actually matters |
| D | Injected mouse button / key stuck DOWN | **RULED OUT at probe time** | `GetAsyncKeyState` all `0x0`. But UHA *injects* LEFTUP/RIGHTUP/MIDDLEUP + 35 keyUPs it never pressed (RC-3) — the inverse problem, and it **breaks the user's own drags** |
| E | Un-released `ClipCursor` | **RULED OUT** | live `GetClipCursor()` = full screen. `ClipCursor` is only ever called as `ClipCursor(None)` (a release) in `cleanup.py:48` |
| F | Overlay / topmost window capturing input | **RULED OUT** | 21 windows enumerated; **zero** topmost windows covering the cursor; the only large topmost candidates are absent; the Win32 banner sets `WS_EX_LAYERED｜WS_EX_TRANSPARENT` (0x20), which *is* click-through |
| G | Library-level grab/suppress | **RULED OUT** | nut-js mouse path is `SendInput` only; `config.js:33 failSafeCorner` ("failsafe corner") is declared and **never read anywhere** — dead config, so even that escape hatch does not exist |
| H | Cleanup path skipped (finally not reached) | **CONFIRMED, opposite direction** | the `finally` **does** run — and that is the bug: it erases `HUMAN_OVERRIDE` (RC-1) |
| I | Agent Core says released, worker still active | **CONFIRMED** | `release_control()` reports success and writes `state: RELEASED` while (a) the override has been discarded, and (b) an in-flight nut-js conveyor keeps writing the cursor; `/call` force-release has never worked (RC-7) |

---

## 3. §34 — Blocking / Suppression Zero Policy

| Hard metric | Value | Evidence |
|---|---|---|
| `USER_INPUT_BLOCKING` | **FALSE** | static scan + binary import table + live measurement |
| `USER_INPUT_SUPPRESSION` | **FALSE** | no consuming hook in any process; probe hook observed events passing through |
| `EXCLUSIVE_MOUSE_GRAB` | **FALSE** | no `SetCapture` / `ClipCursor` set |
| `EXCLUSIVE_KEYBOARD_GRAB` | **FALSE** | none exists in the stack |

**The zero policy is already satisfied at the OS level.** That is exactly why adding more
Stop Buttons could never have fixed this: the problem was never blocking, it was **re-taking**.

---

## 4. Executable proof (new in this pass)

`tests/test_p01_override_survival.py` — offline, deterministic, no mouse involved.
Result: **1/5 pass** (RC-1/2/3/5 pinned; the single pass is the control that proves the asymmetry).

```
FAIL test_human_override_survives_release_control
     AssertionError: release_control wiped HUMAN_OVERRIDE -> RELEASED
FAIL test_no_auto_regrant_after_override
     AssertionError: auto-resume: request_control + grant_control re-opened injection
                      after HUMAN_OVERRIDE (state=CONTROL_ACQUIRED, allow_input=True)
PASS test_emergency_stop_survives_release_control
FAIL test_injected_movement_is_not_mistaken_for_human
     AssertionError: agent's own mid-path cursor sample is classified as PHYSICAL input
FAIL test_gate_is_not_reopened_by_stale_gate_file
     AssertionError: stale gate file re-opened agent injection
```

**Why the previous P0 suite (8/8 + T1–T10 PASS) was green while reality was broken:** it only
ever exercised `EMERGENCY_STOP`, which *is* protected in both `release_control` and
`request_control`. No test in `tests/test_p0_safety.py` or `tools/p0_cu_safety_test.py` ever
combined *human override* with *release + re-acquire*. The control test above reproduces
exactly that blind spot.

---

## 5. New instrumentation (§33)

`tools/p01_input_forensics.py` — read-only forensics, the thing to run the next time someone
says "the agent says it stopped but my mouse does not work":

```
A  window census          topmost / covering / non-transparent window at the cursor
B  button + modifier state GetAsyncKeyState for L/R/M/X buttons + Shift/Ctrl/Alt/Win
C  cursor constraint      GetClipCursor -> ClipCursor still held?
D  input-chain latency    own observe-only LL hook, installed+pumped on the SAME thread,
                          time-boxed, always unhooked -> "is a foreign hook stalling us?"
E  CU service state       /health, lock holder, plus the force-release contract check
F  process census         leftover uha / agent-tars / banner / daemon processes
G  static API audit       forbidden-API scan + libnut native import table
```

Run: `python -m tools.p01_input_forensics --json logs/runs/p01_forensics.json`
(`--no-hook` for a fully non-invasive pass). This run is preserved as
`logs/runs/p01_forensics.json`.

---

## 6. §35 Gate status

Legend: **PASS** = proven by automated evidence. **FAIL** = proven broken. **PENDING HUMAN
VALIDATION** = needs the user's own physical experience. Nothing is written PASS on the basis
of a returned `True`.

> **Status update (fix pass applied)**: gates 3, 4, 5, 7, 8, 9, 12 were FAIL at diagnosis
> time; they are now covered by the fixes in `docs/P0_1_FIX_REPORT.md` and by
> `tools/p01_override_demo.py` (27/27 AUTO PASS).
> **One item is now `HUMAN VERIFIED`, not simulated**: §23 (user keypress → takeover) was
> signed by a real OS-delivered physical key event during the `--human` run
> (`logs/runs/p01_override_demo_20260921-210358.json`, `kind=keyboard`).
> §22 and §24–§31 remain **PENDING HUMAN VALIDATION**.

| # | Gate item | Status at diagnosis | Status now | Note |
|---|---|---|---|---|
| 1 | Root cause of current stuck mouse identified | **PASS (analysis)** | **PASS** | RC-1/RC-2/RC-3 proven; exact per-episode attribution = PENDING HUMAN VALIDATION |
| 2 | No physical user input blocking | **PASS** | **PASS** | §3 table |
| 3 | Human mouse movement overrides Agent | **FAIL** | **PASS (auto)** | override is now terminal; no re-grant. Human-experience side = PENDING |
| 4 | Human keyboard input overrides Agent | **FAIL** | **PASS — HUMAN VERIFIED** | a real physical key event took over: `real_physical_events=2 state=HUMAN_OVERRIDE kind=keyboard`; injection refused, 3 retries refused |
| 5 | Agent injected events correctly distinguished | **FAIL** | **PASS** | injected-flag attribution; cursor-delta heuristic deleted |
| 6 | Emergency hotkey works | **AUTO PASS** (polling) | **AUTO PASS** (polling) | `RegisterHotKey` still unavailable in-process; polling ~50 ms. Physical reliability = PENDING |
| 7 | CU completion leaves user input naturally usable | **FAIL** | **PASS (auto)** | release no longer clears a terminal state; 0 spurious UP events |
| 8 | CU crash leaves user input usable | **FAIL** | **PASS (auto)** | forced release now actually reaches the server (`args`) |
| 9 | Agent crash leaves user input usable | **FAIL** | **PASS (auto)** | stale/permissive gate file can no longer open a closed gate |
| 10 | Stuck injected mouse buttons cleaned | **PARTIAL / FAIL** | **PASS (auto)** | ledger-driven; only UHA-pressed buttons |
| 11 | Stuck injected keys cleaned | **PARTIAL / FAIL** | **PASS (auto)** | 35-key blanket replaced by a tracked set |
| 12 | No automatic resume | **FAIL** | **PASS (auto)** | proven against release + 3 retries |
| 13 | Safety diagnostics implemented | **PASS** | **PASS** | `tools/p01_input_forensics.py` (§5) |
| 14 | *(new)* Banner-first is a real affordance | — | **PASS** | found during the fix: see `P0_1_FIX_REPORT.md` D-8 |

**P4B remains BLOCKED** until the PENDING HUMAN VALIDATION items are signed off by the user:

| Item | How to sign it off |
|---|---|
| §22 user moves mouse → takeover | `python -m tools.p01_override_demo --human`, move the mouse when prompted |
| §23 user keypress → takeover | same run, press a key |
| §24 CU finished → mouse works naturally | leave the demo, then use the mouse normally |
| §25–§27 worker / agent / safety-controller crash | kill the respective process, then use the mouse |
| §28–§30 drag, held key, mid-action takeover | needs a live CU task of yours |
| §31 10x start/finish regression | repeat a CU task 10 times |

Signed off by a real human event:
- §23 user keypress → takeover — **HUMAN VERIFIED** (2026-09-21, `--human` run).

Not yet signed:
- §22 (mouse-move takeover) — the `--human` run recorded `kind=keyboard`, so the
  mouse-move path is still only auto-verified. Re-run and move the mouse to close it.
- Everything in §24–§31.
Per §32 I still do not convert a returned `true` into "user control verified".

---

## 7. Fix status (applied — see `P0_1_FIX_REPORT.md`)

These were proposed here and have since been **applied** in the fix pass, ordered by
causality, not by convenience. Two extra fixes (F10) and four extra defects (D-8 … D-11)
were found while implementing; those are documented in the fix report.

| # | Fix | File | Nature | Applied |
|---|---|---|---|---|
| F1 | `release_control()` must not leave `HUMAN_OVERRIDE` or `EMERGENCY_STOP` — terminal until explicit resume | `src/safety/controller.py` | 3-line guard; turns gate 7/12 green | yes |
| F2 | `request_control()` must refuse while the live state is `HUMAN_OVERRIDE` (currently only `EMERGENCY_STOP`) | `src/safety/controller.py` | 1-line guard | yes |
| F3 | The external gate file may only CLOSE the gate — AND it with `allow_input`, never OR | `src/adapters/computer_use.py` | 4-line change | yes |
| F4 | Human Override must be a real stop: cancel current action, clear the queue, and require an explicit user Resume (banner currently only has STOP) | `src/safety/banner.py`, `src/safety/banner_win32.py` | UI + wiring | yes |
| F5 | Attribute injection from the event's own injected flag (or a per-action injection lease), not from cursor deltas | `src/safety/human_override.py` | replaces the heuristic | yes |
| F6 | Install the low-level observer *and* pump it on one dedicated thread, set `restype`, always chain, and always unhook in cleanup | `src/safety/human_override.py` | corrected (not deleted — flag attribution needs it) | yes |
| F7 | Track the keys/buttons UHA actually pressed and release only those. Never inject UP for input UHA did not create | `src/desktop/hotkey.py` | small set instead of the blanket | yes |
| F8 | Fix the forced release (`args` not `arguments`), or drop the REST fallback and use the MCP session | `src/safety/cleanup.py`, `src/safety/watchdog.py` | 2-line change | yes |
| F9 | Stop the second writer of the safety state file (`tools/p0_safety_daemon.py` re-asserts `EMERGENCY_STOP` every second) | `tools/p0_safety_daemon.py` | delete loop | yes |
| F10 | §32: state plainly in `release_control()` / `force_cleanup_input()` that they prove nothing about physical user control (`user_control_verified: False`) | `src/adapters/computer_use.py`, `src/safety/cleanup.py` | documentation-as-contract | yes |

F6 note: this report proposed *deleting* the hooks. The fix instead **corrected** them,
because F5's injected-flag attribution needs the low-level event stream — polling or
`GetAsyncKeyState` cannot tell a physical event from an injected one, which is exactly
§5's requirement. The hooks remain observe-only and always chain.

After F1–F3, `tests/test_p01_override_survival.py` reached **16/16** (was 1/5), and the full
suite **571/571**. The physical half (§22, §24–§31) remains **PENDING HUMAN VALIDATION**
except §23, which is HUMAN VERIFIED.

---

## 8. Practical note (post-fix)

F1/F2 have landed, so the auto-resume loop described in §0 no longer exists: an override
survives `release_control()` and survives executor retries.

Two caveats that remain true:

1. **`Ctrl+Alt+F12` is still a polling hotkey, not a true OS hotkey**
   (`hotkey_mode: "polling"`, ~50 ms). It works, but the *primary* mechanism is now
   "move the mouse / press a key" (§9), which needs no hotkey at all.
2. **While the agent is mid-`move`, your physical motion is still overwritten** (RC-2):
   nut-js drives ~3600 positions/second via `SetCursorPos`, which bypasses the hook chain.
   What changed is that you no longer have to *win the race* — the next physical event you
   produce latches `HUMAN_OVERRIDE` permanently. Removing the mid-move overwrite requires
   changing the Computer Use backend, not the safety layer.

Housekeeping observed during the audit: the Agent-TARS server may still be listening on
`127.0.0.1:8788`. If you want a clean slate, stop it; nothing in the safety layer depends on
it being up.

---

## Files

- `docs/P0_1_ROOT_CAUSE.md` — this report
- `tools/p01_input_forensics.py` — §33 diagnostics instrument
- `tests/test_p01_override_survival.py` — executable proof (**16/16** after the fix pass; was 1/5)
- `logs/runs/p01_forensics.json` — live machine evidence for §0/§3
- `logs/runs/p0_cu_safety_20260921-195348.json` — the 12-move run cited in RC-4
