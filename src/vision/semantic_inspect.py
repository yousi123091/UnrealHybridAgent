"""Semantic-first scene inspection (Phase 4A).

Replaces "Z high => floating => move to ground" as the default full-scene logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .semantic_support import SemanticSupportResolver, SupportMode, select_for_geometric_check


@dataclass
class SupportProbe:
    label: str
    semantic: str
    confidence: float
    support_method: str
    support_hit: bool | None = None
    hit_actor: str | None = None
    gap: float | None = None
    decision: str = ""
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "actor": self.label,
            "semantic": self.semantic,
            "confidence": self.confidence,
            "support_method": self.support_method,
            "support_hit": self.support_hit,
            "hit_actor": self.hit_actor,
            "gap": self.gap,
            "decision": self.decision,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _label_of(row: Mapping[str, Any]) -> str:
    return str(row.get("label") or row.get("name") or "")


def _loc_of(row: Mapping[str, Any]) -> tuple[float, float, float]:
    loc = row.get("location")
    if isinstance(loc, (list, tuple)) and len(loc) >= 3:
        return float(loc[0]), float(loc[1]), float(loc[2])
    return 0.0, 0.0, 0.0


def line_trace_down(backend: Any, label: str, z: float, *, drop: float = 8000.0) -> dict[str, Any]:
    start = None
    # prefer domain call with actor transform
    try:
        t = backend.get_actor_transform(label)
        loc = getattr(t, "location", None)
        if loc is None and isinstance(t, Mapping):
            loc = t.get("location")
        if loc:
            start = [float(loc[0]), float(loc[1]), float(loc[2]) + 50.0]
    except Exception:
        start = None
    if start is None:
        return {"ok": False, "error": "no origin"}
    end = [start[0], start[1], start[2] - float(drop)]
    try:
        hit = backend.call_domain("actor", "line_trace", {
            "ray_start": start,
            "ray_end": end,
            "actors_to_ignore_labels": [label],
        })
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    ok = bool(hit.get("success")) and bool(hit.get("hit"))
    impact = hit.get("location") or hit.get("impact_point")
    gap = None
    if ok and impact:
        gap = float(start[2]) - float(impact[2]) - 50.0
        if gap < 0:
            gap = float(z) - float(impact[2])
    return {
        "ok": ok,
        "hit": hit,
        "hit_actor": hit.get("hit_actor_label"),
        "impact": impact,
        "gap": gap,
        "normal": hit.get("normal") or hit.get("impact_normal"),
    }


def inspect_scene_semantic(
    backend: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    sample_limit: int | None = None,
    resolver: SemanticSupportResolver | None = None,
) -> dict[str, Any]:
    resolver = resolver or SemanticSupportResolver()
    all_rows = list(rows)
    if sample_limit:
        all_rows = all_rows[: int(sample_limit)]
    sem = resolver.resolve_many(all_rows)
    geo, hanging, needs_ai = select_for_geometric_check(sem)
    by_label = {_label_of(r): r for r in all_rows}

    probes: list[SupportProbe] = []

    # Sample representatives by mode
    wanted_modes = [
        SupportMode.GROUND,
        SupportMode.STRUCTURE,
        SupportMode.ATTACHED,
        SupportMode.HANGING,
        SupportMode.UNKNOWN,
    ]
    for mode in wanted_modes:
        members = [r for r in sem if r.support_mode == mode]
        for r in members[:3]:
            row = by_label.get(r.label, {})
            x, y, z = _loc_of(row)
            probe = SupportProbe(
                label=r.label,
                semantic=r.support_mode.value,
                confidence=r.confidence,
                support_method=r.recommended_check,
                reason=r.reason,
                evidence={"semantic_evidence": r.evidence, "actionable": r.actionable},
            )
            if r.support_mode == SupportMode.HANGING:
                probe.decision = "SKIP_HANGING"
                probe.support_hit = None
                probe.reason = "语义允许离地，不做 ground placement"
            elif r.support_mode == SupportMode.UNKNOWN or r.needs_ai:
                probe.decision = "REQUIRE_CONFIRMATION"
                probe.support_method = "vision_or_human"
                probe.reason = "UNKNOWN/低置信：禁止自动修改"
            elif r.support_mode == SupportMode.GROUND:
                # ground gap vs explicit ground_ref or local floor
                lt = line_trace_down(backend, r.label, z)
                probe.evidence["line_trace"] = lt
                probe.support_hit = lt.get("ok")
                probe.hit_actor = lt.get("hit_actor")
                probe.gap = lt.get("gap")
                if lt.get("ok") and lt.get("gap") is not None:
                    if float(lt["gap"]) <= 20.0:
                        probe.decision = "OK_ON_GROUND"
                    else:
                        probe.decision = "GROUND_GAP_SUSPECT"
                        probe.reason = f"GROUND 构件底边离支撑约 {lt['gap']:.1f}cm"
                else:
                    probe.decision = "GROUND_NO_TRACE"
            elif r.support_mode in (SupportMode.STRUCTURE, SupportMode.ATTACHED):
                lt = line_trace_down(backend, r.label, z)
                probe.evidence["line_trace"] = lt
                probe.support_hit = lt.get("ok")
                probe.hit_actor = lt.get("hit_actor")
                probe.gap = lt.get("gap")
                if lt.get("ok"):
                    probe.decision = "STRUCTURE_SUPPORTED"
                    probe.reason = f"下方命中 {lt.get('hit_actor')}，gap={lt.get('gap')}"
                else:
                    probe.decision = "STRUCTURE_NO_SUPPORT_TRACE"
                    probe.reason = "向下射线未命中支撑，需人工确认（禁止自动砸地）"
            probes.append(probe)

    mode_counts: dict[str, int] = {}
    for r in sem:
        key = r.support_mode.value
        mode_counts[key] = mode_counts.get(key, 0) + 1

    # Old-style geometry-only count for comparison (explicit ground if available)
    old_flagged = _legacy_geometry_flag_count(all_rows)

    semantic_auto_ok = [p for p in probes if p.decision in ("OK_ON_GROUND", "STRUCTURE_SUPPORTED", "SKIP_HANGING")]
    semantic_block = [p for p in probes if p.decision in ("REQUIRE_CONFIRMATION", "GROUND_GAP_SUSPECT", "STRUCTURE_NO_SUPPORT_TRACE")]

    return {
        "scanned": len(all_rows),
        "mode_counts": mode_counts,
        "geometry_candidates": [r.label for r in geo[:30]],
        "hanging_skipped": [r.label for r in hanging[:30]],
        "needs_ai": [r.label for r in needs_ai[:30]],
        "probes": [p.as_dict() for p in probes],
        "legacy_geometry_flagged": old_flagged,
        "semantic_actionable": len(semantic_auto_ok),
        "semantic_requires_confirmation": len(semantic_block),
        "principle": "semantic_first_geometry_second",
        "auto_modify_allowed": False,  # inspect never mutates
    }


def _legacy_geometry_flag_count(rows: list[Mapping[str, Any]], ground_z: float = 100.0, tol: float = 5.0) -> int:
    """Approximate old behavior: anything with bottom_z > ground+tol counts as floating."""
    n = 0
    for r in rows:
        origin = r.get("world_bounds_origin")
        extent = r.get("world_bounds_extent")
        if isinstance(origin, (list, tuple)) and isinstance(extent, (list, tuple)) and len(origin) >= 3 and len(extent) >= 3:
            bottom = float(origin[2]) - float(extent[2])
            if bottom > ground_z + tol:
                n += 1
                continue
        loc = r.get("location")
        if isinstance(loc, (list, tuple)) and len(loc) >= 3:
            if float(loc[2]) > ground_z + tol + 50:
                n += 1
    return n


__all__ = ["SupportProbe", "inspect_scene_semantic", "line_trace_down"]
