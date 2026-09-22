"""Phase 4A real-machine demos A–F."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve().parents[1]
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.core.checkpoint import CheckpointStore, rollback_task  # noqa: E402
from src.core.config import load_config  # noqa: E402
from src.core.execution_mode import ExecutionMode  # noqa: E402
from src.core.log import RunLogger  # noqa: E402
from src.runtime import build_bundle  # noqa: E402
from src.scheduler import Executor  # noqa: E402
from src.skills.registry import get_skill  # noqa: E402
from src.vision.semantic_inspect import inspect_scene_semantic  # noqa: E402

SAFE = ["Platform_Skirt_Front", "Platform_Skirt_West", "Platform_Skirt_East", "EXT_MarkerPost_0_W"]


def _xyz(obj):
    loc = getattr(obj, "location", None)
    if loc is None and isinstance(obj, dict):
        loc = obj.get("location")
    return [float(loc[0]), float(loc[1]), float(loc[2])]


def run_all() -> dict:
    cfg = load_config("config/agent.config.json", reload=True)
    log = RunLogger(cfg.path("logs", ensure_parent=True), run_name="phase4a", console=True)
    evidence: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "demos": {}}

    # cold router
    from src.router.capability_cache import reset_capability_cache
    reset_capability_cache()
    executor = Executor(build_bundle(cfg), cfg, log)
    backend = executor.bundle.unreal.get("UNREAL_MCP")
    if backend is None or not backend.available():
        evidence["error"] = "UNREAL_MCP unavailable"
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return evidence
    # warm once
    t0 = time.perf_counter()
    cold = executor.probe(use_cache=False)
    cold_ms = (time.perf_counter() - t0) * 1000
    latencies_cold = []
    latencies_warm = []
    for i in range(3):
        reset_capability_cache()
        executor.router.capability_cache = executor.capability_cache
        t = time.perf_counter()
        executor.probe(use_cache=False)
        latencies_cold.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        executor.probe(use_cache=True)
        latencies_warm.append((time.perf_counter() - t) * 1000)
    # also measure decide() latency
    from src.router.intent import TaskIntent
    intent = TaskIntent(skill="actor_find", description="find", known_actor="Platform_Skirt_Front",
                        read_only=True, needs_exact_values=False, target_count=1)
    decide_cold = []
    decide_warm = []
    for _ in range(3):
        reset_capability_cache()
        t = time.perf_counter()
        executor.router.decide(intent, backends=executor.bundle.structured_backends(),
                               computer_use_available=True, vision_available=True,
                               task_category="actor_read", use_capability_cache=False)
        decide_cold.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        executor.router.decide(intent, backends=executor.bundle.structured_backends(),
                               computer_use_available=True, vision_available=True,
                               task_category="actor_read", use_capability_cache=True)
        decide_warm.append((time.perf_counter() - t) * 1000)

    # Demo E capability cache
    evidence["demos"]["E_capability_cache"] = {
        "probe_cold_ms": latencies_cold,
        "probe_warm_ms": latencies_warm,
        "decide_cold_ms": decide_cold,
        "decide_warm_ms": decide_warm,
        "cache_stats": executor.capability_cache.stats(),
        "availability": cold,
    }

    # Demo A execution mode
    demo_a = {}
    executor.controller.acknowledge_control()
    executor.set_execution_mode("CONFIRM")
    executor.approval._confirmer = None
    res_conf = executor.run(get_skill("actor_move"), {"actor": SAFE[0], "axis": "z", "delta": 1.0, "mode": "CONFIRM"})
    demo_a["confirm_without_approve"] = {"ok": res_conf.ok, "summary": res_conf.summary()[:240],
                                         "xyz": _xyz(backend.get_actor_transform(SAFE[0]))}
    executor.controller.acknowledge_control()
    executor.approval._confirmer = lambda req: False
    res_deny = executor.run(get_skill("actor_move"), {"actor": SAFE[0], "axis": "z", "delta": 1.0})
    demo_a["confirm_denied"] = {"ok": res_deny.ok, "summary": res_deny.summary()[:240],
                                "xyz": _xyz(backend.get_actor_transform(SAFE[0]))}
    executor.controller.acknowledge_control()
    executor.approval._confirmer = lambda req: True
    res_allow = executor.run(get_skill("actor_move"), {"actor": SAFE[0], "axis": "z", "delta": 1.0})
    after_allow = _xyz(backend.get_actor_transform(SAFE[0]))
    demo_a["confirm_allowed"] = {"ok": res_allow.ok, "summary": res_allow.summary()[:240], "xyz": after_allow}
    # restore absolute if moved
    if after_allow != demo_a["confirm_without_approve"]["xyz"]:
        base = demo_a["confirm_without_approve"]["xyz"]
        executor.controller.acknowledge_control()
        executor.run(get_skill("actor_move"), {"actor": SAFE[0], "location": base, "absolute": True, "mode": "AUTO"})
    executor.controller.acknowledge_control()
    executor.set_execution_mode("AUTO")
    executor.approval._confirmer = None
    res_auto = executor.run(get_skill("actor_move"), {
        "actor": SAFE[0], "location": demo_a["confirm_without_approve"]["xyz"],
        "absolute": True, "mode": "AUTO",
    })
    demo_a["auto_safe"] = {"ok": res_auto.ok, "summary": res_auto.summary()[:200],
                           "xyz": _xyz(backend.get_actor_transform(SAFE[0]))}
    evidence["demos"]["A_execution_mode"] = demo_a

    # Demo B semantic inspect
    res_b = executor.run(get_skill("visual_inspect"), {
        "ground_ref": "EXT_Plaza_Central",
        "limit": 300,
        "semantic_sample": 20,
        "check_floating": True,
    })
    data_b = (res_b.as_dict() if hasattr(res_b, "as_dict") else {})
    ev = (((data_b.get("data") or {}).get("evidence")) or {})
    sem = ev.get("semantic_inspect") or {}
    evidence["demos"]["B_semantic"] = {
        "ok": res_b.ok,
        "summary": res_b.summary()[:300],
        "mode_counts": sem.get("mode_counts"),
        "probes": sem.get("probes"),
        "legacy_geometry_flagged": sem.get("legacy_geometry_flagged"),
        "semantic_actionable": sem.get("semantic_actionable"),
        "semantic_requires_confirmation": sem.get("semantic_requires_confirmation"),
        "floating_summary": {
            "floating_count": (ev.get("floating") or {}).get("floating_count"),
            "flagged_ratio": (ev.get("floating") or {}).get("flagged_ratio"),
            "basis": (ev.get("floating") or {}).get("basis"),
            "semantic": (ev.get("floating") or {}).get("semantic"),
        },
    }

    # Demo C batch mutation
    executor.controller.acknowledge_control()
    executor.set_execution_mode("AUTO")
    targets = []
    before = {}
    for name in SAFE:
        try:
            before[name] = _xyz(backend.get_actor_transform(name))
            targets.append(name)
        except Exception:
            continue
    targets = targets[:3]  # AUTO allow max batch default is 3
    t_batch = time.perf_counter()
    res_c = executor.run(get_skill("batch_mutation"), {
        "targets": targets,
        "axis": "z",
        "delta": 2.0,
        "mode": "AUTO",
        "absolute": False,
    })
    # Also demonstrate CONFIRM+approve on a 4th actor if present
    extra = [n for n in before if n not in targets]
    res_c_confirm = None
    if extra:
        executor.controller.acknowledge_control()
        executor.set_execution_mode("CONFIRM")
        executor.approval._confirmer = lambda req: True
        res_c_confirm = executor.run(get_skill("batch_mutation"), {
            "targets": extra[:1], "axis": "z", "delta": 2.0, "mode": "CONFIRM",
        })
        executor.approval._confirmer = None
        executor.set_execution_mode("AUTO")
        executor.controller.acknowledge_control()
        if res_c_confirm.ok:
            # include in restore set
            targets = targets + extra[:1]
    batch_wall = (time.perf_counter() - t_batch) * 1000
    mid = {n: _xyz(backend.get_actor_transform(n)) for n in targets}
    batch_info = ((res_c.as_dict() if hasattr(res_c, "as_dict") else {}).get("data") or {}).get("batch") or {}
    # restore absolute
    executor.controller.acknowledge_control()
    t_res = time.perf_counter()
    res_c2 = executor.run(get_skill("batch_mutation"), {
        "targets": targets,
        "locations": {n: before[n] for n in targets},
        "absolute": True,
        "mode": "AUTO",
    })
    restore_wall = (time.perf_counter() - t_res) * 1000
    after = {n: _xyz(backend.get_actor_transform(n)) for n in targets}
    restored = all(all(abs(a - b) <= 1.0 for a, b in zip(after[n], before[n])) for n in targets)
    evidence["demos"]["C_batch"] = {
        "targets": targets,
        "before": before,
        "mid": mid,
        "after_restore": after,
        "restored": restored,
        "batch_ok": res_c.ok,
        "restore_ok": res_c2.ok,
        "batch_wall_ms": batch_wall,
        "restore_wall_ms": restore_wall,
        "metrics": {
            "batch_size": batch_info.get("batch_size"),
            "mcp_calls": batch_info.get("mcp_calls"),
            "wall_ms": batch_info.get("wall_ms"),
            "success_count": batch_info.get("success_count"),
            "retry_count": batch_info.get("retry_count"),
            "fallback_count": batch_info.get("fallback_count"),
        },
        "summary": res_c.summary()[:300],
        "summary_restore": res_c2.summary()[:300],
        "confirm_batch_summary": (res_c_confirm.summary()[:200] if res_c_confirm is not None else None),
        "confirm_batch_ok": (res_c_confirm.ok if res_c_confirm is not None else None),
    }

    # Demo D save
    executor.controller.acknowledge_control()
    t_save = time.perf_counter()
    res_d = executor.run(get_skill("level_save"), {"actor": "n/a"})
    save_ms = (time.perf_counter() - t_save) * 1000
    sd = res_d.as_dict() if hasattr(res_d, "as_dict") else {}
    attempts = sd.get("attempts") or []
    methods = [a.get("method") for a in attempts]
    evidence["demos"]["D_save"] = {
        "ok": res_d.ok,
        "method": sd.get("method"),
        "attempts": methods,
        "wall_ms": save_ms,
        "summary": res_d.summary()[:300],
        "first_choice": methods[0] if methods else None,
        "fallback_triggered": len([m for m in methods if m]) > 1,
        "verification": sd.get("verification"),
        "phase3_baseline_ms": [9600, 10100],
    }

    # Demo F checkpoint / rollback
    executor.controller.acknowledge_control()
    actor = targets[0] if targets else SAFE[0]
    origin = _xyz(backend.get_actor_transform(actor))
    res_f_move = executor.run(get_skill("actor_move"), {
        "actor": actor, "axis": "z", "delta": 4.0, "mode": "AUTO",
    })
    moved = _xyz(backend.get_actor_transform(actor))
    store = CheckpointStore(cfg.path("workspace.state_dir") / "checkpoints")
    from src.core.checkpoint import ActorSnapshot, TaskCheckpoint

    task_id = store.new_id()
    cp = TaskCheckpoint(
        task_id=task_id,
        created_at=time.time(),
        level=str(backend.current_level()),
        execution_mode="AUTO",
        plan={"actor": actor, "origin": origin},
        actors=[ActorSnapshot(label=actor, location=origin)],
        methods=["UNREAL_MCP"],
        status="executed",
    )
    store.save(cp)
    executor.controller.acknowledge_control()
    executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 3.0, "mode": "AUTO"})
    dirty = _xyz(backend.get_actor_transform(actor))
    rb = rollback_task(backend, store, task_id)
    final = _xyz(backend.get_actor_transform(actor))
    evidence["demos"]["F_checkpoint"] = {
        "task_id": task_id,
        "origin": origin,
        "after_first_move": moved,
        "dirty": dirty,
        "rollback": {k: rb.get(k) for k in ("status", "ok", "restored", "failed", "results")},
        "final": final,
        "restored": all(abs(a - b) <= 1.0 for a, b in zip(final, origin)),
        "move_ok": res_f_move.ok,
    }

    # safety/control regression quick check
    ctrl = executor.controller
    ctrl.acknowledge_control()
    ctrl.begin_task("p4a_pause")
    ctrl.pause()
    res_p = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 1.0})
    ctrl.acknowledge_control()
    evidence["safety"] = {
        "pause_blocks": (not res_p.ok),
        "pause_summary": res_p.summary()[:200],
        "final_xyz": _xyz(backend.get_actor_transform(actor)),
        "matches_origin": all(abs(a - b) <= 1.0 for a, b in zip(_xyz(backend.get_actor_transform(actor)), origin)),
    }

    evidence["performance"] = executor.performance
    evidence["router_quality"] = executor.quality.summary()
    evidence["capability_cache"] = executor.capability_cache.stats()
    evidence["execution_mode_default"] = executor.config.get("execution.mode")
    evidence["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    out = cfg.path("logs") / "runs" / f"phase4a_evidence_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    print(f"\nEVIDENCE={out}")
    executor.bundle.close()
    return evidence


if __name__ == "__main__":
    run_all()
