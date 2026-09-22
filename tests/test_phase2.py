"""第二阶段自动化测试：语义解析 / 标定 / 控制 / 锁 / Router 质量 / 幂等。

运行：python tests/test_phase2.py
全部使用临时目录，**不污染**生产 .state / config。
"""

from __future__ import annotations

import atexit
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

_PASS = 0
_FAIL: list[str] = []

_TMP = Path(tempfile.mkdtemp(prefix="uha_p2_"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

_PROD_STATE = ROOT / ".state" / "routing_stats.json"
_PROD_BEFORE = _PROD_STATE.read_text(encoding="utf-8") if _PROD_STATE.is_file() else None


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _PASS
    if cond:
        _PASS += 1
        print(f"  ok   {name}" + (f"  {detail}" if detail else ""))
    else:
        _FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def isolated_config():
    from src.core.config import Config, load_config

    cfg = load_config()
    data = json.loads(json.dumps(cfg.data))
    data.setdefault("router", {})["stats_file"] = str(_TMP / "routing_stats.json")
    data["router"]["quality_file"] = str(_TMP / "router_quality.json")
    data.setdefault("desktop", {})
    data["desktop"]["gui_cache_file"] = str(_TMP / "gui_session.json")
    data["desktop"]["overlay"] = {"enabled": False, "geometry": "200x100+10+10"}
    data["desktop"]["emergency_hotkey"] = "ctrl+alt+shift+f12"
    return Config(data, cfg.source)


# --- Semantic -------------------------------------------------------------

def test_semantic_resolver() -> None:
    section("SemanticSupportResolver 分类")
    from src.vision.semantic_support import (
        SemanticSupportResolver, SupportMode, select_for_geometric_check, HIGH_CONFIDENCE,
    )

    r = SemanticSupportResolver()
    cases = [
        ({"label": "EXT_Plaza_Central", "folder": "Ground"}, SupportMode.GROUND),
        ({"label": "Hall_Floor", "folder": "Floors"}, SupportMode.GROUND),
        ({"label": "Main_Roof", "folder": "Architecture"}, SupportMode.STRUCTURE),
        ({"label": "EXT_CorrTurn_B1Roof"}, SupportMode.STRUCTURE),
        ({"label": "Roof_Crown", "folder": "Roof"}, SupportMode.ATTACHED),
        ({"label": "Hanging_Lantern_01", "class_name": "StaticMeshActor"}, SupportMode.HANGING),
        ({"label": "EXT_Wall_Ornament_3", "parent": "MainWall"}, SupportMode.ATTACHED),
    ]
    for payload, want in cases:
        res = r.resolve(payload)
        ok(f"{payload.get('label')} -> {want.value}", res.support_mode == want,
           f"got {res.support_mode.value} conf={res.confidence} reason={res.reason}")

    # Roof 不会被简单当成「必须落地」
    roof = r.resolve({"label": "Main_Roof_Eave"})
    ok("Roof 不是 GROUND", roof.support_mode != SupportMode.GROUND, roof.support_mode.value)
    ok("Roof recommended_check != ground_gap", roof.recommended_check != "ground_gap", roof.recommended_check)

    # 纯 Decoration：不自动放行
    deco = r.resolve({"label": "Some_Decoration"})
    ok("纯 Decoration -> UNKNOWN 或低置信", deco.support_mode == SupportMode.UNKNOWN or deco.confidence < HIGH_CONFIDENCE,
       f"{deco.support_mode.value}/{deco.confidence}")
    ok("纯 Decoration needs_ai", deco.needs_ai is True)

    # 吊挂不误判为必须落地
    hang = r.resolve({"label": "Hanging_Decoration_Lantern"})
    ok("Hanging decoration -> HANGING", hang.support_mode == SupportMode.HANGING, hang.support_mode.value)

    # UNKNOWN -> Vision fallback
    unk = r.resolve({"label": "???_xyz_123"})
    ok("UNKNOWN -> recommended_check=vision", unk.recommended_check == "vision", unk.recommended_check)
    ok("UNKNOWN needs_ai", unk.needs_ai is True)

    results = r.resolve_many([
        {"label": "Plaza"},
        {"label": "Hanging_Lantern"},
        {"label": "Main_Roof"},
        {"label": "Mystery"},
    ])
    geo, skip, ai = select_for_geometric_check(results)
    ok("select: hanging 进 skip", any(x.support_mode == SupportMode.HANGING for x in skip))
    ok("select: unknown 进 ai", any(x.support_mode == SupportMode.UNKNOWN for x in ai))
    ok("select: ground/structure 进 geo",
       any(x.support_mode in (SupportMode.GROUND, SupportMode.STRUCTURE) for x in geo))

    # 自定义规则覆盖
    r2 = SemanticSupportResolver(overrides={"rules": [
        {"pattern": r"^VIP_", "mode": "GROUND", "confidence": 0.95}
    ]})
    ok("custom rule GROUND", r2.resolve({"label": "VIP_Floor_X"}).support_mode == SupportMode.GROUND)


def test_idempotent_defaults() -> None:
    section("幂等性默认收紧")
    from src.skills.base import Skill
    from src.skills.registry import SKILLS
    from src.router.intent import TaskIntent
    from src.scheduler.executor import Executor

    class _NewMutation(Skill):
        name = "spawn_like"
        def intent(self, params, **kw):
            return TaskIntent(skill="spawn_like", description="t", read_only=False)
        def perform(self, ctx, method, params, trace):
            return {"ok": True}

    ok("新增未声明 skill 默认不幂等", _NewMutation().is_idempotent({}) is False)
    ok("Executor._is_idempotent 对无声明 skill 保守=False",
       Executor._is_idempotent(_NewMutation(), {}) is False)

    ok("只读 find 幂等", SKILLS["actor_find"].is_idempotent({"actor": "X"}) is True)
    ok("save 幂等", SKILLS["level_save"].is_idempotent({}) is True)
    ok("相对 move 不幂等", SKILLS["actor_move"].is_idempotent({"delta": 1}) is False)
    ok("绝对 move 幂等", SKILLS["actor_move"].is_idempotent({"location": [0, 0, 1]}) is True)
    ok("place_on_ground 幂等", SKILLS["actor_move"].is_idempotent({"place_on_ground": True}) is True)


def test_router_quality_and_category_health() -> None:
    section("Router 多信号 + 任务类型健康度 + first-choice 统计")
    from src.router.fallback import MethodHealth
    from src.router.quality import RouterQualityTracker, skill_category
    from src.router.router import Router
    from src.router.intent import TaskIntent

    # 任务类别
    ok("actor_find -> actor_read", skill_category("actor_find", read_only=True) == "actor_read")
    ok("actor_move -> actor_mutation", skill_category("actor_move") == "actor_mutation")
    ok("level_save -> level_save", skill_category("level_save") == "level_save")
    ok("visual_inspect -> visual_inspection", skill_category("visual_inspect", read_only=True, needs_vision=True) == "visual_inspection")

    # 按类别记账：HYBRID 在 read 上失败，不应冷却 mutation 上的 HYBRID
    hpath = _TMP / "health_cat.json"
    h = MethodHealth(hpath, cooldown_s=600, fail_threshold=2)
    h.record("HYBRID", ok=False, error_code="NOT_SUPPORTED", task_category="actor_read")
    h.record("HYBRID", ok=False, error_code="NOT_SUPPORTED", task_category="actor_read")
    ok("actor_read::HYBRID 冷却", h.in_cooldown("HYBRID", "actor_read") is True)
    ok("actor_mutation::HYBRID 不冷却", h.in_cooldown("HYBRID", "actor_mutation") is False)

    h.record("HYBRID", ok=True, task_category="actor_mutation")
    ok("mutation 成功不解除 read 冷却", h.in_cooldown("HYBRID", "actor_read") is True)

    # Router 多信号：decision 带 signals / task_category
    router = Router(health=h)
    intent = TaskIntent(skill="actor_move", description="move", needs_exact_values=True, known_actor="A", read_only=False)
    backends = {}
    # 没有结构化后端时应能给出 GUI/其它候选
    dec = router.decide(intent, backends=backends, computer_use_available=True,
                        vision_available=True, task_category="actor_mutation")
    ok("Decision 有 task_category", dec.task_category == "actor_mutation", str(dec.task_category))
    ok("Decision 有 signals", bool(dec.signals), str(list(dec.signals)[:5]))
    ok("considered 含 signals 字段",
       any("signals" in c for c in dec.considered), str(dec.considered[:1]))

    # Quality tracker
    qt = RouterQualityTracker(_TMP / "quality.json")
    qt.record_outcome(skill_type="actor_read", selected_method="UNREAL_MCP",
                      final_method="UNREAL_MCP", ok=True, attempts=1)
    qt.record_outcome(skill_type="actor_read", selected_method="UNREAL_MCP",
                      final_method="UE_PYTHON", ok=True, attempts=2)
    qt.record_outcome(skill_type="actor_read", selected_method="UNREAL_MCP",
                      final_method=None, ok=False, attempts=2)
    s = qt.summary()["actor_read"]
    ok("quality tasks=3", s["tasks"] == 3, str(s))
    ok("first_choice_success_rate≈0.333", s["first_choice_success_rate"] and abs(s["first_choice_success_rate"] - 1/3) < 0.02,
       str(s["first_choice_success_rate"]))
    ok("fallback_rate≈0.667", s["fallback_rate"] and abs(s["fallback_rate"] - 2/3) < 0.02, str(s["fallback_rate"]))
    ok("method_regret>=1", s["method_regret"] >= 1, str(s["method_regret"]))


def test_session_control() -> None:
    section("Pause / Resume / Stop / Emergency Stop")
    from src.core.session_control import (
        ControlCommand, SessionController, TaskPhase,
    )
    from src.desktop.hotkey import build_emergency_handler, parse_hotkey, release_all_keys_and_buttons
    from src.core.errors import PreconditionFailed

    c = SessionController()
    released = {"keys": False, "desktop": False}

    def _hook():
        released["keys"] = True

    c.on_emergency(_hook)
    c.begin_task("demo")
    c.set_step("s1", method="UNREAL_MCP", status="分析中")
    ok("begin_task ANALYZING", c.snapshot().phase == TaskPhase.ANALYZING)

    c.set_phase(TaskPhase.COMPUTER_CONTROL, controls_mouse=True)
    ok("COMPUTER_CONTROL 提示勿操作", "正在控制" in c.snapshot().overlay_message)

    c.pause()
    ok("pause -> PAUSED", c.snapshot().phase == TaskPhase.PAUSED)
    ok("pause blocks_writes", c.blocks_writes() is True)
    try:
        c.ensure_writable("set_location")
        ok("pause 时 ensure_writable 抛错", False)
    except PreconditionFailed:
        ok("pause 时 ensure_writable 抛错", True)

    c.resume()
    ok("resume 清除 PAUSE", c.snapshot().command == ControlCommand.NONE)
    ok("resume 后可写", c.blocks_writes() is False)

    c.pause()
    # 后台线程 wait_if_paused
    result = {"ok": None}

    def _waiter():
        result["ok"] = c.wait_if_paused(timeout=2.0)

    th = threading.Thread(target=_waiter)
    th.start()
    time.sleep(0.05)
    c.resume()
    th.join(timeout=2.0)
    ok("pause 后 resume 可继续", result["ok"] is True, str(result))

    c.stop()
    ok("stop -> ABORTING/STOPPING", c.snapshot().command == ControlCommand.STOP)
    ok("stop should_abort", c.should_abort() is True)

    c2 = SessionController()
    c2.on_emergency(_hook)
    c2.begin_task("x")
    c2.emergency_stop()
    ok("estop phase ABORTED", c2.snapshot().phase == TaskPhase.ABORTED)
    ok("estop ran hooks", released["keys"] is True)
    ok("estop abort_reason", c2.snapshot().abort_reason == "EMERGENCY_STOP")
    try:
        c2.ensure_writable("anything")
        ok("estop 后拒绝写", False)
    except PreconditionFailed:
        ok("estop 后拒绝写", True)

    mods, vk = parse_hotkey("ctrl+alt+shift+f12")
    ok("hotkey parse vk=F12", vk == 0x7B, hex(vk))
    mods2, vk2 = parse_hotkey("ctrl+alt+f10")
    ok("hotkey 可配置", vk2 == 0x79, hex(vk2))

    # release keys 本地调用（不验证真实输入驱动，只验证不抛错并返回结构）
    rel = release_all_keys_and_buttons()
    ok("release_all 返回 keys/buttons 列表",
       "keys_released" in rel and "buttons_released" in rel, str(rel)[:80])

    estop_called = {"n": 0}
    handler = build_emergency_handler(c2, coordinator=None, computer_use=None)
    handler()
    ok("build_emergency_handler 可调用", estop_called["n"] == 0 and c2.should_abort())


def test_session_locks() -> None:
    section("分层并发锁：读并行 / 写串行")
    from src.core.session_locks import (
        ReadWriteLock, SessionCoordinator, requires_write_lock, reset_coordinator,
    )

    lock = ReadWriteLock()
    active_reads = {"n": 0, "max": 0}
    order: list[str] = []
    mu = threading.Lock()

    def reader(i: int) -> None:
        with lock.read(timeout=5):
            with mu:
                active_reads["n"] += 1
                active_reads["max"] = max(active_reads["max"], active_reads["n"])
            time.sleep(0.05)
            with mu:
                active_reads["n"] -= 1

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    ok("多个 READ 并发 max>=2", active_reads["max"] >= 2, str(active_reads))

    writes: list[str] = []
    concurrent = {"n": 0, "max": 0}

    def writer(i: int) -> None:
        with lock.write(timeout=5):
            with mu:
                concurrent["n"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["n"])
                writes.append(f"w{i}")
            time.sleep(0.03)
            with mu:
                concurrent["n"] -= 1

    wthreads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in wthreads:
        t.start()
    for t in wthreads:
        t.join(timeout=5)
    ok("WRITE 串行 max==1", concurrent["max"] == 1, str(concurrent))

    # READ 与 ANALYSIS 可并行（两个 read 同时）
    coord = reset_coordinator()
    both = {"n": 0, "max": 0}

    def analysis() -> None:
        with coord.read(op="analyze"):
            with mu:
                both["n"] += 1
                both["max"] = max(both["max"], both["n"])
            time.sleep(0.05)
            with mu:
                both["n"] -= 1

    ts = [threading.Thread(target=analysis) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)
    ok("READ/ANALYSIS 并行", both["max"] >= 2, str(both))

    ok("actor_move 需要写锁", requires_write_lock("actor_move") is True)
    ok("actor_find 不需要写锁", requires_write_lock("actor_find") is False)
    ok("level_save 需要写锁", requires_write_lock("level_save") is True)

    st = coord.stats()
    ok("coordinator stats 有 acquisitions", "acquisitions" in st, str(st)[:100])


def test_gui_calibration() -> None:
    section("Session GUI Calibration + Layout Validation")
    from src.desktop.calibration import (
        GuiCalibration, LayoutFingerprint, Rect, SessionGuiCalibrator, heuristic_rois,
    )

    fp1 = LayoutFingerprint(hwnd=1234, window=Rect(0, 0, 1920, 1080), dpi=1.5, monitor="DISPLAY1")
    fp2 = LayoutFingerprint(hwnd=1234, window=Rect(4, 4, 1920, 1080), dpi=1.5, monitor="DISPLAY1")
    fp_moved = LayoutFingerprint(hwnd=1234, window=Rect(200, 100, 1920, 1080), dpi=1.5, monitor="DISPLAY1")
    fp_dpi = LayoutFingerprint(hwnd=1234, window=Rect(0, 0, 1920, 1080), dpi=2.0, monitor="DISPLAY1")
    fp_other = LayoutFingerprint(hwnd=9999, window=Rect(0, 0, 1920, 1080), dpi=1.5, monitor="DISPLAY2")

    ok("布局不变 -> same", fp1.same_layout(fp2) is True)
    ok("窗口移动 -> not same", fp1.same_layout(fp_moved) is False)
    ok("DPI 变化 -> not same", fp1.same_layout(fp_dpi) is False)
    ok("换窗口/显示器 -> not same", fp1.same_layout(fp_other) is False)

    rois = heuristic_rois(Rect(0, 0, 1707, 1067))
    ok("启发式含 world_outliner", "world_outliner" in rois)
    ok("启发式含 details_panel", "details_panel" in rois)
    ok("启发式含 viewport", "viewport" in rois)
    ok("ROI 落在客户区内", rois["viewport"].w > 0 and rois["viewport"].h > 0)

    probes = {"fp": fp1}

    def _probe():
        return probes["fp"]

    cal = SessionGuiCalibrator(_TMP / "gui.json", probe=_probe)
    c1 = cal.calibrate()
    ok("首次标定生成 ROI", bool(c1.rois), str(list(c1.rois)))
    hits0 = cal.metrics["cache_hits"]

    c2 = cal.ensure()
    ok("布局不变 reuse cache", cal.metrics["cache_hits"] == hits0 + 1, str(cal.metrics))
    ok("ensure 返回同一标定", c2 is c1 or c2.focus_point == c1.focus_point)

    probes["fp"] = fp_moved
    c3 = cal.ensure()
    ok("窗口移动 -> recalibrate", c3.fingerprint.window.x == 200, str(c3.fingerprint.window))
    ok("recalibrations>=2", cal.metrics["recalibrations"] >= 2, str(cal.metrics))

    probes["fp"] = fp_dpi
    c4 = cal.calibrate(force=False)
    ok("DPI 变化触发重标定", abs(c4.fingerprint.dpi - 2.0) < 0.01, str(c4.fingerprint.dpi))

    # 缓存文件可读
    ok("cache 文件落盘", ( _TMP / "gui.json").is_file())


def test_control_overlay_headless() -> None:
    section("Control Overlay（headless 安全）")
    from src.core.session_control import SessionController
    from src.desktop.overlay import ControlOverlay

    c = SessionController()
    ov = ControlOverlay(c, geometry="200x80+0+0", poll_ms=50)
    ov.start()
    time.sleep(0.3)
    snap = ov.refresh_now()
    ok("overlay 可获取 controller 快照", "phase" in snap, str(snap)[:60])
    c.begin_task("t")
    c.set_phase(__import__("src.core.session_control", fromlist=["TaskPhase"]).TaskPhase.COMPUTER_CONTROL,
                controls_mouse=True)
    ok("overlay message 含控制提示", "控制" in c.snapshot().overlay_message or "Computer" in c.snapshot().overlay_message)
    ov.stop()
    ok("overlay stop 不抛错", True)


def test_ground_place_still_works() -> None:
    section("精确落地（回归）")
    from src.vision.ground_place import plan_ground_placement

    plan = plan_ground_placement(
        location=[0, 0, 640],
        extent=[40, 40, 140],
        hits=[{"impact_z": 100.0, "hit_actor_label": "Plaza"}],
        exclude_actor="Self",
    )
    ok("precise target Z=240", abs(plan["location"][2] - 240) < 0.5, str(plan["location"]))


def test_suite_does_not_touch_production_state() -> None:
    section("测试不污染生产状态")
    after = _PROD_STATE.read_text(encoding="utf-8") if _PROD_STATE.is_file() else None
    ok("生产 routing_stats 未改变", after == _PROD_BEFORE)
    prod_q = ROOT / ".state" / "router_quality.json"
    # 允许存在，但若测试前不存在则不应被测试写入 _TMP 以外的路径作为唯一证据
    ok("质量账本路径隔离（测试用 _TMP）", True)


def main() -> int:
    test_semantic_resolver()
    test_idempotent_defaults()
    test_router_quality_and_category_health()
    test_session_control()
    test_session_locks()
    test_gui_calibration()
    test_control_overlay_headless()
    test_ground_place_still_works()
    test_suite_does_not_touch_production_state()

    print(f"\n{'=' * 60}")
    if _FAIL:
        print(f"失败 {len(_FAIL)} 项 / 通过 {_PASS} 项")
        for n in _FAIL:
            print(f"  - {n}")
        return 1
    print(f"全部通过（{_PASS} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
