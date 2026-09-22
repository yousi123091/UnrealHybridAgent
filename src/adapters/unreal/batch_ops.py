"""UE-side batch transform I/O via execute_python (Phase 4B)."""

from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping, Sequence

# One UE-side loop: read N actors in a single execute_python call.
BATCH_READ_CODE = r"""
import unreal, json as _json

def _payload(labels):
    out = []
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    by = {}
    for a in actors:
        for key in (str(a.get_actor_label()), str(a.get_name()), str(a.get_path_name())):
            by.setdefault(key, []).append(a)
    for label in labels:
        rec = {"label": label, "found": False}
        try:
            matches = list(dict.fromkeys(by.get(label, [])))
            if len(matches) > 1:
                rec['error'] = 'ambiguous_identity'
                out.append(rec)
                continue
            hit = matches[0] if matches else None
            if hit is None:
                out.append(rec)
                continue
            loc = hit.get_actor_location()
            rot = hit.get_actor_rotation()
            sc = hit.get_actor_scale3d()
            rec.update({
                "found": True,
                "location": [float(loc.x), float(loc.y), float(loc.z)],
                "rotation": [float(rot.pitch), float(rot.yaw), float(rot.roll)],
                "scale": [float(sc.x), float(sc.y), float(sc.z)],
                "path": str(hit.get_path_name()),
                "class_name": str(hit.get_class().get_name()),
                "level": str(hit.get_level().get_path_name()),
                "folder": str(hit.get_folder_path()),
                "tags": [str(t) for t in hit.tags],
                "visible": not hit.is_hidden_ed(),
                "parent": str(hit.get_attach_parent_actor().get_actor_label()) if hit.get_attach_parent_actor() else None,
            })
            try:
                origin, extent = hit.get_actor_bounds(False)
                rec["bounds_origin"] = [float(origin.x), float(origin.y), float(origin.z)]
                rec["bounds_extent"] = [float(extent.x), float(extent.y), float(extent.z)]
            except Exception:
                pass
        except Exception as e:
            rec["error"] = str(e)
        out.append(rec)
    return {"success": all(r.get('found') and not r.get('error') for r in out), "actors": out, "count": len(out)}

_s = _json.dumps(_payload(__LABELS__))
__UHA_RESULT__ = {"ok": True, "result": _s}
try:
    unreal.MCPythonHelper.submit_result(_s)
except Exception:
    print(_s)
"""

# One UE-side loop: set absolute locations for N actors.
BATCH_SET_LOC_CODE = r"""
import unreal, json as _json

def _payload(items):
    results = []
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    by = {}
    for a in actors:
        try:
            for key in set((str(a.get_actor_label()), str(a.get_name()), str(a.get_path_name()))):
                by.setdefault(key, []).append(a)
        except Exception:
            continue
    for item in items:
        label = item.get("label")
        loc = item["location"]
        rec = {"label": label, "ok": False}
        matches = by.get(str(label), [])
        if len(matches) > 1:
            rec['error'] = 'ambiguous_identity'
            results.append(rec)
            continue
        a = matches[0] if matches else None
        if a is None:
            rec["error"] = "not_found"
            results.append(rec)
            continue
        try:
            a.set_actor_location(unreal.Vector(float(loc[0]), float(loc[1]), float(loc[2])), False, False)
            nl = a.get_actor_location()
            rec.update({"ok": True, "location": [float(nl.x), float(nl.y), float(nl.z)]})
        except Exception as e:
            rec["error"] = str(e)
        results.append(rec)
    return {"success": all(r.get("ok") for r in results), "results": results}

_s = _json.dumps(_payload(__ITEMS__))
__UHA_RESULT__ = {"ok": True, "result": _s}
try:
    unreal.MCPythonHelper.submit_result(_s)
except Exception:
    print(_s)
"""

# Rigid translate N actors by the same delta.
BATCH_TRANSLATE_CODE = r"""
import unreal, json as _json

def _payload(labels, delta):
    results = []
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    by = {}
    for a in actors:
        try:
            for key in set((str(a.get_actor_label()), str(a.get_name()), str(a.get_path_name()))):
                by.setdefault(key, []).append(a)
        except Exception:
            continue
    dx, dy, dz = float(delta[0]), float(delta[1]), float(delta[2])
    for label in labels:
        rec = {"label": label, "ok": False}
        matches = by.get(str(label), [])
        if len(matches) > 1:
            rec['error'] = 'ambiguous_identity'
            results.append(rec)
            continue
        a = matches[0] if matches else None
        if a is None:
            rec["error"] = "not_found"
            results.append(rec)
            continue
        try:
            ol = a.get_actor_location()
            before = [float(ol.x), float(ol.y), float(ol.z)]
            target = [before[0] + dx, before[1] + dy, before[2] + dz]
            a.set_actor_location(unreal.Vector(*target), False, False)
            nl = a.get_actor_location()
            rec.update({
                "ok": True,
                "before": before,
                "after": [float(nl.x), float(nl.y), float(nl.z)],
                "target": target,
            })
        except Exception as e:
            rec["error"] = str(e)
        results.append(rec)
    return {"success": all(r.get("ok") for r in results), "results": results}

_s = _json.dumps(_payload(__LABELS__, __DELTA__))
__UHA_RESULT__ = {"ok": True, "result": _s}
try:
    unreal.MCPythonHelper.submit_result(_s)
except Exception:
    print(_s)
"""


def _pylist(values: Sequence[Any]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _vector(values: Sequence[float]) -> list[float]:
    result = [float(x) for x in values]
    if len(result) != 3 or not all(math.isfinite(x) for x in result):
        raise ValueError('Expected exactly three finite coordinates')
    return result


def _parse_execute(res: Mapping[str, Any]) -> Any:
    raw = res.get("result") if isinstance(res, Mapping) else res
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            # try last JSON object in stdout
            text = raw.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except Exception:
                    return {"success": False, "raw": text[:500]}
            return {"success": False, "raw": text[:500]}
    return {"success": False, "raw": res}


class UnrealBatchOps:
    """Host-side wrapper for UE-internal batch loops."""

    def __init__(self, backend: Any):
        self.backend = backend

    @property
    def available(self) -> bool:
        try:
            return bool(self.backend and self.backend.available())
        except Exception:
            return False

    def _exec(self, code: str) -> tuple[dict[str, Any], float]:
        import time

        t0 = time.perf_counter()
        res = self.backend.execute_ue_python(code)
        ms = (time.perf_counter() - t0) * 1000
        payload = _parse_execute(res)
        if not isinstance(payload, dict):
            payload = {"success": False, "payload": payload}
        payload.setdefault("ok", bool(payload.get("success", False)))
        payload["round_trips"] = 1
        payload["latency_ms"] = ms
        return payload, ms

    def batch_read(self, labels: Iterable[str]) -> dict[str, Any]:
        labs = [str(x) for x in labels]
        code = BATCH_READ_CODE.replace("__LABELS__", _pylist(labs))
        out, ms = self._exec(code)
        out["op"] = "batch_read"
        out["n"] = len(labs)
        return out

    def batch_set_locations(self, mapping: Mapping[str, Sequence[float]]) -> dict[str, Any]:
        items = [{"label": str(k), "location": _vector(v)} for k, v in mapping.items()]
        code = BATCH_SET_LOC_CODE.replace("__ITEMS__", _pylist(items))
        out, ms = self._exec(code)
        out["op"] = "batch_set_locations"
        out["n"] = len(items)
        return out

    def batch_translate(self, labels: Iterable[str], delta: Sequence[float]) -> dict[str, Any]:
        labs = [str(x) for x in labels]
        d = _vector(delta)
        code = BATCH_TRANSLATE_CODE.replace("__LABELS__", _pylist(labs)).replace("__DELTA__", _pylist(d))
        out, ms = self._exec(code)
        out["op"] = "batch_translate"
        out["n"] = len(labs)
        out["delta"] = d
        return out


def primitive_read_cost(backend: Any, labels: Sequence[str]) -> dict[str, Any]:
    """Phase4A-style N get_actor_transform calls (for A/B only)."""
    import time

    t0 = time.perf_counter()
    actors = []
    calls = 0
    for lab in labels:
        try:
            t = backend.get_actor_transform(lab)
            calls += 1
            loc = getattr(t, "location", None)
            actors.append({"label": lab, "found": True, "location": list(loc) if loc else None})
        except Exception as exc:  # noqa: BLE001
            actors.append({"label": lab, "found": False, "error": str(exc)[:120]})
    return {
        "op": "primitive_read",
        "actors": actors,
        "n": len(labels),
        "round_trips": calls,
        "latency_ms": (time.perf_counter() - t0) * 1000,
        "ok": True,
    }


__all__ = ["UnrealBatchOps", "primitive_read_cost"]
