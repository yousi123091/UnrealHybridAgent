"""Dynamic outcome verification from action semantics (Phase 4B)."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence


def _xyz(v: Any) -> list[float] | None:
    if v is None:
        return None
    if isinstance(v, Mapping):
        if "x" in v:
            return [float(v["x"]), float(v["y"]), float(v["z"])]
        return None
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return [float(v[0]), float(v[1]), float(v[2])]
    if hasattr(v, "__iter__"):
        try:
            vals = list(v)[:3]
            return [float(x) for x in vals]
        except Exception:
            return None
    return None


def _close(a: Sequence[float] | None, b: Sequence[float] | None, tol: float) -> bool:
    if a is None or b is None or len(a) < 3 or len(b) < 3:
        return False
    return all(abs(float(a[i]) - float(b[i])) <= tol for i in range(3))


@dataclass
class VerificationResult:
    status: str  # PASS|FAIL|UNKNOWN
    action: str
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    failed_invariants: list[str] = field(default_factory=list)
    affected_targets: list[str] = field(default_factory=list)
    confidence: float = 1.0
    recommended_next_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "expected": self.expected,
            "observed": self.observed,
            "failed_invariants": self.failed_invariants,
            "affected_targets": self.affected_targets,
            "confidence": self.confidence,
            "recommended_next_action": self.recommended_next_action,
        }


class OutcomeVerifier:
    """Derive invariants from action semantics. Not an agent."""

    def __init__(self, location_tol: float = 1.5, rotation_tol: float = 1.0, scale_tol: float = 0.05):
        self.location_tol = float(location_tol)
        self.rotation_tol = float(rotation_tol)
        self.scale_tol = float(scale_tol)

    def verify(self, action: Mapping[str, Any] | str, observed: Mapping[str, Any]) -> VerificationResult:
        if isinstance(action, str):
            action = {"type": action}
        kind = str(action.get("type") or action.get("action") or "").lower()
        handler = {
            "set_location": self._set_location,
            "setlocation": self._set_location,
            "actor_move": self._set_location,
            "rigid_translate": self._rigid_translate,
            "batch_translate": self._rigid_translate,
            "batch_set_locations": self._batch_set_locations,
            "set_rotation": self._set_rotation,
            "set_visibility": self._set_visibility,
            "visibility": self._set_visibility,
            "support_bounds": self._support_bounds,
        }.get(kind)
        if handler is None:
            return VerificationResult(
                status="UNKNOWN",
                action=kind or "unknown",
                confidence=0.0,
                recommended_next_action="escalate_to_llm",
                expected={"type": kind},
                observed=dict(observed),
                failed_invariants=["no_semantic_verifier"],
            )
        return handler(action, observed)

    def _support_bounds(self, action, observed):
        """Explicit flat solid support only; arbitrary mesh AABBs cannot prove support."""
        label = str(action.get('actor', ''))
        if action.get('support_model') != 'axis_aligned_solid_box':
            return VerificationResult('UNKNOWN', 'support_bounds', confidence=0.0,
                                      failed_invariants=['unsupported_geometry'],
                                      recommended_next_action='query_collision_surface')
        try:
            actor, support = observed['actor'], observed['support']
            a, ae = _xyz(actor['bounds_origin']), _xyz(actor['bounds_extent'])
            s, se = _xyz(support['bounds_origin']), _xyz(support['bounds_extent'])
            if any(v is None or len(v)!=3 or not all(math.isfinite(x) for x in v) for v in (a,ae,s,se)):
                raise ValueError('Invalid bounds')
            if any(x<=0 for x in ae+se):raise ValueError('Degenerate bounds')
        except (KeyError, TypeError, ValueError):
            return VerificationResult('UNKNOWN', 'support_bounds', confidence=0.0,
                                      failed_invariants=['missing_bounds'], recommended_next_action='retry_read')
        gap=a[2]-ae[2]-(s[2]+se[2])
        contained=all(abs(a[i]-s[i])+ae[i]<=se[i]+self.location_tol for i in (0,1))
        failed=[]
        if not contained:failed.append('footprint_not_supported')
        if abs(gap)>self.location_tol:failed.append('contact_gap')
        return VerificationResult('FAIL' if failed else 'PASS', 'support_bounds',
                                  expected={'support_model':'axis_aligned_solid_box','tolerance_cm':self.location_tol},
                                  observed={'gap_cm':gap,'footprint_contained':contained},
                                  failed_invariants=failed,affected_targets=[label],
                                  recommended_next_action='repair_and_reread' if failed else '')

    def _set_location(self, action: Mapping[str, Any], observed: Mapping[str, Any]) -> VerificationResult:
        label = str(action.get("label") or action.get("actor") or observed.get("label") or "")
        target = _xyz(action.get("target") or action.get("location"))
        after = _xyz(observed.get("after") or observed.get("location") or observed.get("observed"))
        before = _xyz(observed.get("before"))
        rot_before = _xyz(observed.get("rotation_before") or observed.get("rotation"))
        rot_after = _xyz(observed.get("rotation_after") or observed.get("rotation"))
        failed = []
        if target is None or after is None:
            return VerificationResult(
                status="UNKNOWN",
                action="set_location",
                affected_targets=[label],
                expected={"target": target},
                observed={"after": after},
                failed_invariants=["missing_state"],
                confidence=0.0,
                recommended_next_action="retry_read",
            )
        if not _close(after, target, self.location_tol):
            failed.append("location_matches_target")
        if rot_before is not None and rot_after is not None and not _close(rot_after, rot_before, self.rotation_tol):
            failed.append("rotation_unchanged")
        status = "PASS" if not failed else "FAIL"
        rec = "" if status == "PASS" else "restore_checkpoint"
        return VerificationResult(
            status=status,
            action="set_location",
            expected={"target": target},
            observed={"after": after, "before": before},
            failed_invariants=failed,
            affected_targets=[label],
            confidence=1.0 if status == "PASS" else 0.8,
            recommended_next_action=rec,
        )

    def _batch_set_locations(self, action: Mapping[str, Any], observed: Mapping[str, Any]) -> VerificationResult:
        targets = action.get("targets") or action.get("locations") or {}
        after_map = observed.get("after") or observed.get("locations") or {}
        failed = []
        affected = []
        for label, loc in (targets.items() if isinstance(targets, Mapping) else []):
            after = _xyz(after_map.get(label) if isinstance(after_map, Mapping) else None)
            tgt = _xyz(loc)
            if not _close(after, tgt, self.location_tol):
                failed.append(f"location::{label}")
                affected.append(label)
        n = len(targets) if isinstance(targets, Mapping) else 0
        if n == 0:
            return VerificationResult("UNKNOWN", "batch_set_locations", confidence=0.0,
                                      recommended_next_action="retry_read", failed_invariants=["no_targets"])
        return VerificationResult(
            status="PASS" if not failed else "FAIL",
            action="batch_set_locations",
            expected={"targets": targets},
            observed={"after": after_map},
            failed_invariants=failed,
            affected_targets=affected or list(targets.keys()) if isinstance(targets, Mapping) else [],
            recommended_next_action="" if not failed else "restore_checkpoint",
        )

    def _rigid_translate(self, action: Mapping[str, Any], observed: Mapping[str, Any]) -> VerificationResult:
        delta = _xyz(action.get("delta") or action.get("translation"))
        items = observed.get("actors") or observed.get("results") or []
        failed = []
        affected = []
        pairwise_fail = []
        if delta is None or not isinstance(items, list) or not items:
            return VerificationResult("UNKNOWN", "rigid_translate", confidence=0.0,
                                      recommended_next_action="retry_read",
                                      failed_invariants=["missing_state"])
        befores = []
        afters = []
        for item in items:
            lab = str(item.get("label"))
            before = _xyz(item.get("before") or item.get("old"))
            after = _xyz(item.get("after") or item.get("location") or item.get("new"))
            expected = None
            if before is not None:
                expected = [before[i] + delta[i] for i in range(3)]
            if expected is None or after is None or not _close(after, expected, self.location_tol):
                failed.append(f"each_loc:: {lab}")
                affected.append(lab)
            if before:
                befores.append((lab, before))
            if after:
                afters.append((lab, after))
        # pairwise relative invariants
        for i in range(len(befores)):
            for j in range(i + 1, len(befores)):
                if befores[i][0] not in [a[0] for a in afters] or befores[j][0] not in [a[0] for a in afters]:
                    continue
                brel = [befores[i][1][k] - befores[j][1][k] for k in range(3)]
                amap = {a[0]: a[1] for a in afters}
                arel = [amap[befores[i][0]][k] - amap[befores[j][0]][k] for k in range(3)]
                if not _close(arel, brel, self.location_tol):
                    pairwise_fail.append(f"pairwise::{befores[i][0]}~{befores[j][0]}")
        failed.extend(pairwise_fail)
        count_ok = True
        status = "PASS" if not failed else "FAIL"
        return VerificationResult(
            status=status,
            action="rigid_translate",
            expected={"delta": delta, "n": len(items), "pairwise_relative_unchanged": True},
            observed={"items": items},
            failed_invariants=failed,
            affected_targets=affected,
            recommended_next_action="" if status == "PASS" else "restore_checkpoint",
        )

    def _set_rotation(self, action: Mapping[str, Any], observed: Mapping[str, Any]) -> VerificationResult:
        target = _xyz(action.get("target") or action.get("rotation"))
        after = _xyz(observed.get("rotation_after") or observed.get("rotation"))
        loc_before = _xyz(observed.get("location_before") or observed.get("before"))
        loc_after = _xyz(observed.get("location_after") or observed.get("location"))
        failed = []
        if target is None or after is None:
            return VerificationResult("UNKNOWN", "set_rotation", confidence=0.0,
                                      recommended_next_action="retry_read", failed_invariants=["missing_state"])
        if not _close(after, target, self.rotation_tol):
            failed.append("rotation_matches_target")
        if loc_before is not None and loc_after is not None and not _close(loc_after, loc_before, self.location_tol):
            failed.append("location_unchanged")
        status = "PASS" if not failed else "FAIL"
        return VerificationResult(
            status=status,
            action="set_rotation",
            expected={"rotation": target},
            observed={"rotation": after, "location": loc_after},
            failed_invariants=failed,
            recommended_next_action="" if status == "PASS" else "restore_checkpoint",
        )

    def _set_visibility(self, action: Mapping[str, Any], observed: Mapping[str, Any]) -> VerificationResult:
        want = action.get("visible")
        got = observed.get("visible_after", observed.get("visible"))
        failed = []
        if want is None or got is None:
            return VerificationResult("UNKNOWN", "set_visibility", confidence=0.0,
                                      recommended_next_action="retry_read", failed_invariants=["missing_state"])
        if bool(want) != bool(got):
            failed.append("visibility_matches_request")
        status = "PASS" if not failed else "FAIL"
        return VerificationResult(
            status=status,
            action="set_visibility",
            expected={"visible": want},
            observed={"visible": got},
            failed_invariants=failed,
            recommended_next_action="" if status == "PASS" else "retry_read",
        )


def build_failure_package(
    *,
    action: Mapping[str, Any],
    targets: Sequence[str],
    verification: VerificationResult | None,
    backend: str = "",
    fallback_history: Sequence[Mapping[str, Any]] | None = None,
    retry_count: int = 0,
    checkpoint_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rec = "restore_checkpoint" if checkpoint_id else "ask_confirmation"
    if verification and verification.recommended_next_action:
        rec = verification.recommended_next_action
    return {
        "action_attempted": dict(action),
        "targets": list(targets),
        "expected_state": (verification.expected if verification else {}),
        "observed_state": (verification.observed if verification else {}),
        "failed_invariants": (verification.failed_invariants if verification else []),
        "backend": backend,
        "fallback_history": list(fallback_history or []),
        "retry_count": int(retry_count),
        "checkpoint_id": checkpoint_id,
        "checkpoint_available": bool(checkpoint_id),
        "safe_recovery_options": ["retry_read", "restore_checkpoint", "ask_confirmation", "escalate_to_llm"],
        "recommended_next_action": rec,
        "extra": dict(extra or {}),
    }


__all__ = ["OutcomeVerifier", "VerificationResult", "build_failure_package"]
