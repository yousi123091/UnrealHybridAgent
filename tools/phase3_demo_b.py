"""Phase 3 Demo B — semantic support classification + precise path evidence.

Does not force mutations. Classifies real scene actors and optionally runs
precise ground place / line_trace on a safe test subject if needed.
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
from src.runtime import build_bundle  # noqa: E402
from src.vision.semantic_support import SemanticSupportResolver  # noqa: E402


def _actor_dict(a: dict) -> dict[str, Any]:
    label = str(a.get("label") or a.get("name") or "")
    return {
        "label": label,
        "class": a.get("class"),
        "location": a.get("location"),
        "folder": a.get("folder"),
        "tags": a.get("tags"),
        "parent": a.get("parent") or a.get("attachment"),
        "mesh": a.get("static_mesh_asset_path") or a.get("mesh"),
    }


def classify(backend, samples: list[str]) -> list[dict]:
    res = backend.call_domain("actor", "get_all_details", {})
    actors = {str(x.get("label")): x for x in (res.get("actors") or [])}
    out = []
    resolver = SemanticSupportResolver()
    for name in samples:
        row = actors.get(name)
        if row is None:
            # fuzzy
            hits = [k for k in actors if name.lower() in k.lower()]
            if not hits:
                out.append({"label": name, "found": False})
                continue
            row = actors[hits[0]]
            name = hits[0]
        payload = _actor_dict(row)
        try:
            result = resolver.resolve(payload)
        except Exception as exc:  # noqa: BLE001
            result = {"error": str(exc)[:200]}
        if hasattr(result, "as_dict"):
            rd = result.as_dict()
        elif isinstance(result, dict):
            rd = result
        else:
            rd = {
                "support_mode": getattr(result, "support_mode", None),
                "confidence": getattr(result, "confidence", None),
                "reason": getattr(result, "reason", None),
                "recommended_check": getattr(result, "recommended_check", None),
                "evidence": getattr(result, "evidence", None),
            }
        out.append({"label": name, "found": True, "actor": payload, "semantic": rd})
    return out


def line_trace_probe(backend, actor: str) -> dict:
    try:
        if hasattr(backend, "call_domain"):
            raw = backend.call_domain("actor", "get_transform", {"actor_label": actor})
            loc = None
            if isinstance(raw, dict):
                loc = raw.get("location")
            if hasattr(raw, "location"):
                loc = list(raw.location)
            if loc is None:
                t = backend.get_actor_transform(actor)
                loc = list(getattr(t, "location", []) or [])
            if not loc:
                return {"ok": False, "error": "no location for trace origin"}
            # GenOrca ue_line_trace signature uses start_x/y/z + end_x/y/z
            sx, sy, sz = float(loc[0]), float(loc[1]), float(loc[2]) + 50.0
            ex, ey, ez = float(loc[0]), float(loc[1]), float(loc[2]) - 5000.0
            hit = backend.call_domain("actor", "line_trace", {
                "ray_start": [sx, sy, sz],
                "ray_end": [ex, ey, ez],
                "actors_to_ignore_labels": [actor],
            })
            return {"ok": True, "start": [sx, sy, sz], "end": [ex, ey, ez], "hit": hit}
        return {"ok": False, "error": "line_trace domain call unavailable"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=(
        "EXT_Plaza_Central,Hall_Floor,Platform_Skirt_Front,Roof_Main,Roof_EaveFront,"
        "Porch_Beam,Hall_Int_Beam_W,EXT_MarkerPost_0_W"
    ))
    ap.add_argument("--trace", default=None, help="actor label for line_trace probe")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config, reload=True)
    bundle = build_bundle(cfg)
    backend = bundle.unreal.get("UNREAL_MCP")
    evidence: dict = {
        "demo": "B",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "result": "FAIL",
        "samples": [],
        "invariants": {},
    }
    if backend is None or not backend.available():
        evidence["error"] = "UNREAL_MCP unavailable"
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        return 2

    samples = [s.strip() for s in args.samples.split(",") if s.strip()]
    rows = classify(backend, samples)
    evidence["samples"] = rows

    # Invariants
    by = {r.get("label"): r for r in rows if r.get("found")}
    sem = {k: (v.get("semantic") or {}) for k, v in by.items()}

    def mode(label):
        return (sem.get(label) or {}).get("support_mode")

    roofs = [k for k in by if k.lower().startswith("roof") or "roof" in k.lower()]
    floors = [k for k in by if "floor" in k.lower() or "plaza" in k.lower()]
    evidence["invariants"]["roof_not_ground_forced"] = all(mode(k) != "GROUND" for k in roofs) if roofs else None
    evidence["invariants"]["floor_or_plaza_is_ground_or_structure"] = all(
        mode(k) in ("GROUND", "STRUCTURE", "ATTACHED") for k in floors
    ) if floors else None
    evidence["invariants"]["modes"] = {k: mode(k) for k in by}

    # line_trace probe on requested or first found skirt/floor
    trace_actor = args.trace or next((k for k in by if "skirt" in k.lower() or "floor" in k.lower()), None)
    if trace_actor:
        evidence["line_trace"] = {"actor": trace_actor, **line_trace_probe(backend, trace_actor)}

    # Decide PASS/PARTIAL
    mode_hits = sum(1 for r in rows if r.get("found") and (r.get("semantic") or {}).get("support_mode"))
    roof_ok = evidence["invariants"]["roof_not_ground_forced"]
    if mode_hits >= 3 and roof_ok is not False:
        if evidence.get("line_trace", {}).get("ok"):
            evidence["result"] = "PASS"
        else:
            evidence["result"] = "PARTIAL"
    elif mode_hits > 0:
        evidence["result"] = "PARTIAL"
    else:
        evidence["result"] = "FAIL"

    out = cfg.path("logs") / "runs" / f"phase3_demo_b_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
    print(f"\nRESULT={evidence['result']}")
    print(f"Evidence: {out}")
    bundle.close()
    return 0 if evidence["result"] in ("PASS", "PARTIAL") else 2


if __name__ == "__main__":
    raise SystemExit(main())
