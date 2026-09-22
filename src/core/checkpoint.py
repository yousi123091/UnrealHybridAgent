"""Task-level checkpoint + absolute-state rollback (Phase 4A)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass
class ActorSnapshot:
    label: str
    location: list[float]
    rotation: list[float] | None = None
    scale: list[float] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskCheckpoint:
    task_id: str
    created_at: float
    level: str | None
    execution_mode: str
    plan: dict[str, Any]
    actors: list[ActorSnapshot]
    methods: list[str] = field(default_factory=list)
    verify_evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = "created"  # created|executing|done|rolled_back|rollback_failed
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "created_at": self.created_at,
            "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.created_at)),
            "level": self.level,
            "execution_mode": self.execution_mode,
            "plan": self.plan,
            "actors": [a.as_dict() for a in self.actors],
            "methods": self.methods,
            "verify_evidence": self.verify_evidence,
            "notes": self.notes,
            "status": self.status,
            "path": self.path,
        }


class CheckpointStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        return f"task-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def path_for(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"

    def save(self, cp: TaskCheckpoint) -> Path:
        p = self.path_for(cp.task_id)
        cp.path = str(p)
        p.write_text(json.dumps(cp.as_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return p

    def load(self, task_id: str) -> dict[str, Any]:
        p = self.path_for(task_id)
        if not p.is_file():
            # allow full filename
            p2 = self.root / task_id
            if p2.is_file():
                p = p2
            else:
                raise FileNotFoundError(f"checkpoint not found: {task_id}")
        return json.loads(p.read_text(encoding="utf-8"))

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))

    def update_status(self, task_id: str, status: str, **fields: Any) -> dict[str, Any]:
        data = self.load(task_id)
        data["status"] = status
        data.update(fields)
        p = self.path_for(task_id)
        if not p.is_file():
            p = self.root / f"{task_id}"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return data


def _xyz_from(obj: Any) -> tuple[list[float], list[float] | None, list[float] | None]:
    if isinstance(obj, Mapping):
        loc = obj.get("location")
        if loc is None or len(loc) != 3:
            raise ValueError("Checkpoint requires a real three-component location")
        rot = obj.get("rotation")
        scale = obj.get("scale")
        return [float(x) for x in loc[:3]], (list(rot[:3]) if rot else None), (list(scale[:3]) if scale else None)
    loc = getattr(obj, "location", None)
    if loc is None or len(loc) != 3:
        raise ValueError("Checkpoint requires a real three-component location")
    rot = getattr(obj, "rotation", None)
    scale = getattr(obj, "scale", None)
    return (
        [float(loc[0]), float(loc[1]), float(loc[2])],
        [float(rot[0]), float(rot[1]), float(rot[2])] if rot else None,
        [float(scale[0]), float(scale[1]), float(scale[2])] if scale else None,
    )


def capture_actors(backend: Any, labels: list[str]) -> list[ActorSnapshot]:
    snaps: list[ActorSnapshot] = []
    for label in labels:
        t = backend.get_actor_transform(label)
        loc, rot, scale = _xyz_from(t)
        snaps.append(ActorSnapshot(label=label, location=loc, rotation=rot, scale=scale))
    return snaps


def rollback_task(
    backend: Any,
    store: CheckpointStore,
    task_id: str,
    *,
    verify_tol: float = 1.0,
) -> dict[str, Any]:
    """Absolute restore of actors recorded in checkpoint. No reverse-delta."""
    data = store.load(task_id)
    if data.get("level") and str(backend.current_level()) != data["level"]:
        raise ValueError("Checkpoint belongs to another level; rollback refused")
    actors = data.get("actors") or []
    results = []
    ok_n = 0
    fail_n = 0
    for row in actors:
        label = row.get("label")
        target = row.get("location")
        entry: dict[str, Any] = {"label": label, "target": target}
        try:
            # idempotent absolute set
            backend.set_actor_location(label, target[:3])
            if row.get("rotation") is not None:
                backend.set_actor_rotation(label, row["rotation"])
            if row.get("scale") is not None:
                backend.set_actor_scale(label, row["scale"])
            cur = backend.get_actor_transform(label)
            cur_loc, cur_rot, cur_scale = _xyz_from(cur)
            entry["after"] = cur_loc
            entry["verified"] = all(abs(a - b) <= verify_tol for a, b in zip(cur_loc, target[:3]))
            for field, observed, tolerance in (("rotation", cur_rot, verify_tol), ("scale", cur_scale, 0.01)):
                if row.get(field) is not None:
                    entry[field] = observed
                    entry["verified"] = entry["verified"] and observed is not None and len(observed) == 3 and all(
                        abs(a-b) <= tolerance for a,b in zip(observed, row[field]))
            if entry["verified"]:
                ok_n += 1
            else:
                fail_n += 1
                entry["error"] = "readback mismatch"
        except Exception as exc:  # noqa: BLE001
            fail_n += 1
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)

    status = "done"
    if fail_n and ok_n:
        status = "PARTIAL"
        store.update_status(task_id, "rollback_partial", rollback_results=results)
    elif fail_n:
        status = "FAIL"
        store.update_status(task_id, "rollback_failed", rollback_results=results)
    else:
        store.update_status(task_id, "rolled_back", rollback_results=results)

    return {
        "task_id": task_id,
        "status": status,
        "ok": fail_n == 0 and ok_n > 0,
        "restored": ok_n,
        "failed": fail_n,
        "results": results,
        "checkpoint": data,
    }


__all__ = [
    "ActorSnapshot",
    "TaskCheckpoint",
    "CheckpointStore",
    "capture_actors",
    "rollback_task",
]
