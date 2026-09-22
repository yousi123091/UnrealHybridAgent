# UHA — Current Status

**This is the authoritative status page for the current public release.**
If a historical report, a phase document, or an old README section conflicts
with this file, **this file wins** (together with `README.md` and the latest
release notes in [`docs/releases/`](releases/)).

---

## Current Release

**`v0.1.0-rc.2`** — source prerelease.
Release notes: [`docs/releases/v0.1.0-rc.2.md`](releases/v0.1.0-rc.2.md)

Validated environment: **Windows, Python 3.12** (on the validation machine).
Other machines, UE versions, and UE projects require their own validation —
UHA makes no cross-platform or cross-version claim.

---

## Active

These are the parts that are actually enabled in this release.

### Execution core

| Component | What it does |
|---|---|
| **Actor query & structured scene inspection** | Find actors; read location / rotation / scale / label / bounds; diff before-vs-after state |
| **Planner** | Turns a supported request into explicit, auditable steps (linear; no dynamic replanning) |
| **Router** | Rules + cost model choose an execution channel and produce an explicit fallback chain |
| **Executor** | `perform → verify → fall back` loop, with non-idempotent guards (e.g. relative moves) |
| **Execution tracing** | Per-run `jsonl` under `logs/runs/`; routing / execution / verification / recovery events are replayable |

### Execution channels

- **Unreal MCP** — structured channel via an external MCP server.
- **UE Python** — official Remote Execution against a running editor.
- **Computer Use / GUI channel** — mouse / keyboard / screenshot, used only
  where structured channels cannot reach.
- **Fallback** — degrading to the next channel is explicit and logged, never silent.

### Verification

- **Post-action independent verification** — after every mutation UHA re-reads
  real scene state instead of trusting the tool return value.
- **Three-state results** — `passed` / `failed` / `skipped` are strictly
  separated. `skipped` means *insufficient evidence* and is never reported as
  a pass.

### Data operations

- **Batch mutation / rollback** — records pre-change transforms and reads state
  back after restore.

### Safety

- **Safety Banner** — independent always-on-top indicator.
- **Input gate** — every input-affecting call is checked against the gate.
- **Emergency Stop**.
- **Human Override** — latching takeover state so a human can take the desktop back.

---

## Deferred

These are **explicitly out of scope for `v0.1.0-rc.2`**. They are not claimed as
working, even though some of them were explored during development.

| Deferred item | Status |
|---|---|
| **UAH desktop HUD** | Source retained in `uah/`; **not enabled** in this release. A release gate prevents startup even if an old config enables it. |
| **Standalone UAH entry points** (`uah/hosts/desktop/*`, `uah/tools/uah.py`) | **Not enabled.** Do not use historical reports' HUD launch commands to validate this release. |
| **Legacy `ControlOverlay`** | **Not enabled.** Cannot be re-enabled through configuration. |
| **Complex mesh support** | Support evidence derived from AABB only is insufficient; unresolved cases return `UNKNOWN`. Collision- and surface-based support is future work. |
| **Full cancellation of already-dispatched remote operations** | UHA can stop *further* dispatch and can release local input ownership. It **cannot** force-stop an in-flight remote call. No "zero-latency cancel" and no "remote operation is guaranteed stopped" claim is made. |
| **Cross-machine / cross-UE-version guarantees** | Not claimed. Each machine, UE version, and project needs its own validation. |
| **Production-grade stability** | Not claimed. This is a source prerelease. |

---

## Historical Reports

The repository contains many `PHASE*`, `P0*`, `P0_1*`, `UAH_*`, validation and
root-cause reports, plus `UHA_V0_1_FEASIBILITY_REPORT.md`.

**What they are:** development-process evidence — what was built, measured, and
concluded *at that point in time*, on the original validation machine.

**What they are not:**

- They are **not** proof that this public release copy passes those validations.
  The public snapshot does not include the original git history, run logs, real
  configuration, or raw acceptance evidence.
- They are **not** a statement of current capability. A report saying something
  was implemented does **not** mean it is ACTIVE in `v0.1.0-rc.2`.
- Paths inside them (`<UHA_ROOT>`, `<PYTHON_EXE>`, `<UE_ROOT>`, …) are
  **anonymised placeholders**. Historical measurement values were **not** rewritten.

**Rule:** when a historical report conflicts with `STATUS.md`, `README.md`, or
the latest release notes, **the release documentation wins.**

---

## License & third-party components

- UHA's own code: **MIT** — see [`../LICENSE`](../LICENSE).
- External services (Unreal MCP, Agent-TARS / Computer Use, Unreal Engine) are
  **not bundled** and are pointed to by local configuration.
  See [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) and
  [`DEPENDENCIES.md`](DEPENDENCIES.md).

---

## Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs **offline tests
only** on Windows / Python 3.12:

- `tests/test_offline.py`
- `tests/test_router.py`
- `tests/test_release_scope.py`
- a repository-wide Python syntax / compile check

Tests requiring a real UE editor, physical mouse/keyboard, real human takeover,
a GUI, or external MCP services are **not** part of normal CI. They are manual /
live validation and must not be mocked into a green result.
