"""Ephemeral task-scoped scene model (Phase 4B). Not world truth."""

from __future__ import annotations

import threading
import time
from typing import Any

from .package import ActorFact, DeltaReport, ObservationPackage


class TaskSceneModel:
    """In-task cache only. Reality remains source of truth."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._lock = threading.RLock()
        self.generation = 0
        self.actors: dict[str, ActorFact] = {}
        self.anomalies: list[dict[str, Any]] = []
        self.last_package: ObservationPackage | None = None
        self.notes: list[str] = []
        self.stale = True
        self.stale_reason = "initial"

    def begin(self) -> None:
        with self._lock:
            self.stale = True
            self.stale_reason = "task_begin"
            self.notes.append(f"begin:{time.time()}")

    def invalidate(self, reason: str) -> None:
        with self._lock:
            self.stale = True
            self.stale_reason = reason

    def apply_package(self, pkg: ObservationPackage) -> None:
        with self._lock:
            self.generation = pkg.generation
            # A snapshot describes this scope now, not the union of every past scope.
            self.actors = {fact.label: fact for fact in pkg.targets}
            self.anomalies = list(pkg.anomalies)
            self.last_package = pkg
            self.stale = False
            self.stale_reason = ""

    def mark_changed(self, labels: list[str]) -> None:
        with self._lock:
            self.stale = True
            self.stale_reason = f"mutation:{','.join(labels[:8])}"

    def snapshot_facts(self) -> dict[str, ActorFact]:
        with self._lock:
            return dict(self.actors)

    def needs_full_refresh(self, *, force: bool = False, context: str = "") -> tuple[bool, str]:
        if force:
            return True, "force_refresh"
        if self.stale:
            return True, self.stale_reason or "stale"
        if context in ("gui_op", "unknown_backend", "large_batch", "external_suspect"):
            return True, context
        return False, ""

    def diff(self, new_pkg: ObservationPackage) -> DeltaReport:
        with self._lock:
            prev_gen = self.generation
            prev = dict(self.actors)
            prev_anoms = {json_key(a) for a in self.anomalies}
        changed = []
        unchanged = []
        for fact in new_pkg.targets:
            old = prev.get(fact.label)
            if old is None:
                changed.append({"label": fact.label, "kind": "new", "now": fact.as_dict()})
                continue
            deltas = {}
            for key in ("location", "rotation", "scale", "visible"):
                ov = getattr(old, key, None)
                nv = getattr(fact, key, None)
                if ov != nv:
                    deltas[key] = {"from": ov, "to": nv}
            if deltas:
                changed.append({"label": fact.label, "kind": "changed", "fields": deltas})
            else:
                unchanged.append(fact.label)
        new_keys = {json_key(a) for a in new_pkg.anomalies}
        new_anoms = [a for a in new_pkg.anomalies if json_key(a) not in prev_anoms]
        resolved = []
        old_by = {json_key(a): a for a in self.anomalies}
        for k in old_by:
            if k not in new_keys:
                resolved.append(old_by[k])
        return DeltaReport(
            task_id=self.task_id,
            from_generation=prev_gen,
            to_generation=new_pkg.generation,
            changed=changed,
            unchanged=unchanged,
            new_anomalies=new_anoms,
            resolved_anomalies=resolved,
            mode="delta",
        )


def json_key(anom: dict[str, Any]) -> str:
    return f"{anom.get('label')}|{anom.get('kind')}|{anom.get('detail', '')[:80]}"


__all__ = ["TaskSceneModel"]
