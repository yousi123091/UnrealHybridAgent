"""Control-plane real-machine verification for Phase 3."""

from __future__ import annotations

import json
import sys
import time
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


def main() -> int:
    cfg = load_config("config/agent.config.json", reload=True)
    log = RunLogger(cfg.path("logs", ensure_parent=True), run_name="phase3_control", console=False)
    bundle = build_bundle(cfg)
    executor = Executor(bundle, cfg, log)
    backend = bundle.unreal["UNREAL_MCP"]
    actor = "Platform_Skirt_Front"
    ctrl = get_controller()
    evidence = {"started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # Clean state via explicit acknowledge (not illegal auto-reset)
    ctrl.acknowledge_control()
    ctrl.begin_task("phase3_control_probe")
    evidence["initial"] = ctrl.snapshot().as_dict()
    before = list(backend.get_actor_transform(actor).location)
    evidence["before_xyz"] = before

    # --- PAUSE blocks new mutations ---
    ctrl.acknowledge_control()
    ctrl.begin_task("phase3_pause_test")
    ctrl.pause()
    evidence["paused"] = ctrl.snapshot().as_dict()
    evidence["blocks_writes"] = ctrl.blocks_writes()
    try:
        res = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 2.0})
        evidence["mutation_while_paused"] = {"ok": bool(res.ok), "summary": res.summary()[:240]}
    except Exception as exc:
        evidence["mutation_while_paused"] = {"ok": False, "error": str(exc)[:240]}
    mid = list(backend.get_actor_transform(actor).location)
    evidence["xyz_after_pause_attempt"] = mid
    evidence["pause_blocks"] = (not evidence["mutation_while_paused"]["ok"]) and mid == before

    # --- RESUME continues ---
    ctrl.resume()
    evidence["resumed"] = ctrl.snapshot().as_dict()
    res2 = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 2.0})
    evidence["mutation_after_resume"] = {"ok": bool(res2.ok), "summary": res2.summary()[:240]}
    mid2 = list(backend.get_actor_transform(actor).location)
    evidence["xyz_after_resume"] = mid2
    evidence["resume_works"] = bool(res2.ok) and abs(mid2[2] - (before[2] + 2.0)) < 1.0
    # restore
    ctrl.acknowledge_control()
    executor.run(get_skill("actor_move"), {
        "actor": actor, "location": before, "absolute": True, "idempotent": True,
    })

    # --- STOP ---
    ctrl.acknowledge_control()
    ctrl.begin_task("phase3_stop_test")
    ctrl.stop()
    evidence["stopped"] = ctrl.snapshot().as_dict()
    try:
        res3 = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 2.0})
        evidence["mutation_after_stop"] = {"ok": bool(res3.ok), "summary": res3.summary()[:240]}
    except Exception as exc:
        evidence["mutation_after_stop"] = {"ok": False, "error": str(exc)[:240]}
    evidence["stop_blocks"] = not evidence["mutation_after_stop"]["ok"]

    # --- ESTOP (production-like hooks) ---
    ctrl.acknowledge_control()
    ctrl.begin_task("phase3_estop_test")
    hook_log: list[str] = []

    def _estop_hooks() -> None:
        hook_log.append("hooks_start")
        try:
            from src.desktop.hotkey import release_all_keys_and_buttons

            rel = release_all_keys_and_buttons()
            hook_log.append(f"keys={rel}")
        except Exception as exc:  # noqa: BLE001
            hook_log.append(f"keys_err={exc}")
        try:
            if bundle.computer_use is not None:
                r = bundle.computer_use.release_control(force=True)
                hook_log.append(f"cu={r}")
        except Exception as exc:  # noqa: BLE001
            hook_log.append(f"cu_err={exc}")
        try:
            executor.coordinator.force_release_all()
            hook_log.append("coordinator_force_release")
        except Exception as exc:  # noqa: BLE001
            hook_log.append(f"coord_err={exc}")
        hook_log.append("hooks_end")

    ctrl.on_emergency(_estop_hooks)
    released = {}
    try:
        if bundle.computer_use is not None:
            bundle.computer_use.acquire_control(wait=True)
            released["acquired_cu"] = True
            released["cu_holder_before"] = bundle.computer_use.peek_lock()
    except Exception as exc:
        released["acquired_cu"] = False
        released["cu_error"] = str(exc)[:200]
    try:
        cm = executor.coordinator.desktop()
        cm.__enter__()
        released["desktop_held"] = True
    except Exception as exc:
        released["desktop_held"] = False
        released["desktop_error"] = str(exc)[:200]

    ctrl.emergency_stop(reason="phase3_estop_real")
    evidence["after_estop"] = ctrl.snapshot().as_dict()
    evidence["estop_hook_log"] = hook_log
    # Hooks should have already released CU; probe residual state without claiming harness cleanup as success
    if bundle.computer_use is not None:
        try:
            released["cu_peek_after_estop"] = bundle.computer_use.peek_lock()
        except Exception as exc:
            released["cu_peek_error"] = str(exc)[:200]
    released["hooks_ran"] = any(x.startswith("hooks_") for x in hook_log)
    released["hooks_released_cu"] = any("cu=" in x and "released" in x for x in hook_log)
    released["hooks_force_released_coordinator"] = any("coordinator_force_release" in x for x in hook_log)
    # Only release desktop context if hooks did not; record which path did it
    if released.get("desktop_held"):
        holder_after = None
        try:
            holder_after = (executor.coordinator.stats() or {}).get("current_holders", {}).get("desktop")
        except Exception:
            holder_after = None
        released["desktop_holder_after_estop"] = holder_after
        if holder_after not in (None, "", "None"):
            try:
                cm.__exit__(None, None, None)
                released["desktop_released_by_harness_fallback"] = True
            except Exception as exc:
                released["desktop_released_by_harness_fallback"] = False
                released["desktop_exit_error"] = str(exc)[:200]
        else:
            released["desktop_released_by_hooks"] = True
    try:
        released["lock_stats"] = executor.coordinator.stats()
    except Exception:
        pass
    evidence["release_probe"] = released
    # end_task must NOT wipe ESTOP
    try:
        ctrl.end_task(ok=False)
        evidence["estop_after_end_task"] = ctrl.snapshot().as_dict()
        evidence["estop_survives_end_task"] = (
            ctrl.snapshot().phase.value == "ABORTED"
            and ctrl.should_abort()
            and ctrl.blocks_writes()
        )
    except Exception as exc:
        evidence["estop_after_end_task_error"] = str(exc)[:200]
        evidence["estop_survives_end_task"] = False

    try:
        res4 = executor.run(get_skill("actor_move"), {"actor": actor, "axis": "z", "delta": 2.0})
        evidence["mutation_after_estop"] = {"ok": bool(res4.ok), "summary": res4.summary()[:240]}
    except Exception as exc:
        evidence["mutation_after_estop"] = {"ok": False, "error": str(exc)[:240]}
    final = list(backend.get_actor_transform(actor).location)
    evidence["final_xyz"] = final
    evidence["estop_blocks"] = not evidence["mutation_after_estop"]["ok"] and abs(final[2] - before[2]) < 1.0
    evidence["phase_aborted"] = evidence["after_estop"]["phase"] == "ABORTED"
    evidence["estop_hooks_proven"] = bool(
        released.get("hooks_ran") and released.get("hooks_released_cu") and released.get("hooks_force_released_coordinator")
    )

    # final restore safety
    ctrl.acknowledge_control()
    ctrl.begin_task("phase3_cleanup")
    executor.run(get_skill("actor_move"), {
        "actor": actor, "location": before, "absolute": True, "idempotent": True,
    })
    final2 = list(backend.get_actor_transform(actor).location)
    evidence["cleanup_xyz"] = final2

    if (
        evidence["pause_blocks"]
        and evidence["resume_works"]
        and evidence["stop_blocks"]
        and evidence["estop_blocks"]
        and evidence["phase_aborted"]
        and evidence.get("estop_survives_end_task")
        and evidence.get("estop_hooks_proven")
    ):
        evidence["result"] = "PASS"
    elif evidence["estop_blocks"] and evidence["phase_aborted"]:
        evidence["result"] = "PARTIAL"
    else:
        evidence["result"] = "FAIL"
    evidence["judgement"] = {
        "pause_blocks": evidence["pause_blocks"],
        "resume_works": evidence["resume_works"],
        "stop_blocks": evidence["stop_blocks"],
        "estop_blocks": evidence["estop_blocks"],
        "phase_aborted": evidence["phase_aborted"],
        "estop_survives_end_task": evidence.get("estop_survives_end_task"),
        "estop_hooks_proven": evidence.get("estop_hooks_proven"),
    }

    out = cfg.path("logs") / "runs" / f"phase3_control_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    print(f"\nRESULT={evidence['result']}")
    print(f"Evidence: {out}")
    bundle.close()
    return 0 if evidence["result"] in ("PASS", "PARTIAL") else 2


if __name__ == "__main__":
    raise SystemExit(main())
