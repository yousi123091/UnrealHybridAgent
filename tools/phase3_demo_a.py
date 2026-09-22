"""Phase 3 Demo A — real structured mutation closed loop.

    python tools/phase3_demo_a.py --actor <name> --delta 5
    python tools/phase3_demo_a.py --auto

Never PASS on success=true alone: every mutation is confirmed by a fresh read.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve().parents[1]
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.core.config import load_config  # noqa: E402
from src.core.log import RunLogger  # noqa: E402
from src.runtime import build_bundle  # noqa: E402
from src.scheduler import Executor  # noqa: E402
from src.skills.registry import get_skill  # noqa: E402


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _as_xyz(loc: object) -> tuple[float, float, float] | None:
    # ActorRef / namedtuple style
    if hasattr(loc, "location"):
        loc = getattr(loc, "location")
    if isinstance(loc, dict):
        try:
            return float(loc.get("x", 0)), float(loc.get("y", 0)), float(loc.get("z", 0))
        except Exception:
            return None
    if isinstance(loc, (list, tuple)) and len(loc) >= 3:
        try:
            return float(loc[0]), float(loc[1]), float(loc[2])
        except Exception:
            return None
    return None


def _transform_payload(obj: object) -> dict:
    """Normalize backend transform results into a JSON-friendly dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    data: dict[str, Any] = {}
    for key in ("name", "label", "class_name", "path", "location", "rotation", "scale"):
        if hasattr(obj, key):
            val = getattr(obj, key)
            if isinstance(val, tuple):
                val = list(val)
            data[key] = val
    extra = getattr(obj, "extra", None)
    if isinstance(extra, dict):
        data.update({k: v for k, v in extra.items() if k not in data})
    data["_repr"] = repr(obj)[:400]
    return data


def _close(a, b, tol=1.0) -> bool:
    if a is None or b is None:
        return False
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def pick_actor(backend, prefer: list[str] | None = None) -> str | None:
    items: list = []
    # Prefer domain call (GenOrca shape): {success, actors:[{label,...}]}
    try:
        res = backend.call_domain("actor", "get_all_details", {})
        if isinstance(res, dict):
            items = res.get("actors") or []
    except Exception:
        items = []
    if not items:
        try:
            res = backend.get_actors()
        except Exception:
            return None
        if isinstance(res, dict):
            items = res.get("actors") or res.get("items") or res.get("data") or []
        elif isinstance(res, list):
            items = res
        else:
            items = []
    if not isinstance(items, list) or not items:
        return None
    names: list[str] = []
    for it in items:
        if isinstance(it, str):
            names.append(it)
        elif isinstance(it, dict):
            for k in ("label", "name", "actor_label", "actor_name"):
                if it.get(k):
                    names.append(str(it[k]))
                    break
    if not names:
        return None
    if prefer:
        for p in prefer:
            for n in names:
                if p.lower() == n.lower():
                    return n
            for n in names:
                if p.lower() in n.lower():
                    return n
    # Prefer small/restorable pieces over giant platform roots
    safe_hints = (
        "skirt", "post", "pillar", "column", "lantern", "prop", "test",
        "cube", "sphere", "light", "floor",
    )
    for hint in safe_hints:
        for n in names:
            if hint in n.lower():
                return n
    return names[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actor", default=None)
    ap.add_argument("--delta", type=float, default=5.0)
    ap.add_argument("--axis", default="z")
    ap.add_argument("--save", action="store_true", default=False)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config, reload=True)
    log = RunLogger(cfg.path("logs", ensure_parent=True), run_name="phase3_demo_a", console=True)
    bundle = build_bundle(cfg)
    executor = Executor(bundle, cfg, log)
    backend = bundle.unreal.get("UNREAL_MCP")

    evidence: dict = {
        "demo": "A",
        "started": _now(),
        "tcp_hint": None,
        "result": "FAIL",
        "steps": [],
    }

    if backend is None or not backend.available():
        diag = backend.diagnostics() if backend else {}
        evidence["error"] = "UNREAL_MCP unavailable"
        evidence["mcp_diagnostics"] = diag
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        bundle.close()
        return 2
    # available() above already initialized profile; keep a diagnostic snapshot
    evidence["mcp_profile"] = backend.profile
    evidence["mcp_caps"] = sorted(backend.capabilities())

    try:
        level = backend.current_level()
        evidence["level_before"] = level
    except Exception as exc:
        evidence["level_before_error"] = str(exc)[:200]

    actor = args.actor
    if not actor and args.auto:
        actor = pick_actor(backend, prefer=["Floor", "Ground", "Plaza", "Cube", "Prop"])
    if not actor:
        evidence["error"] = "no actor specified and auto-pick failed"
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        bundle.close()
        return 2
    evidence["actor"] = actor

    # BEFORE
    try:
        before_raw = backend.get_actor_transform(actor)
        before = _transform_payload(before_raw)
        evidence["before"] = before
        before_xyz = _as_xyz(before_raw if not isinstance(before_raw, dict) else before_raw)
        if before_xyz is None:
            before_xyz = _as_xyz(before.get("location") or before_raw)
    except Exception as exc:
        evidence["error"] = f"before read failed: {exc}"
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        bundle.close()
        return 2
    evidence["before_xyz"] = before_xyz
    if before_xyz is None:
        evidence["error"] = f"could not parse before transform: {before}"
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        bundle.close()
        return 2

    skill = get_skill("actor_move")
    axis = args.axis
    delta = float(args.delta)

    # MUTATE +1delta
    t0 = time.perf_counter()
    result1 = executor.run(skill, {"actor": actor, "axis": axis, "delta": delta})
    evidence["steps"].append({
        "step": "mutate_up",
        "params": {"actor": actor, "axis": axis, "delta": delta},
        "ok": result1.ok,
        "method": getattr(result1, "method", None) or getattr(result1, "chosen_method", None),
        "summary": result1.summary()[:400],
        "duration_ms": (time.perf_counter() - t0) * 1000,
        "as_dict": result1.as_dict() if hasattr(result1, "as_dict") else None,
    })
    evidence["result1_method"] = getattr(result1, "method", None)
    evidence["result1_ok"] = bool(result1.ok)

    try:
        mid_raw = backend.get_actor_transform(actor)
        evidence["after_mutate"] = _transform_payload(mid_raw)
        mid_xyz = _as_xyz(mid_raw)
    except Exception as exc:
        evidence["error"] = f"readback after mutate failed: {exc}"
        mid_xyz = None
    evidence["after_mutate_xyz"] = mid_xyz
    evidence["mutate_verified"] = _close(mid_xyz, (
        before_xyz[0] if axis != "x" else before_xyz[0] + delta,
        before_xyz[1] if axis != "y" else before_xyz[1] + delta,
        before_xyz[2] if axis != "z" else before_xyz[2] + delta,
    ), tol=2.0)

    # RESTORE absolute original location (idempotent)
    restore_params = {"actor": actor, "location": list(before_xyz), "absolute": True, "idempotent": True}
    t1 = time.perf_counter()
    result2 = executor.run(get_skill("actor_move"), restore_params)
    evidence["steps"].append({
        "step": "restore",
        "params": restore_params,
        "ok": result2.ok,
        "summary": result2.summary()[:400],
        "duration_ms": (time.perf_counter() - t1) * 1000,
    })
    try:
        after_raw = backend.get_actor_transform(actor)
        evidence["after_restore"] = _transform_payload(after_raw)
        after_xyz = _as_xyz(after_raw)
    except Exception as exc:
        evidence["error"] = f"readback after restore failed: {exc}"
        after_xyz = None
    evidence["after_restore_xyz"] = after_xyz
    evidence["restore_verified"] = _close(after_xyz, before_xyz, tol=2.0)

    # Idempotent re-apply restore
    result3 = executor.run(get_skill("actor_move"), restore_params)
    try:
        again_raw = backend.get_actor_transform(actor)
        again_xyz = _as_xyz(again_raw)
        evidence["after_idempotent"] = _transform_payload(again_raw)
    except Exception:
        again_xyz = None
    evidence["idempotent_reapply_verified"] = _close(again_xyz, before_xyz, tol=2.0)
    evidence["idempotent_xyz"] = again_xyz
    evidence["steps"].append({
        "step": "idempotent_reapply",
        "ok": result3.ok,
        "xyz": again_xyz,
    })

    # Optional save
    if args.save:
        try:
            save_res = backend.save_level()
            evidence["save"] = save_res
        except Exception as exc:
            evidence["save_error"] = str(exc)[:200]

    try:
        evidence["router_quality"] = executor.quality.summary()
        evidence["lock_stats"] = executor.coordinator.stats()
    except Exception:
        pass
    evidence["finished"] = _now()

    if evidence.get("mutate_verified") and evidence.get("restore_verified") and evidence.get("idempotent_reapply_verified"):
        evidence["result"] = "PASS"
    elif evidence.get("restore_verified") or evidence.get("mutate_verified"):
        evidence["result"] = "PARTIAL"
    else:
        evidence["result"] = "FAIL"

    out = cfg.path("logs") / "runs" / f"phase3_demo_a_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    print(f"\nRESULT={evidence['result']}")
    print(f"Evidence: {out}")
    print(f"Log: {log.jsonl.path if hasattr(log, 'jsonl') else log}")
    bundle.close()
    return 0 if evidence["result"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
