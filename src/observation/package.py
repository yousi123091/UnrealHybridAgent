"""Task-scoped observation package types (Phase 4B)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ActorFact:
    label: str
    location: list[float] | None = None
    rotation: list[float] | None = None
    scale: list[float] | None = None
    bounds_origin: list[float] | None = None
    bounds_extent: list[float] | None = None
    class_name: str = ""
    level: str | None = None
    visible: bool | None = None
    parent: str | None = None
    folder: str | None = None
    tags: list[str] = field(default_factory=list)
    semantic_mode: str | None = None
    semantic_confidence: float | None = None
    semantic_reason: str | None = None
    ground_kind: str | None = None  # GLOBAL/LOCAL/ELEVATED/UNKNOWN_GROUND
    support_method: str | None = None
    support_hit: bool | None = None
    support_gap: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != {} and v != []}


@dataclass
class ObservationPackage:
    task_id: str
    intent: str
    generation: int
    created_at: float = field(default_factory=time.time)
    scope: dict[str, Any] = field(default_factory=dict)
    targets: list[ActorFact] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    geometry: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    force_refresh_reason: str | None = None

    @property
    def generation_token(self) -> str:
        return f"{self.task_id}:{self.generation}"

    def labels(self) -> list[str]:
        return [t.label for t in self.targets]

    def payload_size(self) -> int:
        import json

        return len(json.dumps(self.as_dict(), ensure_ascii=False, default=str))

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "intent": self.intent,
            "generation": self.generation,
            "created_at": self.created_at,
            "scope": self.scope,
            "targets": [t.as_dict() for t in self.targets],
            "relations": self.relations,
            "geometry": self.geometry,
            "anomalies": self.anomalies,
            "provenance": self.provenance,
            "stats": self.stats,
            "force_refresh_reason": self.force_refresh_reason,
        }


@dataclass
class DeltaReport:
    task_id: str
    from_generation: int
    to_generation: int
    changed: list[dict[str, Any]] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    new_anomalies: list[dict[str, Any]] = field(default_factory=list)
    resolved_anomalies: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "delta"  # delta | full_refresh
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["ActorFact", "ObservationPackage", "DeltaReport"]
