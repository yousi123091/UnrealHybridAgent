"""Task-scoped observe_scene (Phase 4B)."""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Sequence

from ..semantics.profile import SemanticProfile, load_profile
from ..vision.semantic_support import SemanticSupportResolver
from .package import ActorFact, ObservationPackage
from .scene_model import TaskSceneModel

INTENT_PRESETS: dict[str, dict[str, Any]] = {
    "inspect_floating": {"geometry": ["support", "gap"], "semantic": True, "anomalies": True},
    "inspect_structural_anomalies": {"geometry": ["support", "gap"], "semantic": True, "anomalies": True},
    "inspect_overlap": {"geometry": ["overlap"], "semantic": False, "anomalies": True},
    "inspect_relative_layout": {"geometry": ["relative"], "semantic": True, "relations": True},
    "inspect_attachment": {"relations": True, "semantic": True},
    "prepare_batch_movement": {"semantic": True, "relations": False, "geometry": [], "basic": True},
    "basic": {"basic": True, "semantic": True},
}


def _vec(payload: Mapping[str, Any], *keys: str) -> list[float] | None:
    for k in keys:
        v = payload.get(k)
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return [float(v[0]), float(v[1]), float(v[2])]
        if isinstance(v, Mapping) and {"x", "y", "z"} <= set(v):
            return [float(v["x"]), float(v["y"]), float(v["z"])]
    return None


class TaskObserver:
    def __init__(
        self,
        backend: Any,
        *,
        task_id: str = "task",
        profile: SemanticProfile | None = None,
        config: Mapping[str, Any] | None = None,
        scene_model: TaskSceneModel | None = None,
        batch_ops: Any | None = None,
    ):
        self.backend = backend
        self.task_id = task_id
        self.config = dict(config or {})
        self.profile = profile or load_profile(self.config.get("semantics.profile") or "example_palace", config=self.config)
        self.resolver = SemanticSupportResolver(overrides=(self.config.get("semantic") or {}))
        self.scene = scene_model or TaskSceneModel(task_id)
        if batch_ops is None:
            try:
                from ..adapters.unreal.batch_ops import UnrealBatchOps

                batch_ops = UnrealBatchOps(backend)
            except Exception:
                batch_ops = None
        self.batch_ops = batch_ops
        self.last_rtt = 0
        self.last_latency_ms = 0.0
        self.last_mode = ""

    def observe(
        self,
        targets: Sequence[str] | None = None,
        *,
        intent: str = "basic",
        scope: Mapping[str, Any] | None = None,
        profile: str | None = None,
        force_refresh: bool = False,
        context: str = "",
        max_neighbors: int = 0,
        proximity_cm: float = 1500.0,
    ) -> ObservationPackage:
        t0 = time.perf_counter()
        preset = INTENT_PRESETS.get(intent, INTENT_PRESETS["basic"])
        scope = dict(scope or {})
        need_full, reason = self.scene.needs_full_refresh(force=force_refresh, context=context)
        use_batch = bool(self.batch_ops and getattr(self.batch_ops, "available", False))
        rtt = 0
        mode = "batch" if use_batch else "primitive"
        if need_full:
            mode = f"full_{mode}"

        labels = [str(x) for x in (targets or [])]
        # relevance expansion: load all once if no targets, then filter
        all_rows: list[dict[str, Any]] = []
        # Explicit targets require one scoped batch read. Full discovery is opt-in
        # for unknown targets / requested neighbours, never a hidden per-step cost.
        if not labels or max_neighbors > 0 or scope.get("discover"):
            res = self.backend.call_domain("actor", "get_all_details", {}) if hasattr(self.backend, "call_domain") else self.backend.get_actors()
            rtt += 1
            rows = res.get("actors") if isinstance(res, Mapping) else res
            if not isinstance(rows, list):
                raise ValueError("Scene discovery did not return actor evidence")
            all_rows = [dict(r) for r in rows if isinstance(r, Mapping)]

        by_label = {str(r.get("label") or r.get("name")): r for r in all_rows}
        if not labels:
            # pick a small default set: first few non-sky actors
            labels = [k for k in list(by_label)[:20] if not any(x in k.lower() for x in ("sky", "light", "atmosphere", "fog"))][:8]

        # relevance filter
        selected = self._select_relevant(labels, by_label, intent=intent, proximity_cm=proximity_cm, max_neighbors=max_neighbors, scope=scope)

        facts_map: dict[str, ActorFact] = {}
        if use_batch and selected:
            payload = self.batch_ops.batch_read(selected)
            rtt += int(payload.get("round_trips") or 1)
            for row in payload.get("actors") or []:
                if row.get("found") is False or row.get("error"):
                    continue
                lab = str(row.get("label"))
                base = by_label.get(lab, {})
                facts_map[lab] = self._to_fact(base, row, intent=intent, preset=preset, profile_name=profile)
        else:
            for lab in selected:
                base = by_label.get(lab, {})
                loc = _vec(base, "location")
                if loc is None:
                    try:
                        t = self.backend.get_actor_transform(lab)
                        rtt += 1
                        loc = list(getattr(t, "location", []) or [])
                        base = {**base, "location": loc, "rotation": getattr(t, "rotation", None), "scale": getattr(t, "scale", None)}
                    except Exception:
                        loc = None
                facts_map[lab] = self._to_fact(base, {"label": lab, "found": loc is not None, "location": loc},
                                                intent=intent, preset=preset, profile_name=profile)

        # relations / nearby
        relations: list[dict[str, Any]] = []
        geometry: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        if preset.get("relations") or intent.startswith("inspect_attachment"):
            for lab, fact in list(facts_map.items())[:20]:
                parent = fact.parent
                if parent:
                    relations.append({"from": lab, "type": "attach", "to": parent})
        if preset.get("geometry") and any(g in preset["geometry"] for g in ("support", "gap")):
            for lab, fact in facts_map.items():
                if fact.semantic_mode in ("HANGING",) or fact.support_method in ("none", "ground_gap_local"):
                    continue
                if fact.semantic_mode in ("STRUCTURE", "ATTACHED", "GROUND") and fact.location:
                    try:
                        from ..vision.semantic_inspect import line_trace_down

                        lt = line_trace_down(self.backend, lab, fact.location[2])
                        rtt += int(lt.get("round_trips") or 1)
                        fact.support_hit = bool(lt.get("ok"))
                        fact.support_gap = lt.get("gap")
                        extra = {"label": lab, "kind": "support", "hit_actor": lt.get("hit_actor"), "gap": lt.get("gap")}
                        geometry.append(extra)
                        if fact.semantic_mode == "GROUND" and fact.ground_kind == "GLOBAL_GROUND" and fact.support_gap is not None and float(fact.support_gap) > 30:
                            anomalies.append({"label": lab, "kind": "ground_gap", "detail": f"gap={fact.support_gap}"})
                        if fact.semantic_mode == "STRUCTURE" and fact.support_hit is False:
                            anomalies.append({"label": lab, "kind": "structure_no_support", "detail": "no downward hit"})
                    except Exception as exc:
                        geometry.append({"label": lab, "kind": "support", "status": "UNKNOWN", "error": str(exc)})
        if preset.get("geometry") and "overlap" in preset["geometry"]:
            anomalies.extend(self._overlap_candidates(list(facts_map.values())))

        if preset.get("anomalies") is False:
            anomalies = []

        generation = self.scene.generation + 1
        pkg = ObservationPackage(
            task_id=self.task_id,
            intent=intent,
            generation=max(generation, 1),
            scope={"requested_targets": labels, "selected": selected, "intent": intent},
            targets=list(facts_map.values()),
            relations=relations,
            geometry=geometry,
            anomalies=anomalies,
            provenance={
                "backend": getattr(self.backend, "name", type(self.backend).__name__),
                "mode": mode,
                "profile": self.profile.name if profile is None else profile,
                "source_of_truth": "live_ue",
                "ephemeral": True,
                "missing_targets": [lab for lab in labels if lab not in facts_map],
            },
            stats={
                "rtt": rtt,
                "selected_n": len(selected),
                "requested_n": len(labels),
                "scene_rows": len(all_rows),
                "force_refresh": need_full,
                "force_refresh_reason": reason,
            },
            force_refresh_reason=reason if need_full else None,
        )
        self.scene.apply_package(pkg)
        self.last_rtt = rtt
        self.last_latency_ms = (time.perf_counter() - t0) * 1000
        self.last_mode = mode
        pkg.stats["latency_ms"] = self.last_latency_ms
        pkg.stats["payload_bytes"] = pkg.payload_size()
        return pkg

    def observe_delta(self, **kwargs: Any):
        return self.observe_delta_safe(**kwargs)

    def observe_delta_safe(self, **kwargs: Any):
        """Preferred API: snapshot prior, observe, diff."""
        self._prior_facts = self.scene.snapshot_facts()
        prior_gen = self.scene.generation
        prior_anoms = list(self.scene.anomalies)
        force = bool(kwargs.get("force_refresh", False))
        context = str(kwargs.get("context") or "")
        need_full, reason = self.scene.needs_full_refresh(force=force, context=context)
        pkg = self.observe(**{**kwargs, "force_refresh": force})
        if need_full:
            from .package import DeltaReport

            return pkg, DeltaReport(self.task_id, prior_gen, pkg.generation, mode="full_refresh", reason=reason,
                                    changed=[{"label": t.label, "kind": "snapshot"} for t in pkg.targets])
        # diff
        from .package import DeltaReport
        from .scene_model import json_key

        changed = []
        unchanged = []
        for fact in pkg.targets:
            old = self._prior_facts.get(fact.label)
            if old is None:
                changed.append({"label": fact.label, "kind": "new", "now": fact.as_dict()})
                continue
            fields = {}
            for key in ("location", "rotation", "scale", "visible"):
                if getattr(old, key, None) != getattr(fact, key, None):
                    fields[key] = {"from": getattr(old, key, None), "to": getattr(fact, key, None)}
            if fields:
                changed.append({"label": fact.label, "kind": "changed", "fields": fields})
            else:
                unchanged.append(fact.label)
        prev_k = {json_key(a) for a in prior_anoms}
        new_k = {json_key(a) for a in pkg.anomalies}
        new_anoms = [a for a in pkg.anomalies if json_key(a) not in prev_k]
        resolved = [a for a in prior_anoms if json_key(a) not in new_k]
        return pkg, DeltaReport(self.task_id, prior_gen, pkg.generation, changed=changed, unchanged=unchanged,
                                new_anomalies=new_anoms, resolved_anomalies=resolved, mode="delta")

    def _select_relevant(
        self,
        requested: Sequence[str],
        by_label: Mapping[str, Mapping[str, Any]],
        *,
        intent: str,
        proximity_cm: float,
        max_neighbors: int,
        scope: Mapping[str, Any],
    ) -> list[str]:
        selected: list[str] = []
        seen = set()

        def add(label: str) -> None:
            if label and label not in seen:
                seen.add(label)
                selected.append(label)

        for lab in requested:
            add(lab)
        # attachment relation
        for lab in list(selected):
            row = by_label.get(lab) or {}
            parent = str(row.get("parent") or row.get("attach_parent") or "")
            if parent:
                add(parent)
        # spatial proximity to explicit targets
        centers = []
        for lab in requested:
            loc = _vec(by_label.get(lab) or {}, "location")
            if loc:
                centers.append((lab, loc))
        if centers and max_neighbors > 0:
            scored = []
            for lab, row in by_label.items():
                if lab in seen:
                    continue
                loc = _vec(row, "location")
                if not loc:
                    continue
                best = min(((loc[0] - c[0]) ** 2 + (loc[1] - c[1]) ** 2 + (loc[2] - c[2]) ** 2) ** 0.5 for _, c in centers)
                if best <= proximity_cm:
                    scored.append((best, lab))
            scored.sort()
            for _, lab in scored[:max_neighbors]:
                add(lab)
        # recently changed from scene model
        if self.scene.stale and self.scene.stale_reason.startswith("mutation:"):
            for lab in self.scene.stale_reason.split(":", 1)[1].split(","):
                add(lab.strip())
        return selected

    def _to_fact(self, base: Mapping[str, Any], row: Mapping[str, Any], *, intent: str, preset: Mapping[str, Any], profile_name: str | None) -> ActorFact:
        label = str(base.get("label") or row.get("label") or "")
        payload = {**base, **{k: v for k, v in row.items() if v is not None}}
        payload.setdefault("label", label)
        base_res = self.resolver.resolve(payload)
        enriched = self.profile.enrich(base_res)
        if profile_name:
            # allow explicit profile override name only for provenance
            enriched["profile"] = profile_name
        return ActorFact(
            label=label,
            location=_vec(payload, "location"),
            rotation=_vec(payload, "rotation"),
            scale=_vec(payload, "scale"),
            bounds_origin=_vec(payload, "world_bounds_origin", "bounds_origin"),
            bounds_extent=_vec(payload, "world_bounds_extent", "bounds_extent"),
            class_name=str(payload.get("class") or payload.get("class_name") or ""),
            level=str(payload.get("level") or "") or None,
            parent=str(payload.get("parent") or payload.get("attach_parent") or "") or None,
            folder=str(payload.get("folder") or "") or None,
            tags=list(payload.get("tags") or []),
            visible=payload.get("visible"),
            semantic_mode=str(enriched.get("support_mode") or ""),
            semantic_confidence=float(enriched.get("confidence") or 0),
            semantic_reason=str(enriched.get("reason") or ""),
            ground_kind=enriched.get("ground_kind"),
            support_method=str(enriched.get("recommended_check") or ""),
            support_hit=None,
            extra={"profile_role": enriched.get("profile_role"), "actionable": enriched.get("actionable"),
                   "raw_evidence": dict(payload)},
        )

    @staticmethod
    def _overlap_candidates(facts: Sequence[ActorFact]) -> list[dict[str, Any]]:
        out = []
        for i, a in enumerate(facts):
            if not a.location or not a.bounds_extent:
                continue
            for b in facts[i + 1 :]:
                if not b.location or not b.bounds_extent:
                    continue
                ac = a.bounds_origin or a.location
                bc = b.bounds_origin or b.location
                dx = abs(ac[0] - bc[0])
                dy = abs(ac[1] - bc[1])
                dz = abs(ac[2] - bc[2])
                ox = a.bounds_extent[0] + b.bounds_extent[0] - dx
                oy = a.bounds_extent[1] + b.bounds_extent[1] - dy
                oz = a.bounds_extent[2] + b.bounds_extent[2] - dz
                if ox > 5 and oy > 5 and oz > 20:
                    out.append({"labels": [a.label, b.label], "kind": "overlap_candidate",
                                "detail": f"overlap≈{min(ox, oy, oz):.1f}cm"})
        return out


__all__ = ["TaskObserver", "INTENT_PRESETS"]
