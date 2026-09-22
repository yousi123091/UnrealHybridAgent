> Public edition: project names are anonymized; historical measurements are retained.

---
feature: phase4b-scene-understanding
status: designed
updated: 2026-09-21
branch: phase4b
commits: pending
---

# Phase 4B Scene Understanding & Efficient Closed Loop

## Report

## [S1] Problem

Phase 4A made UHA daily-usable for structured UE ops (approval, batch, save, checkpoint). Remaining efficiency gaps:

1. Agents must chain many primitive MCP calls to reconstruct local scene state.
2. Phase 4A batch still uses N× read/write MCP round trips (3 actors ≈ 10 calls).
3. Verification is skill-specific, not derived from action semantics.
4. Semantic rules leave Door/Throne/Marker/CorridorFloor as UNKNOWN; multi-level floors look like “must drop to world ground”.
5. GUI confidence is config-only; no screenshot sanity / fail-safe closed loop.

**Out of scope**: VeriForest-style persistent world model, long-term fingerprint DB, certification forest, dependency graph. Reality remains source of truth; scene model is ephemeral/task-scoped only.

## [S2] Design

### Principles
- Reality is source of truth; re-observe when uncertain.
- Structured first, GUI last.
- Batch computation inside UE > repeated cross-process calls.
- Observation is task-relevant, not maximally large.
- Dynamic verification from action semantics when possible.
- UNKNOWN safer than wrong confident classification.
- Phase 4A safety (Approval/Checkpoint/Pause/EStop/readback) must not regress.

### Task-scoped Observation Package
`observe_scene(targets, intent, scope, profile)` returns high-density facts for the **current task only**:
- targets + basic state (transform/bounds/class/level/visibility)
- relations (parent/attachment/nearby)
- geometry facts (support/gap/overlap) **on demand by intent**
- semantic role
- anomaly candidates
- provenance + generation/timestamp

Relevance filter (deterministic): explicit targets > attachment > spatial proximity > semantic relation > task profile > recently changed. No embeddings.

### Ephemeral TaskSceneModel
- Lives only for one task.
- Caches latest observed states + generation.
- Delta observation: changed/unchanged/new/resolved anomalies.
- Force full refresh on: external suspect, generation mismatch, tool fail, GUI op, large batch, force_refresh.
- Never persisted as world truth.

### UE-side Batch I/O
Single `execute_python` (or equivalent) loops N actors inside UE:
- `batch_read_transforms(labels)` → 1 RTT
- `batch_set_locations(map)` / `batch_translate(labels, delta)` → 1 RTT
- Independent host-side readback still required after mutation (safety).

Target: 6-actor rigid transform MCP/UE round trips **≥40% lower** than Phase 4A (no skip-verify).

### Dynamic Outcome Verification
`OutcomeVerifier.verify(action, expected, observed)` derives invariants from action semantics:
- SetLocation: loc≈target, rotation/scale unchanged
- RigidTranslate: each loc≈old+delta; pairwise relatives unchanged
- SetRotation / SetVisibility similarly

Output:
```json
{"status":"PASS|FAIL|UNKNOWN","expected":{},"observed":{},"failed_invariants":[],"affected_targets":[],"confidence":1.0,"recommended_next_action":""}
```
Deterministic recommendations only (retry read / restore checkpoint / ask confirmation / escalate to LLM). Verifier is not an agent.

### Structured Failure Package
On fail: action, targets, expected, observed, failed invariants, backend, fallback history, retry count, checkpoint availability, safe recovery options.

### Project Semantic Profile
`src/semantics/profiles/default.json` + `example_palace.json`:
- name/class/tag patterns → semantic role + support strategy
- Ground taxonomy: GLOBAL_GROUND / LOCAL_GROUND / ELEVATED_GROUND / UNKNOWN_GROUND
- Profile rules override built-in keywords; no forced high-confidence misclassification

### GUI Confidence (P1)
- Pre-action: screenshot + ROI sanity + confidence
- If low: recalibrate or REQUIRE_CONFIRMATION; never blind click
- Post-action: verify expected UI state change; click success ≠ task success
- Heuristic VisionProvider only (no fake VLM)

### A/B Bench
- Prefer `<UE_AGENT_BENCH_ROOT>` if usable; also `scripts/phase4b_bench/` for UHA-local runs
- Experiments A–E per user brief (observation/batch/verify/GUI/semantic)
- Metrics: success, steps, tool calls, UE RTTs, wall time, MCP calls, wrong mutation, restore success; tokens=null if unavailable

## [S3] Out of Scope
- Persistent world model / VeriForest features
- Full provider gateway / hot swap / marketplace UI
- Full Approval UI / task history dashboard
- Blind UNKNOWN elimination
- Overnight autonomous production claims without unattended stress evidence
- Phase 4C items listed in user brief

## Tasks

- [ ] T1: Workspace branch phase4b + baseline — acceptance: branch exists, Phase4A green still reproducible
- [ ] T2: Observation package + TaskSceneModel + delta — acceptance: observe_scene returns task-filtered package; delta works; force_refresh path; offline tests (covers: S2 Observation/SceneModel)
- [ ] T3: UE-side batch read/write adapters — acceptance: batch_read/batch_translate via execute_python; RTT measured vs Phase4A (covers: S2 Batch)
- [ ] T4: Dynamic OutcomeVerifier + FailurePackage — acceptance: PASS/FAIL/UNKNOWN on real cases incl. intentional bad mutation (covers: S2 Verification)
- [ ] T5: Semantic profiles + multi-level ground — acceptance: Example profile reduces UNKNOWN on Door/Marker/Corridor without unsafe auto-action (covers: S2 Semantic)
- [ ] T6: GUI confidence pre/post — acceptance: good ROI proceeds; shifted ROI recalibrates/fails closed (covers: S2 GUI)
- [ ] T7: Bench A–E + regression 382+ — acceptance: PHASE4B_REPORT.md with matrix + performance + Q1–Q7 (covers: S1,S2)
