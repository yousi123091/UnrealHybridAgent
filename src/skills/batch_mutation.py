"""Batch mutation pipeline (Phase 4A)."""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..core.checkpoint import CheckpointStore, TaskCheckpoint, capture_actors
from ..core.errors import PreconditionFailed
from ..core.execution_mode import ApprovalGate, ApprovalRequest, ExecutionMode
from ..router.intent import TaskIntent
from ..validation.result import CheckResult
from ..validation.verifiers import Verifier
from .base import Skill, SkillContext, SkillTrace


class BatchMutationSkill(Skill):
    name = "batch_mutation"
    description = "批量修改 Actor（绝对坐标 / 统一 delta），带 checkpoint 与批量回读"
    keywords = ("批量", "batch", "一起改", "多个")
    preferred_methods = ("UNREAL_MCP",)
    supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON"})

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        # absolute mode is idempotent; relative batch is not by default
        return bool(params.get("absolute") or params.get("idempotent"))

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        return "batch_mutation"

    def intent(self, params: Mapping[str, Any], **overrides: Any) -> TaskIntent:
        targets = params.get("targets") or params.get("actors") or []
        n = len(targets) if isinstance(targets, (list, tuple)) else 1
        kw = {
            "skill": self.name,
            "description": f"batch mutation n={n}",
            "target_count": n,
            "needs_exact_values": bool(params.get("absolute") or params.get("axis")),
            "read_only": False,
        }
        kw.update(overrides)
        return TaskIntent(**kw)

    def preflight(self, ctx: SkillContext, params: Mapping[str, Any]) -> dict[str, Any]:
        backend = ctx.structured_or_none("UNREAL_MCP") or ctx.structured_or_none("UE_PYTHON")
        if backend is None:
            raise PreconditionFailed("batch_mutation 需要结构化后端")
        targets = self._targets(params)
        snaps = capture_actors(backend, targets)
        return {
            "actors": [s.as_dict() for s in snaps],
            "n": len(snaps),
            "mcp_calls_estimate": 1 + len(snaps),  # get_all optional + one read each
        }

    def _targets(self, params: Mapping[str, Any]) -> list[str]:
        t = params.get("targets") or params.get("actors") or []
        if isinstance(t, str):
            t = [x.strip() for x in t.split(",") if x.strip()]
        return [str(x) for x in t]

    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> dict[str, Any]:
        backend = ctx.structured(method) if method in ("UNREAL_MCP", "UE_PYTHON") else (
            ctx.structured_or_none("UNREAL_MCP") or ctx.structured_or_none("UE_PYTHON")
        )
        if backend is None:
            raise PreconditionFailed("batch_mutation 无可用结构化后端")

        targets = self._targets(params)
        limit = int(ctx.config.get("safety.batch_default_limit") or 20)
        if len(targets) > limit:
            raise PreconditionFailed(f"batch size {len(targets)} > limit {limit}")

        from ..adapters.unreal.batch_ops import UnrealBatchOps
        from ..verification.outcome import OutcomeVerifier, build_failure_package

        batch_ops = UnrealBatchOps(backend)
        verifier = OutcomeVerifier()

        # Capability probe once
        t_probe = time.perf_counter()
        _ = backend.available()
        probe_ms = (time.perf_counter() - t_probe) * 1000
        rtt = 1

        # Checkpoint
        store = CheckpointStore(ctx.config.path("workspace.state_dir") / "checkpoints")
        mode = str(params.get("execution_mode") or ctx.config.get("execution.mode") or "CONFIRM")
        gate: ApprovalGate | None = params.get("_approval_gate")  # type: ignore
        task_id = store.new_id()

        # Phase4B: UE-side batch before-read (1 RTT when available)
        t_read = time.perf_counter()
        before_payload = batch_ops.batch_read(targets) if batch_ops.available else None
        rtt += int((before_payload or {}).get("round_trips") or 0)
        if before_payload and before_payload.get("actors"):
            from ..core.checkpoint import ActorSnapshot

            before = []
            for row in before_payload["actors"]:
                if row.get("found") is False or row.get("error") or not row.get("location"):
                    raise PreconditionFailed(f"Cannot checkpoint target: {row.get('label')}: {row.get('error', 'not_found')}")
                loc = row["location"]
                before.append(ActorSnapshot(label=str(row.get("label")), location=[float(x) for x in loc[:3]],
                                            rotation=row.get("rotation"), scale=row.get("scale"),
                                            extra={"path": row.get("path"), "level": row.get("level")}))
        else:
            before = capture_actors(backend, targets)
            rtt += len(targets)
        before_ms = (time.perf_counter() - t_read) * 1000
        before_map = {s.label: list(s.location) for s in before}

        absolute = bool(params.get("absolute") or params.get("location") or params.get("locations"))
        axis = str(params.get("axis") or "z")
        delta = float(params.get("delta") or 0.0)
        locations = params.get("locations")

        plan_items = []
        for snap in before:
            if locations and snap.label in locations:
                target = [float(v) for v in locations[snap.label][:3]]
            elif absolute and params.get("location"):
                target = [float(v) for v in (params.get("location") or [])[:3]]
            else:
                target = list(snap.location)
                idx = {"x": 0, "y": 1, "z": 2}.get(axis, 2)
                target[idx] = target[idx] + delta
            plan_items.append({"label": snap.label, "before": snap.location, "target": target})

        # Approval gate (risk-based, batch-aware)
        if gate is not None:
            n = len(plan_items)
            req = ApprovalRequest(
                task_id=task_id,
                action="batch_mutation",
                mode=ExecutionMode.parse(mode),
                risk_level="medium" if (absolute or n <= getattr(gate, "auto_allow_max_batch", 3)) else "high",
                reasons=["batch mutation"],
                summary=f"batch n={n} absolute={absolute}",
                diff={"items": plan_items},
                batch_size=n,
                reversible=True,
                rollback_planned=True,
                verify_planned=True,
                extras={"large_batch": n > int(ctx.config.get("safety.approval_batch_threshold") or 5)},
            )
            gate.require(req)

        cp = TaskCheckpoint(
            task_id=task_id,
            created_at=time.time(),
            level=None,
            execution_mode=mode,
            plan={"items": plan_items, "absolute": absolute, "axis": axis, "delta": delta},
            actors=before,
            methods=[method],
        )
        try:
            cp.level = str(backend.current_level())
            rtt += 1
        except Exception:
            pass
        store.save(cp)

        # Phase4B write path: prefer UE-side batch loops
        mcp_calls = 1  # capability probe-ish
        t0 = time.perf_counter()
        failures = []
        write_payload: dict[str, Any] = {}
        action_sem: dict[str, Any]
        if not absolute and not locations:
            dvec = [0.0, 0.0, 0.0]
            dvec[{"x": 0, "y": 1, "z": 2}.get(axis, 2)] = delta
            action_sem = {"type": "rigid_translate", "delta": dvec, "targets": list(targets)}
        else:
            action_sem = {"type": "batch_set_locations", "targets": {i["label"]: i["target"] for i in plan_items}}

        if batch_ops.available and action_sem["type"] == "batch_set_locations":
            write_payload = batch_ops.batch_set_locations(action_sem["targets"])
            rtt += int(write_payload.get("round_trips") or 1)
        elif batch_ops.available and action_sem["type"] == "rigid_translate":
            write_payload = batch_ops.batch_translate(targets, action_sem["delta"])
            rtt += int(write_payload.get("round_trips") or 1)
        else:
            # fallback primitive writes
            for item in plan_items:
                try:
                    backend.set_actor_location(item["label"], item["target"][:3])
                    mcp_calls += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append({"label": item["label"], "error": f"{type(exc).__name__}: {exc}"})

        # Independent host-side batch readback (safety, cannot skip)
        t_rb = time.perf_counter()
        after_payload = batch_ops.batch_read(targets) if batch_ops.available else None
        rtt += int((after_payload or {}).get("round_trips") or 0)
        after = []
        observed_map: dict[str, list[float]] = {}
        if after_payload and after_payload.get("actors"):
            for row in after_payload["actors"]:
                loc = [float(x) for x in (row.get("location") or [])[:3]]
                tgt = next((i["target"] for i in plan_items if i["label"] == row.get("label")), None)
                observed_map[str(row.get("label"))] = loc
                after.append({
                    "label": row.get("label"),
                    "location": loc,
                    "target": tgt,
                    "ok": bool(tgt) and len(loc) == 3 and all(abs(a - b) <= 1.5 for a, b in zip(loc, tgt)),
                    "before": before_map.get(str(row.get("label"))),
                })
        else:
            for item in plan_items:
                try:
                    t = backend.get_actor_transform(item["label"])
                    rtt += 1
                    loc = [float(x) for x in list(getattr(t, "location", []) or [])[:3]]
                    observed_map[item["label"]] = loc
                    after.append({
                        "label": item["label"],
                        "location": loc,
                        "target": item["target"],
                        "ok": len(loc) == 3 and all(abs(a - b) <= 1.5 for a, b in zip(loc, item["target"][:3])),
                        "before": item["before"],
                    })
                except Exception as exc:  # noqa: BLE001
                    after.append({"label": item["label"], "error": str(exc)[:200], "ok": False})
        readback_ms = (time.perf_counter() - t_rb) * 1000

        # Dynamic outcome verification
        observed_for_ver = {
            "actors": after,
            "after": observed_map,
            "locations": observed_map,
        }
        if action_sem["type"] == "batch_set_locations":
            observed_for_ver["after"] = observed_map
        vres = verifier.verify(action_sem, observed_for_ver)

        wall_ms = (time.perf_counter() - t0) * 1000
        store.update_status(task_id, "executed", after=after, mcp_calls=mcp_calls + rtt)

        success_n = sum(1 for r in after if r.get("ok"))
        fail_n = len(after) - success_n
        failure_pkg = None
        if vres.status != "PASS":
            failure_pkg = build_failure_package(
                action=action_sem,
                targets=list(targets),
                verification=vres,
                backend=method,
                fallback_history=[],
                retry_count=0,
                checkpoint_id=task_id,
            )

        trace.before["actors"] = [a.as_dict() for a in before]
        trace.after["batch"] = {
            "task_id": task_id,
            "batch_size": len(plan_items),
            "success_count": success_n,
            "fail_count": fail_n,
            "perform_failures": failures,
            "retry_count": 0,
            "fallback_count": 0,
            "mcp_calls": mcp_calls + rtt,
            "ue_round_trips": rtt,
            "probe_ms": probe_ms,
            "before_read_ms": before_ms,
            "readback_ms": readback_ms,
            "wall_ms": wall_ms,
            "after": after,
            "plan": plan_items,
            "write_payload": {k: write_payload.get(k) for k in ("op", "n", "success", "latency_ms", "round_trips") if k in (write_payload or {})},
            "outcome_verification": vres.as_dict(),
            "failure_package": failure_pkg,
            "pipeline": "phase4b_ue_batch" if batch_ops.available else "phase4a_primitive",
        }
        return {
            "ok": vres.status == "PASS" and not failures,
            "transport": method,
            "batch": trace.after["batch"],
        }

    def verify(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> Any:
        v = Verifier()
        batch = trace.after.get("batch") or {}
        after = batch.get("after") or []
        ok_n = sum(1 for r in after if r.get("ok"))
        v.add(CheckResult(
            "batch.readback",
            passed=ok_n == len(after) and len(after) > 0,
            expected=f"{len(after)}/{len(after)}",
            actual=f"{ok_n}/{len(after)}",
            detail=f"batch_size={batch.get('batch_size')} wall_ms={batch.get('wall_ms')} rtt={batch.get('ue_round_trips')}",
        ))
        v.add(CheckResult(
            "batch.no_unplanned_retry",
            passed=(batch.get("retry_count") == 0),
            expected=0,
            actual=batch.get("retry_count"),
        ))
        ov = batch.get("outcome_verification") or {}
        v.add(CheckResult(
            "batch.dynamic_outcome",
            passed=(ov.get("status") == "PASS"),
            expected="PASS",
            actual=ov.get("status"),
            detail=f"invariants_failed={ov.get('failed_invariants')}",
        ))
        return v.run()


__all__ = ["BatchMutationSkill"]
