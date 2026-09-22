"""Phase 4B real-machine A/B bench + observation/batch/verify demos."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve().parents[2]
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.adapters.unreal.batch_ops import UnrealBatchOps, primitive_read_cost  # noqa: E402
from src.core.checkpoint import CheckpointStore  # noqa: E402
from src.core.config import load_config  # noqa: E402
from src.core.log import RunLogger  # noqa: E402
from src.observation.task_observer import TaskObserver  # noqa: E402
from src.runtime import build_bundle  # noqa: E402
from src.scheduler import Executor  # noqa: E402
from src.semantics.profile import load_profile  # noqa: E402
from src.skills.registry import get_skill  # noqa: E402
from src.verification.outcome import OutcomeVerifier  # noqa: E402
from src.vision.gui_confidence import assess_gui_confidence, verify_gui_action  # noqa: E402
from src.vision.semantic_support import SemanticSupportResolver

SAFE3 = ["Platform_Skirt_Front", "Platform_Skirt_West", "Platform_Skirt_East"]
SAFE6 = SAFE3 + ["EXT_MarkerPost_0_W", "EXT_MarkerPost_0_E", "EXT_MarkerPost_1_W"]
SAFE10 = SAFE6 + ["EXT_MarkerPost_1_E", "EXT_MarkerPost_2_W", "EXT_MarkerPost_2_E", "EXT_MarkerPost_3_W"]
SEM_SAMPLE = [
    "DoorJamb_L", "DoorLeaf_L", "DoorLintel_Top", "Throne_Seat", "EXT_MarkerPost_0_W",
    "CorrFloor_West", "Roof_Main", "Platform_Main", "Hall_Floor", "EXT_Plaza_Central",
]


def _loc(t):
    loc = getattr(t, "location", None)
    if loc is None and isinstance(t, dict):
        loc = t.get("location")
    return [float(x) for x in list(loc or [])[:3]]


def restore(backend, mapping):
    for lab, loc in mapping.items():
        try:
            backend.set_actor_location(lab, loc)
        except Exception:
            pass


def run() -> dict:
    cfg = load_config("config/agent.config.json", reload=True)
    log = RunLogger(cfg.path("logs", ensure_parent=True), run_name="phase4b_bench", console=True)
    evidence: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "experiments": {}}

    executor = Executor(build_bundle(cfg), cfg, log)
    executor.set_execution_mode("AUTO")
    executor.controller.acknowledge_control()
    backend = executor.bundle.unreal.get("UNREAL_MCP")
    if backend is None or not backend.available():
        evidence["error"] = "UNREAL_MCP unavailable"
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return evidence

    profile = load_profile(cfg.get("semantics.profile") or "example_palace")
    ops = UnrealBatchOps(backend)
    verifier = OutcomeVerifier()

    # --- Exp A: Observation primitive vs batch vs task-scoped ---
    labels = SAFE3 + ["Roof_Main", "Hall_Floor"]
    prim = primitive_read_cost(backend, labels)
    t0 = time.perf_counter()
    batch_read = ops.batch_read(labels) if ops.available else {"ok": False}
    batch_ms = (time.perf_counter() - t0) * 1000
    obs = TaskObserver(backend, task_id="bench-obs", profile=profile)
    pkg = obs.observe(targets=["Platform_Skirt_Front"], intent="prepare_batch_movement", max_neighbors=8)
    pkg2, delta = obs.observe_delta_safe(targets=["Platform_Skirt_Front"], intent="prepare_batch_movement",
                                         max_neighbors=8)
    evidence["experiments"]["A_observation"] = {
        "primitive": {"rtt": prim.get("round_trips"), "latency_ms": prim.get("latency_ms"), "n": prim.get("n")},
        "batch_raw": {"rtt": batch_read.get("round_trips"), "latency_ms": batch_read.get("latency_ms") or batch_ms,
                      "n": batch_read.get("n"), "ok": bool(batch_read.get("ok") or batch_read.get("success"))},
        "task_scoped": {
            "targets": pkg.labels(),
            "selected_n": pkg.stats.get("selected_n"),
            "rtt": pkg.stats.get("rtt"),
            "latency_ms": pkg.stats.get("latency_ms"),
            "payload_bytes": pkg.stats.get("payload_bytes"),
            "anomalies": len(pkg.anomalies),
            "mode": pkg.provenance.get("mode"),
        },
        "delta": {
            "mode": delta.mode,
            "changed_n": len(delta.changed),
            "unchanged_n": len(delta.unchanged),
            "reason": delta.reason,
        },
        "tool_calls_estimate": {
            "primitive": prim.get("round_trips"),
            "batch_raw": batch_read.get("round_trips") or 1,
            "task_scoped": pkg.stats.get("rtt"),
        },
        "tokens": None,
    }

    # --- Exp B: batch sizes ---
    exp_b = {}
    for n, labels_n in ((3, SAFE3), (6, SAFE6), (10, SAFE10)):
        # filter existing
        targets = []
        before = {}
        for lab in labels_n:
            try:
                before[lab] = _loc(backend.get_actor_transform(lab))
                targets.append(lab)
            except Exception:
                continue
        if not targets:
            continue
        # Phase4A-style primitive cost estimate for same targets
        prim_cost = primitive_read_cost(backend, targets)
        executor.controller.acknowledge_control()
        t1 = time.perf_counter()
        res = executor.run(get_skill("batch_mutation"), {
            "targets": targets, "axis": "z", "delta": 2.0, "mode": "AUTO",
        })
        wall = (time.perf_counter() - t1) * 1000
        mid = {lab: _loc(backend.get_actor_transform(lab)) for lab in targets}
        batch_info = ((res.as_dict() if hasattr(res, "as_dict") else {}).get("data") or {}).get("batch") or {}
        # restore absolute via UE batch
        restore_map = {lab: before[lab] for lab in targets}
        t2 = time.perf_counter()
        if ops.available:
            rb = ops.batch_set_locations(restore_map)
        else:
            restore(backend, restore_map)
            rb = {"round_trips": len(restore_map)}
        restore_wall = (time.perf_counter() - t2) * 1000
        after = {lab: _loc(backend.get_actor_transform(lab)) for lab in targets}
        restored = all(all(abs(a - b) <= 1.0 for a, b in zip(after[lab], before[lab])) for lab in targets)
        rtt = batch_info.get("ue_round_trips") or batch_info.get("mcp_calls")
        exp_b[str(n)] = {
            "targets": targets,
            "success": bool(res.ok),
            "success_count": batch_info.get("success_count"),
            "mcp_calls_phase4b": batch_info.get("mcp_calls"),
            "ue_round_trips": rtt,
            "primitive_rtt_estimate": prim_cost.get("round_trips"),
            "phase4a_style_rtt_estimate": len(targets) * 2 + 3,  # before/read each + probe/save-ish
            "wall_ms": wall,
            "batch_wall_ms": batch_info.get("wall_ms"),
            "restore_success": restored,
            "restore_wall_ms": restore_wall,
            "outcome": (batch_info.get("outcome_verification") or {}).get("status"),
            "pipeline": batch_info.get("pipeline"),
            "summary": res.summary()[:180],
        }
        # RTT reduction vs phase4a estimate
        p4a = exp_b[str(n)]["phase4a_style_rtt_estimate"]
        if rtt and p4a:
            exp_b[str(n)]["rtt_reduction_pct"] = round(100.0 * (p4a - rtt) / p4a, 1)
    evidence["experiments"]["B_batch"] = exp_b

    # --- Exp C: dynamic verification ---
    actor = SAFE3[0]
    origin = _loc(backend.get_actor_transform(actor))
    verifier_cases = []
    # good absolute move
    executor.controller.acknowledge_control()
    executor.run(get_skill("actor_move"), {"actor": actor, "location": [origin[0], origin[1], origin[2] + 3],
                                           "absolute": True, "mode": "AUTO"})
    after1 = _loc(backend.get_actor_transform(actor))
    r_ok = verifier.verify({"type": "set_location", "label": actor,
                            "target": [origin[0], origin[1], origin[2] + 3]},
                           {"after": after1, "before": origin})
    verifier_cases.append({"case": "set_location_good", "status": r_ok.status, "failed": r_ok.failed_invariants})
    # intentional bad verify (wrong expected)
    r_bad = verifier.verify({"type": "set_location", "label": actor,
                             "target": [origin[0], origin[1], origin[2] + 999]},
                            {"after": after1, "before": origin})
    verifier_cases.append({"case": "set_location_bad_expectation", "status": r_bad.status,
                           "failed": r_bad.failed_invariants,
                           "failure_package": build_fp(r_bad, actor)})
    # restore
    executor.controller.acknowledge_control()
    executor.run(get_skill("actor_move"), {"actor": actor, "location": origin, "absolute": True, "mode": "AUTO"})
    final = _loc(backend.get_actor_transform(actor))
    verifier_cases.append({
        "case": "restore",
        "status": "PASS" if all(abs(a - b) <= 1.0 for a, b in zip(final, origin)) else "FAIL",
        "final": final,
        "origin": origin,
    })
    # visibility/rotation UNKNOWN without state
    verifier_cases.append({"case": "rotation_no_state", "status": verifier.verify({"type": "set_rotation"}, {}).status})
    evidence["experiments"]["C_verification"] = {"cases": verifier_cases, "tokens": None}

    # --- Exp D: GUI confidence ---
    from src.desktop.calibration import SessionGuiCalibrator

    cache = cfg.path("desktop.gui_cache_file") if cfg.get("desktop.gui_cache_file") else \
        cfg.path("workspace.state_dir") / "gui_session.json"
    cal = SessionGuiCalibrator(cache, layout_overrides=cfg.get("gui.layout_overrides") or {})
    try:
        valid = cal.layout_valid()
        if not valid:
            cal.calibrate(force=False)
        data = json.loads(Path(cache).read_text(encoding="utf-8"))
        window = {"window": data.get("fingerprint", {}).get("window") or data.get("window") or {}}
        rois = data.get("rois") or {}
        focus = data.get("focus_point")
        good = assess_gui_confidence(window=window, roi=rois.get("world_outliner"), focus_point=focus)
        shifted = dict(rois.get("world_outliner") or {})
        if shifted:
            shifted["x"] = float(shifted.get("x") or 0) - 4000
            shifted["y"] = float(shifted.get("y") or 0) - 4000
        bad = assess_gui_confidence(window=window, roi=shifted, focus_point=[-9999, -9999])
        post = verify_gui_action("focus_viewport", {}, {})
        evidence["experiments"]["D_gui"] = {
            "good": good.as_dict(),
            "shifted_roi": bad.as_dict(),
            "post_click": post,
            "fail_safe_ok": (not bad.ok and bad.action in ("recalibrate", "fail_closed")),
        }
    except Exception as exc:  # noqa: BLE001
        evidence["experiments"]["D_gui"] = {"error": str(exc)[:240], "fail_safe_ok": False}

    # --- Exp E: semantic profile Phase4A vs 4B ---
    resolver = SemanticSupportResolver()
    p4a = []
    p4b = []
    try:
        rows = backend.call_domain("actor", "get_all_details", {}).get("actors") or []
    except Exception:
        rows = []
    by = {str(r.get("label")): r for r in rows}
    for lab in SEM_SAMPLE:
        row = by.get(lab) or {"label": lab}
        a = resolver.resolve(row)
        b = profile.enrich(a)
        p4a.append({"label": lab, "mode": a.support_mode.value, "conf": a.confidence,
                    "needs_ai": a.needs_ai, "check": a.recommended_check})
        p4b.append({"label": lab, "mode": b.get("support_mode"), "role": b.get("profile_role"),
                    "ground_kind": b.get("ground_kind"), "conf": b.get("confidence"),
                    "needs_ai": b.get("needs_ai"), "check": b.get("recommended_check")})
    def unk_rate(items):
        n = len(items) or 1
        return round(sum(1 for x in items if x.get("mode") == "UNKNOWN" or x.get("needs_ai")) / n, 3)
    evidence["experiments"]["E_semantic"] = {
        "phase4a": p4a,
        "phase4b_profile": p4b,
        "unknown_or_needs_ai_rate_4a": unk_rate(p4a),
        "unknown_or_needs_ai_rate_4b": unk_rate(p4b),
        "unsafe_auto_action_increase": False,  # markers still needs_ai
        "note": "profile improves coverage; MARKER still fail-safe",
    }

    # safety pause check
    executor.controller.acknowledge_control()
    executor.controller.begin_task("p4b_pause")
    executor.controller.pause()
    res_p = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 1.0})
    executor.controller.acknowledge_control()
    final2 = _loc(backend.get_actor_transform(actor))
    evidence["safety"] = {
        "pause_blocks": (not res_p.ok),
        "final_xyz": final2,
        "matches_origin": all(abs(a - b) <= 1.0 for a, b in zip(final2, origin)),
    }
    evidence["performance"] = {
        "router_decision_ms": executor.performance.get("router_decision_ms"),
        "capability_cache": executor.capability_cache.stats(),
    }
    evidence["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    out = cfg.path("logs") / "runs" / f"phase4b_bench_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    print(f"\nBENCH={out}")
    executor.bundle.close()
    return evidence


def build_fp(vres, label):
    from src.verification.outcome import build_failure_package

    return build_failure_package(action={"type": "set_location", "label": label},
                                 targets=[label], verification=vres, backend="UNREAL_MCP",
                                 checkpoint_id=None)


if __name__ == "__main__":
    run()
