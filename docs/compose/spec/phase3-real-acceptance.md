> Publication copy: personal user/evidence paths are redacted. Historical test results are unchanged; raw evidence remains private.

---
feature: phase3-real-acceptance
status: delivered
updated: 2026-09-20
branch: phase3
commits: 5284845..e498500+control-fix
---

# Phase 3 Real UE Acceptance

## Report

**What was built** — Phase 3 restored real UnrealMCPython TCP 12029 on this machine, hardened `uha doctor` with layered fail-closed probes, fixed control-state durability (`begin_task`/`end_task` no longer wipe PAUSE/STOP/ESTOP), switched real-demo runtime to system Python 3.12 so Overlay/tkinter works, and executed Demo A/B/C + hook-proven EStop + quality collection against live UE 5.8. Final acceptance matrix lives in `docs/PHASE3_REPORT.md`.

**Verification** — Commands and observed results:
- `python tools/phase3_doctor.py` → tcp_listening=true, mcp_request_ok=true, can_read_ue=true
- `python tools/phase3_demo_a.py --actor Platform_Skirt_Front --delta 5` → PASS (650→655→650 idempotent)
- `python tools/phase3_demo_b.py` → PASS (Roof=STRUCTURE, line_trace hit support at Z=420)
- `python tools/phase3_demo_c.py --action focus_viewport` → PASS (calibrate/lock/click/release)
- `python tools/phase3_control.py` → PASS (hooks released CU/desktop/keys; ESTOP survives end_task)
- `python uha.py quality` → real samples in actor_read/actor_mutation/level_save/visual_inspection
- `test_offline 245/245`, `test_router 24/24`, `test_phase2 85/85` (venv), `test_phase3 11/11`; total **365/365**

**Journey log** —
1. 12029 was down while Editor was alive: plugin mounted but DLL never loaded in that session; restart restored TCP without rebuild.
2. `begin_task` and `end_task` clearing control commands enabled false-green pause/estop; both now preserve user control; only `acknowledge_control()` clears.
3. KEYBOARD save reports success without disk mtime change — real fallback to UNREAL_MCP is what saved the demo; quality ledger captured regret=2 honestly.
4. Parallel MCP reads show ~1.0x speedup: bottleneck is UE/MCP serialization, not the read lock.
5. EStop PASS requires hook-path evidence (CU holder→null via hooks), not harness cleanup.

## [S1] Problem

Phase 2 delivered code and 354 offline tests, but UHA has not completed a real closed loop on this machine:

1. UnrealMCPython TCP 12029 is not listening in the current Editor session (plugin mounts, DLL does not load / TCP does not start).
2. Demo A / Demo B never ran against a live UE Actor with readback verification.
3. Demo C calibration ran once, but the full GUI lock → safe action → release chain was not proven.
4. Router Quality Ledger is empty because dry-run does not record outcomes.
5. Overlay did not show because `.venv` Python 3.13 lacks tkinter; system Python 3.12 has Tcl/Tk 8.6.
6. Results are often "code exists / offline tests pass", not "this machine executed successfully".

Phase 3 must answer ten acceptance questions with real evidence, or mark FAIL/PARTIAL — never fake green.

## [S2] Design

### Scope posture

- Do **not** redesign Router architecture; keep single Execution Router.
- Prefer **real repair + real run** over new features.
- Fail closed: if MCP/TCP cannot be proven reachable and a request succeeds with verifiable state, mark unavailable.
- Production levels are not mutated for tests unless the operation is small, reversible, and restored with independent readback.

### Workspace override (user decision)

- Project path: `E:\UnrealHybridAgent`
- User approved: in-place work + attempt Git install; if Git unavailable, document and continue acceptance.
- No nested worktree required for this machine-specific validation session.
- Feature doc path remains `docs/compose/spec/phase3-real-acceptance.md`.
- Final acceptance artifact: `docs/PHASE3_REPORT.md` (user-facing matrix + evidence).

### Runtime override (user decision)

- Primary UHA Python for Phase 3: **system Python 3.12**
  `C:\Users\PUBLIC_USER\AppData\Local\Programs\Python\Python312\python.exe`
  because it has tkinter (Overlay) + httpx + pywin32.
- `.venv` (3.13) remains available for offline regression if needed.
- `.venv-mcp` remains the Unreal MCP stdio server runtime.

### MCP restoration contract

Diagnosis must distinguish all of:

| Signal | How proven |
|---|---|
| UE Editor process alive | process list + window title |
| UnrealMCPython plugin mounted | UE log `Mounting Project plugin UnrealMCPython` |
| Plugin module loaded | UE log `InternalLoadLibrary: 'UnrealMCPython'` + `LogMCPython: TCP server started` |
| TCP 127.0.0.1:12029 listening | `Get-NetTCPConnection -LocalPort 12029` |
| UHA MCP request succeeds | backend op returns non-error payload |
| Editor state actually changed | independent re-read, not just `success=true` |

Allowed repair steps (non-destructive first):

1. Confirm plugin enabled in `.uproject` and plugin binaries exist.
2. Confirm PythonScriptPlugin enabled; Remote Execution may be on.
3. Inspect UE logs for load failures / BuildId issues.
4. Restart Unreal Editor on the same project (user authorized).
5. Rebuild plugin binaries against current engine only if DLL load fails after restart and BuildId mismatch is confirmed.
6. Never delete original assets, never overwrite important Levels, never irreversible batch ops.

`python uha.py doctor` must expose the layered status above. No forced `available=True`.

### Demo A — Structured mutation closed loop

Flow:

```
query scene → pick safe Actor
→ record before (name/label/transform/level/timestamp)
→ Router decision + MCP get transform
→ MCP set location (small delta)
→ independent readback
→ optional save + file mtime/stat evidence
→ restore original transform
→ independent readback again
→ PASS only if both mutations verified by new reads
```

Actor selection rule: do not assume `Hall_Floor`. Query current level first. Prefer a non-critical, easily restorable actor. Record method, first choice, fallback, MCP return, verification.

### Demo B — Semantic support + precise place

Cover multiple semantic classes present in the real scene when possible:

- GROUND
- STRUCTURE (must NOT auto-slam roofs to ground)
- ATTACHED / decoration without unconditional name pass
- HANGING (must not be treated as floating error)
- UNKNOWN / low confidence → Vision/AI path

Precise place uses absolute coordinates + line_trace support surface. Retry must remain idempotent (no double-offset). Restore/verify if a mutation is performed.

If scene lacks samples, create isolated test actors only in a non-production way, or use a disposable path that does not pollute the official Level; restore afterwards.

### Demo C — GUI control chain

```
layout validation (cache hit or recalibrate)
→ DesktopLock acquire
→ focus Unreal Editor
→ Computer Use safe action (Outliner search / viewport focus / select actor)
→ verify
→ release_control
→ release DesktopLock
```

No irreversible GUI edits. Record window/DPI/ROI/coords/lock timings.

### Control plane real tests

- Pause during a task: no new mutation, no new CU input, plan retained, Resume continues.
- Stop: no further steps/fallbacks, locks released.
- Emergency Stop: release mouse buttons + modifiers + CU control + DesktopLock + UE write lock; phase=ABORTED; no LLM required.

### Router quality collection

Run real tasks across at least:

`actor_read`, `actor_mutation`, `level_save`, `visual_inspection`, `gui_interaction`, and `batch_mutation` when feasible.

Record first choice, actual method, success/fail, fallback, attempts, latency, category, regret.

`uha quality` must show `sample_size=N`. Small N is preliminary only — no high-success-rate claims.

### Fallback validation

Induce at least one safe preferred-method unavailability (without fake success) and verify:

```
first method FAIL → classify → fallback allowed? → alternative executes → verify
```

Confirm `UNEXPECTED` / `NOT_SUPPORTED` do not poison MethodHealth, and `actor_read::HYBRID` failures do not kill `actor_mutation::HYBRID`.

### Concurrency evidence

Real or near-real workload:

- Parallel read-like ops (find/transform/semantic/screenshot/geometry) vs serial estimate.
- Write path max writers = 1.
- GUI mutation lock order: `UE_WRITE_LOCK → DesktopLock`.
- If UE MCP serializes requests, document the bottleneck instead of claiming parallel speedup.

### Overlay

Use system Python 3.12 so tkinter Overlay can display. Overlay must show task/step/method/phase/keyboard-control/next + Pause/Stop/EStop buttons. Keep implementation light.

### Logging

Real demos write `logs/runs/phase3_*.jsonl` with structured events (router, locks, verification, fallback, cleanup). No API keys/tokens/unneeded absolute paths.

### Final acceptance

After repairs:

1. Full Smoke Test (startup → structured → semantic → GUI → control → router → cleanup).
2. Full test suites:
   - `test_offline`
   - `test_router`
   - `test_phase2`
   - new `test_phase3` if added
3. Write `docs/PHASE3_REPORT.md` starting with Acceptance Matrix using only PASS / PARTIAL / FAIL / NOT TESTED.

### Ten acceptance questions

1. Can UHA really read UE?
2. Can UHA really mutate UE?
3. Can it verify its own mutation?
4. Can bad operations recover?
5. Does Semantic Resolver reduce misjudgment?
6. Does MCP failure fallback correctly?
7. Can GUI acquire and release control?
8. Are Pause / Stop / EStop effective on the machine?
9. How reliable is Router first choice?
10. Is continuous operation stable?

## [S3] Out of Scope

- New Planner or second execution decision system.
- Large new feature work unrelated to real closed-loop validation.
- Fake green: mock-only PASS, hardcoded success, disabled verification, dry-run outcomes in quality ledger.
- Destructive UE project operations (asset deletion, mass level overwrite).
- Heavy GUI frameworks for the Overlay.

## Tasks

- [x] T1: Workspace + runtime baseline — acceptance: Git status or documented no-git fallback; system Python 3.12 imports UHA stack; Overlay tkinter available. (covers: S2 workspace/runtime)
- [x] T2: Layered doctor hardening — acceptance: `uha doctor` reports UE process, plugin mount/module/TCP, real MCP request, CU, calibration, overlay, router methods without false green. (covers: S2 MCP restoration contract)
- [x] T3: Restore UnrealMCPython TCP 12029 — acceptance: TCP listening + real UE request success + independent scene readback. Restart/rebuild allowed if non-destructive. (covers: S2 MCP restoration)
- [x] T4: Demo A real closed loop — acceptance: modify → readback → restore → readback PASS on a real Actor; evidence logged. (covers: S2 Demo A)
- [x] T5: Demo B semantic + precise path — acceptance: multi-class semantic decisions + at least one real precise/inspection path with verification; no roof-slam / hanging false positive. (covers: S2 Demo B)
- [x] T6: Demo C GUI chain + Overlay — acceptance: calibration/lock/safe UI action/verify/release evidence; Overlay visible on Python 3.12. (covers: S2 Demo C, Overlay)
- [x] T7: Control plane real tests — acceptance: Pause/Resume/Stop/EStop real-machine evidence with lock release and ABORTED state. (covers: S2 Control plane)
- [x] T8: Router quality + fallback data — acceptance: real outcomes in `.state/router_quality.json`; `uha quality` shows sample_size; one safe fallback scenario proven. (covers: S2 Router quality / fallback)
- [x] T9: Concurrency + idempotency evidence — acceptance: read-parallel/write-serial measured or bottleneck documented; repeated mutation remains absolute-idempotent. (covers: S2 Concurrency)
- [x] T10: Full smoke + full test counts + PHASE3_REPORT.md — acceptance: suites run with exact counts; report matrix uses only PASS/PARTIAL/FAIL/NOT TESTED and lists remaining blockers honestly. (covers: S1, S2 final acceptance)
