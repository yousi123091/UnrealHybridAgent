"""Phase 4A offline tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.checkpoint import ActorSnapshot, CheckpointStore, TaskCheckpoint, rollback_task  # noqa: E402
from src.core.execution_mode import (  # noqa: E402
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    ExecutionMode,
)
from src.core.session_control import ControlCommand, SessionController, TaskPhase  # noqa: E402
from src.router.capability_cache import CapabilityCache, reset_capability_cache  # noqa: E402
from src.router.rules import evaluate  # noqa: E402
from src.router.intent import TaskIntent  # noqa: E402
from src.vision.semantic_inspect import inspect_scene_semantic  # noqa: E402


def test_execution_mode_parse() -> None:
    assert ExecutionMode.parse("auto") == ExecutionMode.AUTO
    assert ExecutionMode.parse("CONFIRM") == ExecutionMode.CONFIRM
    assert ExecutionMode.parse(None) == ExecutionMode.CONFIRM


def test_gate_read_allow() -> None:
    g = ApprovalGate(ExecutionMode.CONFIRM)
    r = g.evaluate(ApprovalRequest("t", "inspect", ExecutionMode.CONFIRM, "low", extras={"read_only": True}))
    assert r.decision == ApprovalDecision.ALLOW


def test_gate_save_allow() -> None:
    g = ApprovalGate(ExecutionMode.CONFIRM)
    r = g.evaluate(ApprovalRequest("t", "level_save", ExecutionMode.CONFIRM, "low", reversible=True))
    assert r.decision == ApprovalDecision.ALLOW


def test_gate_auto_blocks_unknown() -> None:
    g = ApprovalGate(ExecutionMode.AUTO)
    r = g.evaluate(ApprovalRequest(
        "t", "actor_move", ExecutionMode.AUTO, "medium",
        semantic_modes=["UNKNOWN"], reversible=True, rollback_planned=True,
    ))
    assert r.decision == ApprovalDecision.REQUIRE_CONFIRMATION


def test_gate_confirm_batch_requires() -> None:
    g = ApprovalGate(ExecutionMode.CONFIRM, batch_confirm_threshold=3)
    r = g.evaluate(ApprovalRequest(
        "t", "batch_mutation", ExecutionMode.CONFIRM, "medium",
        batch_size=10, reversible=True,
    ))
    assert r.decision == ApprovalDecision.REQUIRE_CONFIRMATION


def test_gate_confirm_deny_by_user() -> None:
    g = ApprovalGate(ExecutionMode.CONFIRM, confirmer=lambda req: False)
    r = g.evaluate(ApprovalRequest(
        "t", "actor_move", ExecutionMode.CONFIRM, "medium", reversible=True,
        rollback_planned=True,
    ))
    assert r.decision == ApprovalDecision.DENY


def test_gate_auto_allow_small_safe_batch() -> None:
    g = ApprovalGate(ExecutionMode.AUTO, auto_allow_max_batch=3)
    r = g.evaluate(ApprovalRequest(
        "t", "batch_mutation", ExecutionMode.AUTO, "low",
        batch_size=2, reversible=True, rollback_planned=True, verify_planned=True,
    ))
    assert r.decision == ApprovalDecision.ALLOW


def test_save_rule_prefers_structured() -> None:
    intent = TaskIntent(skill="level_save", description="save", target_count=1,
                        needs_exact_values=False, read_only=False,
                        context={"prefer_api_save": True})
    cands = evaluate(intent, available={"UNREAL_MCP", "KEYBOARD", "UE_PYTHON"})
    assert cands, "save should have candidates"
    assert cands[0].method == "UNREAL_MCP"
    assert cands[0].rule == "structured_save"


def test_capability_cache_hit_and_invalidate() -> None:
    reset_capability_cache()
    cache = CapabilityCache(ttl_s=30)
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return {"UNREAL_MCP": True, "KEYBOARD": True}

    a = cache.get(probe)
    b = cache.get(probe)
    assert calls["n"] == 1
    assert a.ready["UNREAL_MCP"] is True
    assert b.ready["UNREAL_MCP"] is True
    cache.invalidate("test")
    cache.get(probe)
    assert calls["n"] == 2
    assert cache.stats()["hits"] >= 1


def test_semantic_inspect_no_auto_modify() -> None:
    class FakeBackend:
        def get_actor_transform(self, label):
            class T:
                location = (0.0, 0.0, 2000.0)
            return T()

        def call_domain(self, tool, action, params=None):
            return {"success": True, "hit": True, "location": [0, 0, 1500],
                    "hit_actor_label": "Beam_X", "normal": [0, 0, 1]}

    rows = [
        {"label": "EXT_Plaza_Central", "location": [0, 0, 0]},
        {"label": "Roof_Main", "location": [0, 0, 2300], "world_bounds_origin": [0, 0, 2300],
         "world_bounds_extent": [100, 100, 50]},
        {"label": "Hall_Int_Beam_W", "location": [0, 0, 2100]},
        {"label": "EXT_MarkerPost_0_W", "location": [0, 0, 200]},
    ]
    out = inspect_scene_semantic(FakeBackend(), rows)
    assert out["auto_modify_allowed"] is False
    by = {p["actor"]: p for p in out["probes"]}
    assert by["Roof_Main"]["semantic"] == "STRUCTURE"
    assert by["Roof_Main"]["decision"] != "OK_ON_GROUND"
    assert by["EXT_Plaza_Central"]["semantic"] == "GROUND"
    assert "UNKNOWN" in out["mode_counts"] or by.get("EXT_MarkerPost_0_W", {}).get("decision") == "REQUIRE_CONFIRMATION"


def test_checkpoint_rollback_absolute() -> None:
    class FakeBackend:
        def current_level(self):
            return "L"

        def __init__(self):
            self.state = {"A": [1.0, 2.0, 3.0]}

        def set_actor_location(self, label, location):
            loc = list(location)[:3]
            self.state[label] = [float(loc[0]), float(loc[1]), float(loc[2])]

        def get_actor_transform(self, label):
            class T:
                pass
            t = T()
            t.location = tuple(self.state[label])
            return t

    backend = FakeBackend()
    backend.state["A"] = [10.0, 20.0, 30.0]
    tmp = Path(tempfile.mkdtemp())
    store = CheckpointStore(tmp)
    cp = TaskCheckpoint(
        task_id="task-x", created_at=0.0, level="L", execution_mode="AUTO",
        plan={}, actors=[ActorSnapshot(label="A", location=[10.0, 20.0, 30.0])],
    )
    store.save(cp)
    backend.state["A"] = [99.0, 99.0, 99.0]
    out = rollback_task(backend, store, "task-x")
    assert out["ok"] is True
    assert backend.state["A"] == [10.0, 20.0, 30.0]
    # idempotent re-apply
    out2 = rollback_task(backend, store, "task-x")
    assert backend.state["A"] == [10.0, 20.0, 30.0]
    assert out2["ok"] is True


def test_control_state_regression() -> None:
    c = SessionController()
    c.begin_task("t")
    c.pause()
    c.end_task(True)
    assert c.snapshot().phase == TaskPhase.PAUSED
    c.acknowledge_control()
    c.begin_task("t2")
    c.emergency_stop(reason="x")
    c.end_task(False)
    assert c.snapshot().phase == TaskPhase.ABORTED
    assert c.should_abort()


def test_batch_skill_registered() -> None:
    from src.skills.registry import SKILLS

    assert "batch_mutation" in SKILLS


def main() -> int:
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_phase4: {len(tests) - failed}/{len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
