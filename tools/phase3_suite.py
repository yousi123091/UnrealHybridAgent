"""Phase 3 suite: real router quality, fallback, control, save, concurrency evidence."""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_THIS = Path(__file__).resolve().parents[1]
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.core.config import load_config  # noqa: E402
from src.core.log import RunLogger  # noqa: E402
from src.core.session_control import get_controller  # noqa: E402
from src.runtime import build_bundle  # noqa: E402
from src.scheduler import Executor  # noqa: E402
from src.skills.registry import get_skill  # noqa: E402


def _xyz(obj):
    loc = getattr(obj, "location", None)
    if loc is None and isinstance(obj, dict):
        loc = obj.get("location")
    if loc is None:
        return None
    return [float(loc[0]), float(loc[1]), float(loc[2])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--actor", default="Platform_Skirt_Front")
    ap.add_argument("--ground-ref", default="EXT_Plaza_Central")
    args = ap.parse_args()

    cfg = load_config(args.config, reload=True)
    log = RunLogger(cfg.path("logs", ensure_parent=True), run_name="phase3_suite", console=True)
    bundle = build_bundle(cfg)
    executor = Executor(bundle, cfg, log)
    backend = bundle.unreal["UNREAL_MCP"]
    actor = args.actor
    suite: dict = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "result": "FAIL",
        "tasks": {},
    }

    def run_skill(name: str, params: dict) -> dict:
        t0 = time.perf_counter()
        skill = get_skill(name)
        res = executor.run(skill, params)
        return {
            "skill": name,
            "params": params,
            "ok": bool(res.ok),
            "method": getattr(res, "method", None),
            "summary": res.summary()[:300],
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
            "verification": getattr(res, "verification", None).as_dict()
            if getattr(res, "verification", None) and hasattr(res.verification, "as_dict")
            else str(getattr(res, "verification", None))[:200],
        }

    # --- structured read ---
    suite["tasks"]["actor_find"] = run_skill("actor_find", {"actor": actor})
    suite["tasks"]["actor_inspect"] = run_skill("actor_inspect", {"actor": actor})

    # --- mutation ---
    before = backend.get_actor_transform(actor)
    suite["before_xyz"] = _xyz(before)
    suite["tasks"]["actor_mutation"] = run_skill("actor_move", {"actor": actor, "axis": "z", "delta": 3.0})
    mid = backend.get_actor_transform(actor)
    suite["tasks"]["actor_mutation"]["after_xyz"] = _xyz(mid)
    suite["tasks"]["restore"] = run_skill("actor_move", {
        "actor": actor,
        "location": suite["before_xyz"],
        "absolute": True,
        "idempotent": True,
    })
    after = backend.get_actor_transform(actor)
    suite["tasks"]["restore"]["after_xyz"] = _xyz(after)

    # --- level_save ---
    suite["tasks"]["level_save"] = run_skill("level_save", {"actor": actor})

    # --- visual inspection ---
    suite["tasks"]["visual_inspection"] = run_skill("visual_inspect", {
        "ground_ref": args.ground_ref,
        "region": [-10000, 10000, -8000, 8000],
    })

    # --- gui_interaction via skill if available ---
    try:
        suite["tasks"]["gui_interaction"] = run_skill("level_save", {"method": "KEYBOARD", "actor": actor, "prefer_gui": True})
    except Exception as exc:
        suite["tasks"]["gui_interaction"] = {"ok": False, "error": str(exc)[:200]}

    # --- parallel reads ---
    def _read_job(label: str):
        t0 = time.perf_counter()
        be = bundle.unreal["UNREAL_MCP"]
        t = be.get_actor_transform(label)
        return label, _xyz(t), round((time.perf_counter() - t0) * 1000, 1)

    labels = [actor, "Hall_Floor", "Roof_Main", args.ground_ref, "Porch_Beam"]
    t_serial_est = 0.0
    serial_results = []
    t0 = time.perf_counter()
    for lab in labels:
        r = _read_job(lab)
        serial_results.append(r)
        t_serial_est += r[2]
    serial_wall = round((time.perf_counter() - t0) * 1000, 1)
    t1 = time.perf_counter()
    parallel_results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(_read_job, lab) for lab in labels]
        for f in as_completed(futs):
            parallel_results.append(f.result())
    parallel_wall = round((time.perf_counter() - t1) * 1000, 1)
    suite["concurrency"] = {
        "serial_est_ms": round(t_serial_est, 1),
        "serial_wall_ms": serial_wall,
        "parallel_wall_ms": parallel_wall,
        "speedup_if_any": round(serial_wall / parallel_wall, 2) if parallel_wall else None,
        "note": "MCP server may serialize TCP requests; if speedup≈1 bottleneck is UE/MCP not locks",
        "labels": labels,
    }

    # --- fallback scenario: temporarily mark MCP cooldown via health failure simulation ---
    # Safe induced failure: call a non-existent actor; executor should classify failure.
    suite["tasks"]["induced_failure"] = run_skill("actor_find", {"actor": "UHA_PHASE3_DOES_NOT_EXIST_XYZ"})
    try:
        from src.router.fallback import MethodHealth
        health = MethodHealth(cfg.path("router.stats_file"))
        suite["health_after_failure"] = {
            k: v for k, v in health.stats().items() if "UNREAL_MCP" in k or "actor" in k
        }
    except Exception as exc:
        suite["health_after_failure_error"] = str(exc)[:200]

    # Fallback proof: force a method preference that may not work — if controller allows
    # We run a skill with explicit method override if supported
    try:
        res = executor.run(get_skill("actor_find"), {"actor": actor, "force_method": "UE_COMMANDLET"})
        suite["tasks"]["fallback_pref_unavailable_method"] = {
            "ok": bool(res.ok),
            "method": getattr(res, "method", None),
            "summary": res.summary()[:300],
            "attempts": [getattr(a, "__dict__", str(a))[:200] for a in (getattr(res, "attempts", []) or [])],
        }
        if hasattr(res, "as_dict"):
            suite["tasks"]["fallback_pref_unavailable_method"]["as_dict"] = {
                k: res.as_dict().get(k) for k in ("skill", "ok", "method", "attempts", "error")
            }
    except Exception as exc:
        suite["tasks"]["fallback_pref_unavailable_method"] = {"error": str(exc)[:300]}

    # --- control plane ---
    controller = get_controller()
    # Reset any prior state if possible
    try:
        if hasattr(controller, "resume"):
            controller.resume()
        if hasattr(controller, "reset"):
            controller.reset()
    except Exception:
        pass
    control = {}
    control["initial"] = controller.snapshot().as_dict() if hasattr(controller.snapshot(), "as_dict") else str(controller.snapshot())
    controller.pause()
    control["after_pause"] = controller.snapshot().as_dict()
    # mutation while paused should be blocked by ensure_writable
    try:
        pre = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 1.0})
        control["mutation_while_paused"] = {"ok": bool(pre.ok), "summary": pre.summary()[:200]}
    except Exception as exc:
        control["mutation_while_paused"] = {"ok": False, "error": str(exc)[:200]}
    controller.resume()
    control["after_resume"] = controller.snapshot().as_dict()
    controller.stop()
    control["after_stop"] = controller.snapshot().as_dict()
    try:
        if hasattr(controller, "reset"):
            controller.reset()
    except Exception:
        pass
    controller.emergency_stop(reason="phase3_suite_test")
    control["after_estop"] = controller.snapshot().as_dict()
    try:
        post = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 1.0})
        control["mutation_after_estop"] = {"ok": bool(post.ok), "summary": post.summary()[:200]}
    except Exception as exc:
        control["mutation_after_estop"] = {"ok": False, "error": str(exc)[:200]}
    try:
        if hasattr(controller, "reset"):
            controller.reset()
    except Exception:
        pass
    suite["control"] = control

    # --- quality / locks ---
    try:
        suite["router_quality"] = executor.quality.summary()
        suite["lock_stats"] = executor.coordinator.stats()
        suite["performance"] = getattr(executor, "last_performance", None) or getattr(executor, "performance", None)
    except Exception as exc:
        suite["quality_error"] = str(exc)[:200]

    # Final actor should be restored
    final = backend.get_actor_transform(actor)
    suite["final_xyz"] = _xyz(final)
    suite["restored"] = _xyz(final) == suite["before_xyz"] or (
        suite["before_xyz"] and _xyz(final) and all(abs(a - b) < 1.0 for a, b in zip(_xyz(final), suite["before_xyz"]))
    )

    # Result grading
    ok_core = (
        suite["tasks"].get("actor_find", {}).get("ok")
        and suite["tasks"].get("actor_mutation", {}).get("ok")
        and suite["tasks"].get("restore", {}).get("ok")
        and suite["restored"]
    )
    pause_blocked = not control.get("mutation_while_paused", {}).get("ok", True)
    estop_blocked = not control.get("mutation_after_estop", {}).get("ok", True)
    if ok_core and pause_blocked and estop_blocked:
        suite["result"] = "PASS"
    elif ok_core:
        suite["result"] = "PARTIAL"
    else:
        suite["result"] = "FAIL"
    suite["judgement"] = {
        "structured_loop": ok_core,
        "pause_blocks_mutation": pause_blocked,
        "estop_blocks_mutation": estop_blocked,
    }
    suite["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    out = cfg.path("logs") / "runs" / f"phase3_suite_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(suite, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(suite, ensure_ascii=False, indent=2, default=str))
    print(f"\nRESULT={suite['result']}")
    print(f"Evidence: {out}")
    bundle.close()
    return 0 if suite["result"] in ("PASS", "PARTIAL") else 2


if __name__ == "__main__":
    raise SystemExit(main())
