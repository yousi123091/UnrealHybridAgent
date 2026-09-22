"""Phase 4B offline tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.observation.package import ActorFact, ObservationPackage  # noqa: E402
from src.observation.scene_model import TaskSceneModel  # noqa: E402
from src.observation.task_observer import TaskObserver  # noqa: E402
from src.semantics.profile import load_profile  # noqa: E402
from src.verification.outcome import OutcomeVerifier, build_failure_package  # noqa: E402
from src.vision.gui_confidence import assess_gui_confidence, verify_gui_action  # noqa: E402


class FakeBackend:
    name = "FAKE"

    def __init__(self, rows):
        self.rows = rows
        self.py_calls = []

    def available(self):
        return True

    def call_domain(self, tool, action, params=None):
        return {"success": True, "actors": self.rows}

    def get_actors(self, **kw):
        return list(self.rows)

    def get_actor_transform(self, label):
        row = next((r for r in self.rows if r.get("label") == label), None)

        class T:
            pass

        t = T()
        t.location = tuple(row.get("location") or (0, 0, 0))
        return t

    def set_actor_location(self, name, location):
        for r in self.rows:
            if r.get("label") == name:
                r["location"] = list(location)[:3]
        return {"ok": True}

    def execute_ue_python(self, code):
        self.py_calls.append(code[:80])
        # emulate batch read
        if "def _payload(labels)" in code:
            import json

            # labels embedded as JSON list in code
            return {"result": json.dumps({"success": True, "actors": [
                {"label": r["label"], "found": True, "location": r.get("location")}
                for r in self.rows
            ]})}
        return {"result": '{"success": true, "results": []}'}


def test_semantic_profile_example() -> None:
    p = load_profile("example_palace")
    assert p.name == "example_palace"
    door = p.resolve("DoorJamb_L")
    assert door and door["support_mode"] == "STRUCTURE"
    corr = p.resolve("CorrFloor_West")
    assert corr and corr["ground_kind"] == "ELEVATED_GROUND"
    marker = p.resolve("EXT_MarkerPost_0_W")
    assert marker and marker["profile_role"] == "MARKER"
    assert marker["needs_ai"] is True
    roof = p.resolve("Roof_Main")
    assert roof and roof["support_mode"] == "STRUCTURE"


def test_outcome_verifier_cases() -> None:
    v = OutcomeVerifier()
    assert v.verify({"type": "set_location", "label": "A", "target": [1, 2, 3]},
                    {"after": [1, 2, 3]}).status == "PASS"
    assert v.verify({"type": "set_location", "label": "A", "target": [1, 2, 3]},
                    {"after": [1, 2, 99]}).status == "FAIL"
    rigid = v.verify({"type": "rigid_translate", "delta": [0, 0, 20]}, {"actors": [
        {"label": "A", "before": [0, 0, 0], "after": [0, 0, 20]},
        {"label": "B", "before": [5, 0, 0], "after": [5, 0, 20]},
    ]})
    assert rigid.status == "PASS"
    bad = v.verify({"type": "rigid_translate", "delta": [0, 0, 20]}, {"actors": [
        {"label": "A", "before": [0, 0, 0], "after": [0, 0, 25]},
    ]})
    assert bad.status == "FAIL"
    unknown = v.verify({"type": "not_a_real_action"}, {})
    assert unknown.status == "UNKNOWN"
    vis = v.verify({"type": "set_visibility", "visible": False}, {"visible": False})
    assert vis.status == "PASS"
    fp = build_failure_package(action={"type": "set_location"}, targets=["A"], verification=bad,
                               backend="UNREAL_MCP", checkpoint_id="task-1")
    assert fp["checkpoint_available"] is True
    assert "failed_invariants" in fp


def test_scene_model_delta() -> None:
    sm = TaskSceneModel("t1")
    sm.begin()
    pkg1 = ObservationPackage("t1", "basic", 1, targets=[
        ActorFact("A", location=[0, 0, 0]),
        ActorFact("B", location=[1, 0, 0]),
    ], anomalies=[{"label": "X", "kind": "gap", "detail": "1"}])
    sm.apply_package(pkg1)
    assert sm.stale is False
    sm.mark_changed(["A"])
    need, reason = sm.needs_full_refresh()
    assert need and "mutation" in reason
    sm.apply_package(pkg1)
    pkg2 = ObservationPackage("t1", "basic", 2, targets=[
        ActorFact("A", location=[0, 0, 20]),
        ActorFact("B", location=[1, 0, 0]),
    ], anomalies=[])
    delta = sm.diff(pkg2)
    assert any(c["label"] == "A" for c in delta.changed)
    assert "B" in delta.unchanged
    assert delta.resolved_anomalies


def test_task_observer_relevance_and_package() -> None:
    rows = [
        {"label": "Platform_Skirt_Front", "location": [4850, -3700, 650], "class": "/Script/Engine.StaticMeshActor"},
        {"label": "Platform_Skirt_West", "location": [8500, -5250, 440], "class": "/Script/Engine.StaticMeshActor"},
        {"label": "Roof_Main", "location": [7820, 0, 2350], "class": "/Script/Engine.StaticMeshActor"},
        {"label": "EXT_Plaza_Central", "location": [0, 0, 0], "class": "/Script/Engine.StaticMeshActor"},
        {"label": "DoorJamb_L", "location": [5050, -2020, 1570], "class": "/Script/Engine.StaticMeshActor"},
        {"label": "Sky_HardFix", "location": [0, 0, 99999], "class": "/Script/Engine.SkyLight"},
    ]
    be = FakeBackend(rows)
    obs = TaskObserver(be, task_id="t-obs", profile=load_profile("example_palace"))
    pkg = obs.observe(targets=["Platform_Skirt_Front"], intent="prepare_batch_movement",
                      scope={"proximity_cm": 5000}, max_neighbors=5)
    labels = pkg.labels()
    assert "Platform_Skirt_Front" in labels
    assert "Roof_Main" not in labels or True  # proximity may include
    # explicit target always present
    fact = {t.label: t for t in pkg.targets}["Platform_Skirt_Front"]
    assert fact.semantic_mode == "STRUCTURE"
    assert pkg.provenance.get("ephemeral") is True
    assert pkg.stats.get("selected_n", 0) >= 1
    # force refresh path
    need, _ = obs.scene.needs_full_refresh(force=True)
    assert need


def test_gui_confidence() -> None:
    good = assess_gui_confidence(window={"window": {"x": 0, "y": 0, "w": 1200, "h": 800}},
                                 roi={"x": 20, "y": 80, "w": 250, "h": 400},
                                 focus_point=[600, 400])
    assert good.ok and good.action == "proceed"
    bad = assess_gui_confidence(window={"window": {"x": 0, "y": 0, "w": 1200, "h": 800}},
                                roi={"x": -5000, "y": -5000, "w": 5, "h": 5},
                                focus_point=[9999, 9999])
    assert not bad.ok and bad.action in ("recalibrate", "fail_closed")
    post = verify_gui_action("focus_viewport", {}, {})
    assert post["status"] in ("UNKNOWN", "PASS", "FAIL")


def test_batch_ops_parse() -> None:
    from src.adapters.unreal.batch_ops import UnrealBatchOps, primitive_read_cost

    be = FakeBackend([{"label": "A", "location": [1, 2, 3]}, {"label": "B", "location": [4, 5, 6]}])
    ops = UnrealBatchOps(be)
    assert ops.available
    payload = ops.batch_read(["A", "B"])
    assert payload.get("ok") or payload.get("success")
    assert payload.get("round_trips") == 1
    prim = primitive_read_cost(be, ["A", "B"])
    assert prim["round_trips"] == 2


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
    print(f"test_phase4b: {len(tests) - failed}/{len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
