"""离线回归测试：不需要 UE、不需要网络、不需要 Computer Use。

覆盖三块最容易悄悄写错、又最影响正确性的逻辑：

    1. 第三方 MCP 的参数映射（GenOrca 用 actor_label，不是 actor_name）
    2. 浮空判定的几何（中位数地面 + 包围盒底边，不是中心点）
    3. 验证器的"通过/不通过/没做"三种结论不能互相冒充

运行：``python tests/test_offline.py`` 或 ``pytest tests/test_offline.py``
"""

from __future__ import annotations

import atexit
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

_PASS = 0
_FAIL: list[str] = []

# --- 测试必须与生产状态隔离 ---------------------------------------------------
#
# `.state/routing_stats.json` 是**生产状态**（跨任务的通道健康度）。测试里造一次
# 失败，如果记进了这个文件，下一个真实任务的最优通道就会带着 60s 冷却出场，
# 并且会一路劣化到"没有任何可用的执行方式"。真实踩过一次，复盘见
# docs/FINDINGS.md#f5。所以：测试内的 Executor 一律用隔离配置，并加一条
# 收尾断言盯着生产文件没被动过。
_TEST_STATE = Path(tempfile.mkdtemp(prefix="uha_test_state_"))
atexit.register(shutil.rmtree, _TEST_STATE, ignore_errors=True)

_PROD_STATE = ROOT / ".state" / "routing_stats.json"


def _snapshot(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


_PROD_STATE_BEFORE = _snapshot(_PROD_STATE)


class _StubLogger:
    """什么都不做的 logger：测试里只关心"记了什么账"，不关心怎么打印。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.events: list[tuple[str, dict]] = []

    def warn(self, msg: str, *a, **kw) -> None:  # noqa: D102
        self.warnings.append(str(msg))

    def error(self, msg: str, *a, **kw) -> None:  # noqa: D102
        self.warnings.append(str(msg))

    def info(self, msg: str, *a, **kw) -> None:  # noqa: D102
        pass

    def event(self, name: str, **kw) -> None:  # noqa: D102
        self.events.append((name, kw))


def isolated_config():
    """与生产配置内容一致、但状态文件指向临时目录的 Config。"""
    from src.core.config import Config, load_config

    cfg = load_config()
    data = json.loads(json.dumps(cfg.data))  # 深拷贝：绝不改动全局缓存的那一份
    data.setdefault("router", {})["stats_file"] = str(_TEST_STATE / "routing_stats.json")
    data["router"]["quality_file"] = str(_TEST_STATE / "router_quality.json")
    data.setdefault("desktop", {})
    data["desktop"]["gui_cache_file"] = str(_TEST_STATE / "gui_session.json")
    data["desktop"]["overlay"] = {"enabled": False}
    return Config(data, cfg.source)


def test_suite_does_not_touch_production_state() -> None:
    """收尾断言：跑完这一整套测试，生产健康度文件必须一字未改。"""
    section("测试不污染生产状态（.state/routing_stats.json）")
    after = _snapshot(_PROD_STATE)
    ok(
        "离线测试没有改动生产健康度文件",
        after == _PROD_STATE_BEFORE,
        f"before={_PROD_STATE_BEFORE!r} after={after!r}",
    )


def ok(label: str, cond: bool, hint: str = "") -> None:
    global _PASS
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL.append(label)
        print(f"  FAIL {label} {hint}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --- 1. MCP 参数映射 ----------------------------------------------------------


def test_domain_mapping() -> None:
    section("第三方 MCP 参数映射（GenOrca domains 契约）")
    from src.adapters.unreal.unreal_mcp import DOMAIN_MAP, _flatten_domain_args

    ok("set_location -> actor/set_location", DOMAIN_MAP["set_actor_location"] == ("actor", "set_location"))
    ok("save_level -> level/save_current_level",
       DOMAIN_MAP["save_level"] == ("level", "save_current_level"))
    ok("get_actors -> actor/get_all_details", DOMAIN_MAP["get_actors"] == ("actor", "get_all_details"))
    ok("没有 find 动作，find_actor 复用 get_transform",
       DOMAIN_MAP["find_actor"] == ("actor", "get_transform"))
    ok("viewport_screenshot -> vision/capture_viewport",
       DOMAIN_MAP["viewport_screenshot"] == ("vision", "capture_viewport"))

    a = _flatten_domain_args("set_actor_location", {"name": "Hall_Floor", "location": (1, 2, 3)})
    ok("位置参数键是 actor_label", "actor_label" in a, str(a))
    ok("位置参数值正确", a == {"actor_label": "Hall_Floor", "location": [1, 2, 3]}, str(a))
    ok("绝不出现 actor_name", "actor_name" not in a, str(a))

    r = _flatten_domain_args("set_actor_rotation", {"name": "A", "rotation": (10, 20, 30)})
    ok("旋转参数键是 rotation", r == {"actor_label": "A", "rotation": [10, 20, 30]}, str(r))

    s = _flatten_domain_args("set_actor_scale", {"name": "A", "scale": (2, 2, 2)})
    ok("缩放参数键是 scale", s == {"actor_label": "A", "scale": [2.0, 2.0, 2.0]}, str(s))

    e = _flatten_domain_args("execute_ue_python", {"code": "print(1)"})
    ok("execute_python 只传 code", e == {"code": "print(1)"}, str(e))

    g = _flatten_domain_args("get_actors", {"name_like": "Hall", "limit": 10})
    ok("get_all_details 不接受参数（过滤在客户端做）", g == {}, str(g))


def test_actor_ref_from_real_payload() -> None:
    section("ActorRef 解析真实 get_all_details 载荷")
    from src.adapters.base import ActorRef

    payload = {
        "label": "Hall_Floor",
        "class": "/Script/Engine.StaticMeshActor",
        "location": [7800.0, 0.0, 840.0],
        "rotation": [0.0, 0.0, 0.0],
        "world_bounds_origin": [7800.0, 0.0, 900.0],
        "world_bounds_extent": [4200.0, 4800.0, 60.0],
    }
    ref = ActorRef.from_payload(payload)
    ok("label 落到 name", ref.name == "Hall_Floor", ref.name)
    ok("class 落到 class_name", ref.class_name == payload["class"], ref.class_name)
    ok("location 解析正确", ref.location == (7800.0, 0.0, 840.0), str(ref.location))
    ok("z 快捷属性可用", ref.z == 840.0, str(ref.z))

    flat = ActorRef.from_payload({"actor_label": "X", "location_x": 1, "location_y": 2, "location_z": 3})
    ok("平铺坐标也认", flat.location == (1.0, 2.0, 3.0), str(flat.location))

    # 回归：get_transform 的行里只有 actor_label，name 也必须落到 "X"
    only_label = ActorRef.from_payload({"actor_label": "X", "location": [1, 2, 3]})
    ok("只有 actor_label 时 name 不为空", only_label.name == "X", only_label.name)

    # 回归：包围盒在 extra 里，as_dict() 默认丢掉会让浮空判断退化到"中心点近似"
    with_bounds = ActorRef.from_payload({
        "label": "HoverRock", "class": "/Script/Engine.StaticMeshActor",
        "location": [0.0, 0.0, 500.0],
        "world_bounds_origin": [0.0, 0.0, 500.0],
        "world_bounds_extent": [50.0, 50.0, 50.0],
    })
    ok("默认 as_dict 不含包围盒", "world_bounds_origin" not in with_bounds.as_dict())
    ok("include_extra 能取回包围盒",
       with_bounds.as_dict(include_extra=True).get("world_bounds_origin") == [0.0, 0.0, 500.0])

    from src.vision.judge import Observation
    o = Observation.from_payload(with_bounds.as_dict(include_extra=True))
    ok("Observable 拿到底边（=450，离地 450）", o.bottom_z == 450.0, str(o.bottom_z))


# --- 1b. 真实响应形态（回归：曾把 get_transform 误判成"查不到"）-------------------


def test_extract_rows_real_shapes() -> None:
    section("响应形态抽取（回归）")
    from src.adapters.unreal.unreal_mcp import _extract_rows

    # get_all_details：包在 actors 里
    many = _extract_rows({"success": True, "actors": [{"label": "A"}, {"label": "B"}]})
    ok("get_all_details 抽出列表", len(many) == 2, str(many))

    # get_transform：顶层本身就是一行（这是曾经报"未返回 transform"的根因）
    one = _extract_rows({
        "success": True, "actor_label": "Hall_Floor",
        "location": [7800.0, 0.0, 840.0], "rotation": [0, 0, 0], "scale": [1, 1, 1],
        "ok": True, "_transport": "unreal_mcp:domains",
    })
    ok("顶层单行能抽出来", len(one) == 1 and one[0].get("actor_label") == "Hall_Floor", str(one))

    # 只回 message 的写操作不应被误认成一行
    none = _extract_rows({"success": True, "message": "Actor 'X' transform updated for: location."})
    ok("纯 message 不当成行", none == [], str(none))


# --- 2. 浮空判定 --------------------------------------------------------------


def test_floating_judge() -> None:
    section("浮空判定（几何，可复现）")
    from src.vision.judge import Observation, judge_floating, judge_by_center, summarise

    def obs(label: str, bottom: float, height: float = 100.0, cls: str = "/Script/Engine.StaticMeshActor"):
        return Observation(
            label=label,
            location=(0.0, 0.0, bottom + height / 2),
            bounds_origin=(0.0, 0.0, bottom + height / 2),
            bounds_extent=(50.0, 50.0, height / 2),
            class_name=cls,
        )

    rows = [obs("ground_a", 0.0), obs("ground_b", 0.0), obs("ground_c", 0.0),
            obs("wall", 0.0), obs("floater", 300.0)]
    cands = judge_floating(rows, ground_tolerance=5.0)
    labels = [c.observation.label for c in cands]
    ok("只挑出浮空的那个", labels == ["floater"], str(labels))
    ok("离地高度算对", abs(cands[0].gap - 300.0) < 1e-6, str(cands[0].gap))
    ok("地面取低分位数", abs(cands[0].ground_z - 0.0) < 1e-6, str(cands[0].ground_z))

    # 整个场景被抬高：不应误判
    raised = [obs("a", 500.0), obs("b", 500.0), obs("c", 500.0)]
    ok("整体抬高不误报", judge_floating(raised, ground_tolerance=5.0) == [], "should be empty")

    # 灯/相机不参与
    lamp = rows + [obs("SpotLight_1", 900.0, cls="/Script/Engine.SpotLight")]
    ok("灯光被排除", all(c.observation.label != "SpotLight_1" for c in judge_floating(lamp)))

    # 没有 bounds 的观测不参与（而不是拿中心点硬猜）
    no_bounds = [Observation(label="x", location=(0, 0, 900))]
    ok("无包围盒不参与判定", judge_floating(no_bounds) == [])

    # 退路：中心点近似要明确标注 basis
    center = judge_by_center([Observation(label="a", location=(0, 0, 0)),
                              Observation(label="b", location=(0, 0, 0)),
                              Observation(label="c", location=(0, 0, 500))])
    ok("中心点退路有标注", center and center[0].basis == "center_fallback", str(center))

    s = summarise(cands)
    ok("汇总含 floating_count", s["floating_count"] == 1, str(s))
    # 分母未知时按 fail-closed 处理：算不准可信度就拒绝自动修复
    ok("分母未知 -> reliable=False（fail-closed）", s["reliable"] is False, str(s["reliable"]))
    ok("分母未知时标明原因", s.get("reliability_basis") == "unknown_denominator", str(s.get("reliability_basis")))
    s_known = summarise(cands, scanned=len(rows), usable=len(rows), basis="bounds_bottom")
    ok("给出分母后少量浮空 -> 判定可信", s_known["reliable"] is True, str(s_known))


def test_floating_architecture_regression() -> None:
    """回归：示例宫殿级场景（大量高处构件）里，地面估计不能被"高处占多数"带偏。

    实测数据：1184 个 Actor，用中位数（856cm）当地面 -> 569 个构件被判浮空
    （整栋楼的屋顶、斗拱、梁全在名单里）。
    """
    section("浮空判定：建筑场景回归 + 可信度闸门")
    from src.vision.judge import summarise, judge_floating, Observation

    def obs(label: str, bottom: float, height: float = 200.0):
        return Observation(
            label=label,
            location=(0.0, 0.0, bottom + height / 2),
            bounds_origin=(0.0, 0.0, bottom + height / 2),
            bounds_extent=(50.0, 50.0, height / 2),
            class_name="/Script/Engine.StaticMeshActor",
        )

    # 少数落地构件 + 大量屋顶/梁（在 800~3000cm 高处）
    ground = [obs(f"ground_{i}", 0.0) for i in range(80)]
    roofs = [obs(f"roof_{i}", 800.0 + i) for i in range(400)]
    scene = ground + roofs

    cands = judge_floating(scene, ground_tolerance=5.0)
    summary = summarise(cands, scanned=len(scene), usable=len(scene), basis="bounds_bottom")
    ok("地面估计没被高处构件带偏（≈0，不是 856）",
       abs((summary["ground_z"] or -1) - 0.0) < 5.0, str(summary["ground_z"]))
    ok("屋顶确实被判为高于地面（这是事实，不是误判）",
       summary["floating_count"] == len(roofs), str(summary["floating_count"]))
    ok("占比过高 -> reliable=False（拒绝自动修复）",
       summary["reliable"] is False, str(summary["reliable"]))
    ok("不可信时给出 warning", bool(summary.get("warning")), str(summary.get("warning")))

    # 真正的"少量异常"场景应当是可信的
    healthy = ground + [obs("one_stray", 300.0)]
    good = summarise(judge_floating(healthy), scanned=len(healthy), usable=len(healthy))
    ok("少量浮空 -> reliable=True", good["reliable"] is True, str(good))
    ok("少量浮空的 ground_z 仍贴近地面",
       abs((good["ground_z"] or -1) - 0.0) < 5.0, str(good["ground_z"]))


# --- 3. 验证器 ----------------------------------------------------------------


def test_verifiers() -> None:
    section("验证器：通过 / 不通过 / 没做 三态")
    from src.adapters.base import ActorRef
    from src.validation.result import CheckResult, VerifyReport
    from src.validation.verifiers import (
        check_axis_delta, check_dirty_empty, check_floating_resolved,
        check_save_result, check_transform_equals, check_unchanged,
    )

    after = ActorRef(name="A", label="A", location=(1.0, 2.0, 840.0 + 20.0))
    checks = check_transform_equals(after, expected_location=(1.0, 2.0, 860.0), pos_tol=0.5)
    ok("坐标一致 -> 通过", all(c.passed for c in checks), str([c.as_dict() for c in checks]))

    bad = check_transform_equals(after, expected_location=(1.0, 2.0, 900.0), pos_tol=0.5)
    ok("坐标不符 -> 不通过", not bad[0].passed)
    ok("不通过不算 skipped", not bad[0].skipped)

    d = check_axis_delta((1, 2, 840), (1, 2, 860), axis=2, delta=20.0, tol=0.5)
    ok("Z +20 判定通过", d.passed, d.line())
    d2 = check_axis_delta((1, 2, 840), (1, 2, 840), axis=2, delta=20.0, tol=0.5)
    ok("没动 -> 不通过", not d2.passed, d2.line())

    u = check_unchanged((1, 2, 840), (1.0001, 2.0, 900), axes=(0, 1), tol=0.5)
    ok("XY 未变 -> 通过", u.passed, u.line())

    ok("保存 API 返回 success -> 通过", check_save_result({"success": True}).passed)
    ok("保存返回 false -> 不通过", not check_save_result({"success": False}).passed)
    ok("保存没返回 -> skipped", check_save_result(None).skipped)

    ok("脏包 0 -> 通过", check_dirty_empty({"dirty_count": 0}).passed)
    ok("脏包 3 -> 不通过", not check_dirty_empty({"dirty_count": 3}).passed)
    ok("查不到脏包 -> skipped", check_dirty_empty(None).skipped)

    ok("浮空已消除 -> 通过", check_floating_resolved([], target_label="floater").passed)
    ok("浮空仍在 -> 不通过",
       not check_floating_resolved([{"label": "floater"}], target_label="floater").passed)

    # 报告语义
    r = VerifyReport().add(CheckResult("x", passed=False, skipped=True, reason="没证据"))
    ok("全跳过 -> inconclusive", r.inconclusive and not r.passed, r.summary())
    r2 = VerifyReport().add(CheckResult("x", passed=True))
    ok("有通过项 -> passed", r2.passed and not r2.inconclusive)

    # 回归：曾经因为 [check_transform_equals(...)] 多套一层列表，导致
    # report.passed 里炸 'list' object has no attribute 'skipped'
    nested = VerifyReport().extend([check_transform_equals(after, expected_location=(1.0, 2.0, 860.0))])
    ok("嵌套列表被拉平", all(isinstance(c, CheckResult) for c in nested.checks), str(nested.checks))
    ok("拉平后结论可用", nested.passed and not nested.inconclusive, nested.summary())

    r3 = VerifyReport().add(CheckResult("x", passed=True)).add(CheckResult("y", passed=False))
    ok("含失败项 -> 不通过", not r3.passed)


def test_verifiers_accept_dict() -> None:
    section("验证器接受 dict 形态（回归：曾经静默变 skipped）")
    from src.validation.result import CheckResult
    from src.validation.verifiers import check_dirty_empty, check_file_written, check_transform_equals

    # demp1 的真实根因：after 是 ActorRef.as_dict() 的字典，
    # 旧实现用 getattr 读 location -> 读不到 -> 检查被跳过
    as_dict = {"name": "Hall_Floor", "label": "Hall_Floor", "location": [7800.0, 0.0, 880.0],
               "rotation": [0.0, 0.0, 0.0], "scale": [54.0, 90.0, 0.4]}
    checks = check_transform_equals(as_dict, expected_location=[7800.0, 0.0, 880.0], label="Hall_Floor")
    ok("dict 形态不再跳过", all(not c.skipped for c in checks), str([c.as_dict() for c in checks]))
    ok("dict 形态能判定通过", all(c.passed for c in checks))

    bad = check_transform_equals(as_dict, expected_location=[7800.0, 0.0, 999.0], label="Hall_Floor")
    ok("dict 形态能判定不通过", not bad[0].passed, bad[0].line())

    # 空证据闸门：保存前后脏包都是 0 -> 必须 skipped，不能算通过
    vac = check_dirty_empty({"dirty_count": 0}, before_count=0)
    ok("脏包 0→0 判为 skipped（不假绿）", vac.skipped and not vac.passed, vac.line())
    real = check_dirty_empty({"dirty_count": 0}, before_count=3)
    ok("脏包 3→0 才判通过", real.passed and not real.skipped, real.line())

    # 保存的外部证据：.umap mtime 是否推进
    f = check_file_written({"path": "a.umap", "mtime": 100.0}, {"path": "a.umap", "mtime": 101.0})
    ok("mtime 推进 -> 保存落盘", f.passed, f.line())
    f2 = check_file_written({"path": "a.umap", "mtime": 100.0}, {"path": "a.umap", "mtime": 100.0})
    ok("mtime 未变 -> 判定没落盘", not f2.passed and not f2.skipped, f2.line())
    f3 = check_file_written(None, {"path": "a.umap", "mtime": 1.0})
    ok("缺基线 -> skipped", f3.skipped, f3.line())


def test_fallback_counts_failures_not_successes() -> None:
    section("降级计数（回归：成功不能吃掉重试预算）")
    from src.router.fallback import FallbackManager

    fm = FallbackManager(max_attempts_per_method=2, max_total_fallbacks=5)
    fm.record("UNREAL_MCP", ok=True)
    fm.record("UNREAL_MCP", ok=True)
    ok("连成功 2 次仍可再试", fm.can_try("UNREAL_MCP"), str(fm.state.as_dict()))
    ok("成功不计入 exhausted", "UNREAL_MCP" not in fm.state.exhausted)

    fm.record("MOUSE", ok=False, error_code="NOT_SUPPORTED")
    ok("结构性失败立刻判死", not fm.can_try("MOUSE"))

    fm.record("VISION", ok=False, error_code="TRANSPORT_ERROR")
    ok("普通失败 1 次还留机会", fm.can_try("VISION"))
    fm.record("VISION", ok=False, error_code="TRANSPORT_ERROR")
    ok("普通失败 2 次才判死", not fm.can_try("VISION"))

    fm.reset()
    ok("reset 后状态清空", fm.can_try("VISION") and fm.state.total_fallbacks == 0) 
    ok("reset 后 attempts/failures 归零",
       not fm.state.attempts and not fm.state.failures, str(fm.state.as_dict()))


# --- 4. 占位符与计划 ----------------------------------------------------------


def test_plan_and_placeholders() -> None:
    section("计划与占位符")
    from src.core.errors import PreconditionFailed
    from src.planner import Planner
    from src.scheduler.executor import _resolve_placeholders

    plan = Planner().plan_raise_and_save("Hall_Floor", 20.0)
    ok("demo1 计划三步", len(plan.steps) == 3, str([s.skill for s in plan.steps]))
    ok("顺序是 find -> move -> save",
       [s.skill for s in plan.steps] == ["actor_find", "actor_move", "level_save"])
    ok("move 的 delta 正确", plan.steps[1].params["delta"] == 20.0)

    plan2 = Planner().plan_floating_repair(drop_by=50.0)
    ok("demo2 含占位符", plan2.steps[1].params["actor"] == "@floating_top")

    shared = {"floating_top": "SM_Column_Front_0_Cap"}
    got = _resolve_placeholders({"actor": "@floating_top", "delta": -50.0, "x": None}, shared)
    ok("占位符解析成功", got == {"actor": "SM_Column_Front_0_Cap", "delta": -50.0}, str(got))

    try:
        _resolve_placeholders({"actor": "@nope"}, shared)
        ok("未知占位符应当报错", False, "没有抛异常")
    except PreconditionFailed:
        ok("未知占位符报错（不静默用 0 代替）", True)


def test_floating_reliability_guard() -> None:
    """不可信的浮空结论**不能**驱动自动修改——宁可这一步失败得清清楚楚。"""
    section("浮空结论可信度闸门（防「整栋楼被搬走」）")
    from src.core.errors import PreconditionFailed
    from src.scheduler.executor import Executor, _resolve_placeholders
    from src.skills.base import SkillResult

    def result(reliable: bool, count: int, candidates: list[dict]) -> SkillResult:
        floating = {
            "floating_count": count,
            "candidates": candidates,
            "reliable": reliable,
            "usable_actors": 100,
            "flagged_ratio": 0.4 if not reliable else 0.01,
            "ground_z": 0.0,
        }
        if not reliable:
            floating["warning"] = "占比过高，地面估计偏了"
        return SkillResult(
            skill="visual_inspect", ok=True, method="UNREAL_MCP",
            data={"ok": True, "artifacts": {"ue_viewport": "x.png"},
                  "evidence": {"floating": floating}},
        )

    # --- 不可信：数据照摊，但占位符上闸 ---
    shared: dict = {}
    Executor._absorb(shared, "visual_inspect", result(
        False, 569, [{"label": "SM_Roof_0", "gap": 3200.0}]))
    ok("不可信结论仍进 shared 供人看", "floating" in shared)
    ok("不可信时不发布 floating_top", "floating_top" not in shared, str(sorted(shared)))
    ok("不可信时登记 blocked_placeholders",
       "floating_top" in (shared.get("blocked_placeholders") or {}), str(shared))
    ok("不可信时标 floating_unreliable", shared.get("floating_unreliable") is True)

    try:
        _resolve_placeholders({"actor": "@floating_top", "delta": -50.0}, shared)
        ok("不可信占位符必须被拦下", False, "居然解析成功了")
    except PreconditionFailed as exc:
        ok("不可信占位符被拦下（拒绝自动修复）", True)
        ok("拦下时给出原因", "占比过高" in str(exc), str(exc))

    # --- 可信：正常发布 ---
    ok_shared: dict = {}
    Executor._absorb(ok_shared, "visual_inspect", result(
        True, 1, [{"label": "SM_Stray_0", "gap": 300.0, "ground_z": 0.0}]))
    ok("可信时发布 floating_top", ok_shared.get("floating_top") == "SM_Stray_0", str(ok_shared))
    ok("可信时没有 block", not (ok_shared.get("blocked_placeholders") or {}))
    got = _resolve_placeholders({"actor": "@floating_top", "delta": -50.0}, ok_shared)
    ok("可信时占位符正常解析", got == {"actor": "SM_Stray_0", "delta": -50.0}, str(got))


def test_explicit_ground_detector() -> None:
    """受控检测：显式地面参照 + 限定区域。这是在多层场景里唯一站得住的做法。"""
    section("受控检测（显式地面参照 + 限定区域）")
    from src.vision.judge import (
        Observation, find_reference, judge_against_ground, rects_overlap, summarise, within_region,
    )

    def plate(label, ox, oy, top, bottom, ex=5000.0, ey=5000.0):
        """一块地面：给定顶面与底面，推出 origin/extent。"""
        c = (top + bottom) / 2.0
        return Observation(
            label=label, location=(ox, oy, c),
            bounds_origin=(ox, oy, c), bounds_extent=(ex, ey, (top - bottom) / 2.0),
            class_name="/Script/Engine.StaticMeshActor",
        )

    def prop(label, x, y, bottom, height=100.0, ex=50.0):
        return Observation(
            label=label, location=(x, y, bottom + height / 2),
            bounds_origin=(x, y, bottom + height / 2),
            bounds_extent=(ex, ex, height / 2),
            class_name="/Script/Engine.StaticMeshActor",
        )

    # 广场顶面 = 0；广场上三个道具贴地，一个被抬到 400
    scene = [
        plate("EXT_Plaza", 0.0, 0.0, top=0.0, bottom=-200.0),
        prop("SM_Prop_A", 1000.0, 0.0, 0.0),
        prop("SM_Prop_B", 1200.0, 200.0, 0.0),
        prop("SM_Stray", 1100.0, 100.0, 400.0),     # 真的悬空 400cm
        prop("SM_Far", 20000.0, 20000.0, 0.0),      # 区域外
    ]

    ref = find_reference(scene, "EXT_Plaza")
    ok("找得到地面参照物", ref is not None and ref.label == "EXT_Plaza")
    ok("参照物顶面取对（=0）", abs(ref.top_z - 0.0) < 1e-6, str(ref.top_z))
    ok("参照物带包围盒范围", ref.footprint == (-5000.0, 5000.0, -5000.0, 5000.0), str(ref.footprint))
    ok("找不到的参照物返回 None（不猜）", find_reference(scene, "NoSuchFloor") is None)
    ok("空 label 返回 None", find_reference(scene, "") is None)
    ok("没有包围盒的参照物返回 None",
       find_reference([Observation(label="F", location=(0, 0, 0))], "F") is None)

    region = (-2000.0, 2000.0, -2000.0, 2000.0)
    ok("区域内判定", within_region(prop("p", 0.0, 0.0, 0.0), region) is True)
    ok("区域外判定", within_region(prop("p", 9999.0, 0.0, 0.0), region) is False)

    cands = judge_against_ground(scene, ground_z=ref.top_z, region=region,
                                 exclude_labels=("EXT_Plaza",))
    labels = [c.observation.label for c in cands]
    ok("只挑出真正悬空的那个", labels == ["SM_Stray"], str(labels))
    ok("gap 算对", abs(cands[0].gap - 400.0) < 1e-6, str(cands[0].gap))
    ok("标注为显式地面", cands[0].basis == "explicit_ground", cands[0].basis)
    ok("贴地的道具不误报", "SM_Prop_A" not in labels and "SM_Prop_B" not in labels, str(labels))

    # 不给区域时，区域外的 SM_Far 也是贴地的 -> 依然不误报
    allc = judge_against_ground(scene, ground_z=ref.top_z, exclude_labels=("EXT_Plaza",))
    ok("不限定区域时贴地件仍不误报", [c.observation.label for c in allc] == ["SM_Stray"],
       str([c.observation.label for c in allc]))

    # 显式地面 -> 可执行；统计地面 -> 不可执行
    in_region = [o for o in scene if within_region(o, region)]
    ok_verdict = summarise(cands, scanned=len(scene), usable=len(in_region),
                           basis="explicit_ground", ground_reference=ref.as_dict())
    ok("显式地面 -> reliable", ok_verdict["reliable"] is True, str(ok_verdict["reliable"]))
    ok("显式地面 -> actionable", ok_verdict["actionable"] is True, str(ok_verdict["actionable"]))
    ok("显式地面 -> 无警告", not ok_verdict.get("warning"), str(ok_verdict.get("warning")))
    ok("结果里带上参照物", ok_verdict["ground_reference"]["label"] == "EXT_Plaza")

    stat = summarise(cands, scanned=len(scene), usable=len(scene), unguided=True)
    ok("统计地面 -> reliable 仍可能为真", stat["reliable"] is True, str(stat["reliable"]))
    ok("统计地面 -> actionable 必须为假", stat["actionable"] is False, str(stat["actionable"]))
    ok("统计地面 -> 挂 unguided 警告",
       "统计推断" in (stat.get("warning") or ""), str(stat.get("warning")))

    # --- 回归：受控模式（有 region）不能被"占比上限"自己打死 ---
    # 实测撞出来的 bug：小区域里 3 个构件、1 个真悬空 = 33% > 25%，
    # 于是检测器明明找对了，却被自己的安全闸拦下不许修。
    small = [
        plate("EXT_Plaza", 0.0, 0.0, top=0.0, bottom=-200.0),
        prop("P1", 0.0, 0.0, 0.0),
        prop("P3", 800.0, 0.0, 400.0),          # 真悬空 400
    ]
    r2 = find_reference(small, "EXT_Plaza")
    reg2 = (-2000.0, 2000.0, -2000.0, 2000.0)
    c2 = judge_against_ground(small, ground_z=r2.top_z, region=reg2, exclude_labels=("EXT_Plaza",))
    ok("小区域里确实判出 1 个", len(c2) == 1, str([x.observation.label for x in c2]))
    scope2 = [o for o in small if within_region(o, reg2)]
    ok("分母是 3（区域内有 3 个构件）", len(scope2) == 3, str(len(scope2)))
    v_scoped = summarise(c2, scanned=len(small), usable=len(scope2),
                         basis="explicit_ground", ground_reference=r2.as_dict(),
                         enforce_ratio=False,
                         coverage_ok=rects_overlap(r2.footprint, reg2))
    ok("占比确实超过 25%（1/3）", v_scoped["flagged_ratio"] > 0.25, str(v_scoped["flagged_ratio"]))
    ok("受控+区域 -> 占比高也不否决 reliable", v_scoped["reliable"] is True, str(v_scoped["reliable"]))
    ok("受控+区域 -> actionable", v_scoped["actionable"] is True, str(v_scoped["actionable"]))
    ok("受控+区域 -> ratio_enforced=False", v_scoped["ratio_enforced"] is False)
    ok("受控+区域 -> 仍有提示（不否决）",
       "偏高" in (v_scoped.get("warning") or ""), str(v_scoped.get("warning")))

    # 全场景 + 显式地面：占比门槛保留（那时高占比确实更像"参照物选错了"）
    v_all = summarise(c2, scanned=len(small), usable=len(small),
                      basis="explicit_ground", enforce_ratio=True)
    ok("全场景+显式地面 -> 占比门槛仍然生效", v_all["actionable"] is False, str(v_all["actionable"]))
    ok("全场景+显式地面 -> 说明是地面估错",
       "地面高度估错" in (v_all.get("warning") or ""), str(v_all.get("warning")))

    # 覆盖率：区域跑到参照物范围之外 -> 不可执行
    ok("矩形重叠判定", rects_overlap((-10, 10, -10, 10), (-5, 5, -5, 5)) is True)
    ok("矩形不重叠判定", rects_overlap((-10, -5, -10, 10), (0, 10, 0, 10)) is False)
    ok("仅边界相切不算重叠", rects_overlap((-10, 0, -10, 10), (0, 10, 0, 10)) is False)
    v_cov = summarise(c2, scanned=len(small), usable=len(scope2),
                      basis="explicit_ground", enforce_ratio=False, coverage_ok=False)
    ok("区域超出参照物范围 -> reliable=False", v_cov["reliable"] is False, str(v_cov["reliable"]))
    ok("区域超出参照物范围 -> 有解释",
       "超出" in (v_cov.get("warning") or ""), str(v_cov.get("warning")))
    ok("区域超出参照物范围 -> coverage_ok=False",
       v_cov["coverage_ok"] is False, str(v_cov["coverage_ok"]))


def test_floating_verdict_paths() -> None:
    """把 skill 的真实代码路径跑一遍（假后端）。

    回归对象：曾经在 skill 里把 `judge_against_ground` 的
    `exclude_labels` 写成了 `ignore_labels`，只有在真机上才会 TypeError。
    """
    section("visual_inspect 判定路径（含显式地面 / 统计地面 / 参照物缺失）")
    from src.core.errors import PreconditionFailed
    from src.skills.base import SkillTrace
    from src.skills.visual_inspect import VisualInspectSkill

    class _FakeBackend:
        def __init__(self, rows): self._rows = rows
        def get_actors(self, **_kw): return [dict(r) for r in self._rows]

    class _FakeBundle:
        def __init__(self, rows): self._b = _FakeBackend(rows)
        def unreal_for(self, _method): return self._b

    class _FakeCtx:
        def __init__(self, rows):
            self.bundle = _FakeBundle(rows)
        def structured_or_none(self, method):
            return self.bundle.unreal_for(method)

    def row(label, x, y, top, bottom, ex=50.0, ey=50.0):
        c = (top + bottom) / 2.0
        return {
            "label": label, "class": "/Script/Engine.StaticMeshActor",
            "location": [x, y, c],
            "world_bounds_origin": [x, y, c],
            "world_bounds_extent": [ex, ey, (top - bottom) / 2.0],
        }

    rows = [
        row("EXT_Plaza", 0.0, 0.0, top=0.0, bottom=-200.0, ex=5000.0, ey=5000.0),
        row("Post_A", 1000.0, 0.0, top=100.0, bottom=0.0),
        row("Post_B", -1000.0, 0.0, top=100.0, bottom=0.0),
        row("Post_Stray", 2000.0, 0.0, top=500.0, bottom=400.0),
        row("Hall_Floor_Panel_Stray", 2500.0, 0.0, top=500.0, bottom=400.0),
    ]
    skill = VisualInspectSkill()
    ctx = _FakeCtx(rows)
    trace = SkillTrace()

    # 1) 显式地面 + 语义优先：GROUND 浮空可进候选；STRUCTURE/Post 不按 ground 砸地
    v = skill._floating_verdict(
        ctx, {"ground_ref": "EXT_Plaza", "region": [-3000, 3000, -3000, 3000], "use_semantic": True}, trace)
    ok("显式地面路径能跑通", v is not None and v.get("basis") == "explicit_ground", str(v))
    labels = [c["label"] for c in v["candidates"]]
    ok("GROUND 浮空面板被标出",
       "Hall_Floor_Panel_Stray" in labels, str(labels))
    ok("STRUCTURE/Post 不被当成需砸地的 ground 浮空",
       "Post_Stray" not in labels and "Post_A" not in labels, str(labels))
    ok("参照物被排除（不会把自己当悬空物）",
       "EXT_Plaza" not in labels, str(labels))
    ok("结果带 ground_reference", v["ground_reference"]["label"] == "EXT_Plaza")
    # 兼容旧语义关闭路径（显式对照）
    v_old = skill._floating_verdict(
        ctx, {"ground_ref": "EXT_Plaza", "region": [-3000, 3000, -3000, 3000], "use_semantic": False}, SkillTrace())
    ok("关闭语义时仍可跑几何路径", v_old is not None and v_old.get("basis") == "explicit_ground", str(v_old)[:200])

    # 2) 参照物不存在 -> 明确中止，不猜地面
    try:
        skill._floating_verdict(ctx, {"ground_ref": "NoSuchFloor"}, SkillTrace())
        ok("缺失参照物必须报错", False, "居然没抛异常")
    except PreconditionFailed as exc:
        ok("缺失参照物报 PreconditionFailed（不猜地面）", True)
        ok("报错里点出参照物名字", "NoSuchFloor" in str(exc), str(exc))

    # 3) 不给参照物 -> 统计地面，结论不可执行
    v2 = skill._floating_verdict(ctx, {}, SkillTrace())
    ok("统计地面路径能跑通", v2 is not None, str(v2))
    ok("统计地面 -> unguided=True", v2.get("unguided") is True, str(v2.get("unguided")))
    ok("统计地面 -> actionable=False", v2["actionable"] is False, str(v2["actionable"]))
    ok("统计地面 -> 有 warning", bool(v2.get("warning")), str(v2.get("warning")))

    # 4) 复验：expect_no_floating 在"已无浮空"时应通过
    rows_clean = [r for r in rows if r["label"] not in ("Post_Stray", "Hall_Floor_Panel_Stray")]
    clean = skill._floating_verdict(
        _FakeCtx(rows_clean), {"ground_ref": "EXT_Plaza", "region": [-3000, 3000, -3000, 3000]},
        SkillTrace())
    ok("清理后浮空数归零", clean["floating_count"] == 0, str(clean["floating_count"]))
    ok("清理后仍可执行", clean["actionable"] is True, str(clean["actionable"]))


def test_text_planner() -> None:
    section("自然语言解析（不假装全懂）")
    from src.planner import parse_actor, parse_axis, parse_delta, plan_from_text

    ok("解析 Z 轴", parse_axis("把 Z 轴抬高 20") == "z")
    ok("解析 X 轴", parse_axis("沿 X 方向移动 5") == "x")
    d, _u = parse_delta("把「Hall_Floor」抬高 20 厘米")
    ok("解析 +20cm", abs(d - 20.0) < 1e-9, str(d))
    d2, _u2 = parse_delta("降低 3 米")
    ok("米换算成厘米且取负", abs(d2 + 300.0) < 1e-9, str(d2))
    ok("引号里的名字能解析", parse_actor("把「Hall_Floor」抬高 20") == "Hall_Floor")

    plan = plan_from_text("把「Hall_Floor」抬高 20，然后保存")
    skills = [s.skill for s in plan.steps]
    ok("识别出 定位->移动->保存",
       skills == ["actor_find", "actor_move", "level_save"], str(skills))
    ok("计划带 warnings（提示这是推断）", bool(plan.meta.get("warnings")))

    # 缺 Actor 名时必须报错，不能猜一个对象去改
    try:
        plan_from_text("抬高 20")
        ok("缺 Actor 名应当报错", False, "没有抛异常")
    except ValueError:
        ok("缺 Actor 名报错（不猜对象）", True)


def test_unexpected_error_does_not_poison_health() -> None:
    """回归：本地代码缺陷不能被记成"通道不健康"。

    真实踩过：skill 里把 `exclude_labels` 写成 `ignore_labels`，一个 TypeError
    就让 UNREAL_MCP 连续失败 =2 被冷却隔离，之后连只读巡检都选不出任何方式。
    一个代码 bug 不该有能力"封杀"一条通道。
    """
    section("本地代码缺陷不污染通道健康度")
    import time as _time
    from src.scheduler.executor import Executor

    class _Stub:
        def __init__(self): self.calls = []
        def record(self, *a, **kw): self.calls.append((a, kw))
        def error(self, *a, **kw): self.calls.append((a, kw))

    class _StubRouter:
        def __init__(self): self.calls = []
        def feedback(self, *a, **kw): self.calls.append((a, kw))

    cfg = isolated_config()
    ex = Executor(bundle=None, config=cfg, logger=_Stub())  # type: ignore[arg-type]
    ex.health = _Stub()
    ex.router = _StubRouter()
    ex.fallback = _Stub()

    ex._record_failure("UNREAL_MCP", "UNEXPECTED", "TypeError: boom", _time.perf_counter())
    ok("UNEXPECTED 不进 health", ex.health.calls == [], str(ex.health.calls))
    ok("UNEXPECTED 不进 router", ex.router.calls == [], str(ex.router.calls))
    ok("UNEXPECTED 不进 fallback 预算", ex.fallback.calls == [], str(ex.fallback.calls))

    ex._record_failure("UNREAL_MCP", "TOOL_CALL_FAILED", "网络抖了一下", _time.perf_counter())
    ok("真实失败仍然进 health", len(ex.health.calls) == 1, str(ex.health.calls))
    ok("真实失败仍然进 router", len(ex.router.calls) == 1, str(ex.router.calls))
    ok("真实失败仍然进 fallback", len(ex.fallback.calls) == 1, str(ex.fallback.calls))


def test_not_supported_does_not_poison_health() -> None:
    """回归：**任务与通道不匹配**（NOT_SUPPORTED）也不能算"通道不健康"。

    真实踩过（docs/FINDINGS.md#f5）：只读巡检碰上"UNREAL_MCP 冷却中"，只能退到
    HYBRID/KEYBOARD/VISION/MOUSE，而这些通道本来就不做只读查询，于是全部
    NOT_SUPPORTED —— 被记进健康度之后，它们也带上了冷却，下一步连
    "没有任何可用的执行方式"都报出来了。不匹配是结构性的，重跑一百次也是不匹配。
    """
    section("任务与通道不匹配不污染通道健康度")
    import time as _time
    from src.scheduler.executor import Executor

    class _Stub:
        def __init__(self): self.calls = []
        def record(self, *a, **kw): self.calls.append((a, kw))
        def error(self, *a, **kw): self.calls.append((a, kw))

    class _StubRouter:
        def __init__(self): self.calls = []
        def feedback(self, *a, **kw): self.calls.append((a, kw))

    ex = Executor(bundle=None, config=isolated_config(), logger=_StubLogger())  # type: ignore[arg-type]
    ex.health = _Stub()
    ex.router = _StubRouter()
    ex.fallback = _Stub()

    ex._record_failure("HYBRID", "NOT_SUPPORTED", "hybrid 不做只读查询", _time.perf_counter())
    ok("NOT_SUPPORTED 不进 health", ex.health.calls == [], str(ex.health.calls))
    ok("NOT_SUPPORTED 不进 router 成功率", ex.router.calls == [], str(ex.router.calls))
    ok("NOT_SUPPORTED 仍进本次任务的降级预算（避免原地重试）",
       len(ex.fallback.calls) == 1, str(ex.fallback.calls))
    ok("留下了一条可排查的 health_skip 记录",
       any(name == "health_skip" for name, _ in ex.logger.events),
       str([n for n, _ in ex.logger.events]))

    # 对照组：真失败照旧记账
    ex._record_failure("HYBRID", "TOOL_CALL_FAILED", "真的连不上", _time.perf_counter())
    ok("对照：真失败仍然进 health", len(ex.health.calls) == 1, str(ex.health.calls))


def test_health_record_is_self_consistent() -> None:
    """成功一次之后，"冷却"这个账必须一起销掉。

    否则会留下 `consecutive_failures=0` 却仍在冷却的自相矛盾记录——`uha stats`
    读起来全是误导，排查时会被带偏到"通道坏了"而不是"账没销"。
    """
    section("健康度记录自洽：成功即解除冷却")
    import tempfile
    from pathlib import Path as _P
    from src.router.fallback import MethodHealth

    with tempfile.TemporaryDirectory() as td:
        h = MethodHealth(_P(td) / "routing_stats.json", cooldown_s=600.0, fail_threshold=2)
        h.record("UNREAL_MCP", ok=False, error_code="X")
        h.record("UNREAL_MCP", ok=False, error_code="X")
        ok("两次连续失败 -> 冷却中", h.in_cooldown("UNREAL_MCP") is True)
        h.record("UNREAL_MCP", ok=True)
        ok("成功一次后不再冷却（前提'连续失败'已不成立）", h.in_cooldown("UNREAL_MCP") is False)
        ok("成功不抹掉历史计数（ok 仍是累计值）",
           h.stats()["UNREAL_MCP"]["ok"] == 1 and h.stats()["UNREAL_MCP"]["failed"] == 2,
           str(h.stats()["UNREAL_MCP"]))


def test_cooldown_is_surfaced_in_failure() -> None:
    """失败信息要能指出"真正的病因是通道在冷却"，而不是只报最后一棒的错误。

    真实踩过：UNREAL_MCP 冷却中，路由退到 GUI；最终报出来的却是
    "未标定 world_outliner 面板的 ROI"——一个和真正病因无关的报错，
    排查方向被完全带偏。
    """
    section("失败信息点出冷却中的通道")
    from src.scheduler.executor import Executor

    class _H:
        def __init__(self, cooling): self.cooling = set(cooling)
        def in_cooldown(self, m): return m in self.cooling

    ex = Executor(bundle=None, config=isolated_config(), logger=_StubLogger())  # type: ignore[arg-type]
    ex.health = _H({"UNREAL_MCP"})  # type: ignore[assignment]

    note = ex._cooldown_note({"UNREAL_MCP": False, "HYBRID": True})
    ok("点出了 UNREAL_MCP 在冷却", "UNREAL_MCP" in note, note)
    ok("没有冤枉不在冷却的通道", "HYBRID" not in note, note)
    ok("给出了销账入口", "uha stats --reset" in note, note)

    ex.health = _H(set())  # type: ignore[assignment]
    ok("没有通道在冷却时不加噪音", ex._cooldown_note({"UNREAL_MCP": True}) == "")


def test_method_health_clear() -> None:
    section("方法健康度：清账（解除冷却）")
    import tempfile
    from pathlib import Path as _P
    from src.router.fallback import MethodHealth

    with tempfile.TemporaryDirectory() as td:
        path = _P(td) / "state" / "routing_stats.json"
        h = MethodHealth(path, cooldown_s=600.0, fail_threshold=2)
        h.record("UNREAL_MCP", ok=False, error_code="X")
        h.record("UNREAL_MCP", ok=False, error_code="X")
        ok("连续失败到阈值 -> 冷却中", h.in_cooldown("UNREAL_MCP") is True)
        h.clear()
        ok("clear 后不再冷却", h.in_cooldown("UNREAL_MCP") is False)
        ok("clear 后统计为空", h.stats() == {}, str(h.stats()))
        ok("clear 落盘（文件仍存在且可读）", path.is_file())


def test_step_method_preference() -> None:
    """步骤级"建议方式"：只重排顺序，不删降级链。"""
    section("步骤级建议方式（prefer_method）")
    import time as _time
    from src.planner import Planner, PlanStep
    from src.scheduler.executor import Executor

    # 1) 计划层面：demo2 的搬运步骤应当建议 HYBRID
    plan = Planner().plan_floating_repair(drop_by=50.0, ground_ref="EXT_Plaza_Central")
    move = plan.steps[1]
    ok("搬运步骤建议 HYBRID", move.prefer_method == "HYBRID", str(move.prefer_method))
    ok("巡检步骤不指定方式（交给 Router）",
       plan.steps[0].prefer_method is None and plan.steps[2].prefer_method is None)
    ok("as_dict 带上 prefer_method", plan.as_dict()["steps"][1]["prefer_method"] == "HYBRID")
    ok("describe 里能看到建议方式", "建议方式：HYBRID" in plan.describe(), plan.describe())

    # 2) 执行器层面：prefer_method 只重排，不缩短候选列表
    class _StepRec:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set(self, **kw): pass
        def meta(self, **kw): pass

    class _Log:
        def __init__(self):
            self.events = []
            self.steps = []
        def event(self, kind, **kw): self.events.append((kind, kw))
        def info(self, *a, **kw): pass
        def warn(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def step(self, name, **kw):
            self.steps.append((name, kw))
            return _StepRec()

    class _Bundle:
        def gui_available(self): return False
        def structured_backends(self): return {}

    class _Router:
        def __init__(self, order): self._order = order
        def decide(self, intent, **kw):
            from src.router.router import Decision
            return Decision(method=self._order[0], reason="测试", rule="test",
                            alternatives=[{"method": m, "reason": "备选", "score": 1.0}
                                          for m in self._order[1:]])
        def available_methods(self, **kw):
            return {m: True for m in self._order}
        def feedback(self, *a, **kw): pass

    class _Skill:
        name = "fake"
        def intent(self, params, **kw):
            from src.router.intent import TaskIntent
            return TaskIntent(skill="fake", description="测试用")
        def perform(self, ctx, method, params, trace):
            trace.after["transform"] = {"location": [0.0, 0.0, 100.0]}
            return {"ok": True, "transport": method}
        def preflight(self, ctx, params): return {}
        def verify(self, ctx, method, params, trace):
            from src.validation.result import CheckResult, VerifyReport
            return VerifyReport().add(CheckResult("fake.ok", passed=True))

    def _run_with(prefer, order):
        log = _Log()
        ex = Executor(bundle=_Bundle(), config=isolated_config(), logger=log)  # type: ignore[arg-type]
        ex.router = _Router(order)
        ex.run(_Skill(), {}, prefer_method=prefer)
        return log

    # prefer_method 只重排，不缩短候选列表
    log = _run_with("HYBRID", ["UNREAL_MCP", "HYBRID", "UE_PYTHON"])
    pref = [kw for kind, kw in log.events if kind == "method_preference"]
    ok("发了 method_preference 事件", bool(pref), str([k for k, _ in log.events]))
    ok("HYBRID 被提到第一个", pref and pref[0].get("result") == "reordered", str(pref))
    ok("记录了 Router 本来会选谁",
       pref and pref[0].get("router_would_have_chosen") == "UNREAL_MCP", str(pref))
    ok("降级链没被删（仍是 3 条）",
       pref and len(pref[0].get("order") or []) == 3, str(pref[0].get("order")))
    # 被建议重排后，真正首选的那一步**不该**被日志写成"降级尝试"
    first_step_reason = (log.steps[0][1].get("reason") if log.steps else "") or ""
    ok("建议方式不被误记为降级尝试", "降级尝试" not in first_step_reason, first_step_reason)
    ok("建议方式的首步理由点明是步骤建议",
       "显式建议" in first_step_reason, first_step_reason)
    ok("没有发出 fallback 事件（首步就是首选）",
       not any(k == "fallback" for k, _ in log.events), str([k for k, _ in log.events]))

    # 建议的方式就是 Router 首选 -> 不折腾
    log_same = _run_with("HYBRID", ["HYBRID", "UNREAL_MCP"])
    same = [kw for kind, kw in log_same.events if kind == "method_preference"]
    ok("建议与首选一致 -> already_selected",
       same and same[0].get("result") == "already_selected", str(same))

    # 建议的方式不在候选里 -> 忽略并记账，不硬闯
    log_ign = _run_with("MOUSE", ["UNREAL_MCP", "UE_PYTHON"])
    ign = [kw for kind, kw in log_ign.events if kind == "method_preference"]
    ok("不可用的建议被忽略",
       ign and ign[0].get("result") == "ignored_not_available", str(ign))


def test_non_idempotent_retry_guard() -> None:
    """回归：不可幂等的动作（相对位移）**绝不能**因为一次可疑失败就被重做。

    真实事故：HYBRID 已经把构件从 640 移到 240（回读 3 项全过），
    但一条"整屏画面变化"弱检查判它失败 → 框架按降级链换 UNREAL_MCP 又做了一遍
    → 构件到了 -160（多下移 400，沉进地面）。而"沉进地面"不是浮空，
    最后一步复验反而通过 —— 计划报"全部成功"，场景已经坏了。
    """
    section("不可幂等动作的重试护栏（防重复生效）")
    from src.core.errors import ToolCallFailed
    from src.router.intent import TaskIntent
    from src.scheduler.executor import Executor
    from src.validation.result import CheckResult, VerifyReport

    class _StepRec:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set(self, **kw): pass
        def meta(self, **kw): pass

    class _Log:
        def __init__(self):
            self.events = []
            self.steps = []
        def event(self, kind, **kw): self.events.append((kind, kw))
        def info(self, *a, **kw): pass
        def warn(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def step(self, name, **kw):
            self.steps.append((name, kw))
            return _StepRec()

    class _Bundle:
        def gui_available(self): return False
        def structured_backends(self): return {}

    class _Router:
        def decide(self, intent, **kw):
            from src.router.router import Decision
            return Decision(method="UNREAL_MCP", reason="测试", rule="test",
                            alternatives=[{"method": "UE_PYTHON", "reason": "备选", "score": 1.0}])
        def available_methods(self, **kw):
            return {"UNREAL_MCP": True, "UE_PYTHON": True}
        def feedback(self, *a, **kw): pass

    class _Env:
        """模拟 UE 里的一个标量状态（例如某轴的 Z）。"""
        def __init__(self, z): self.z = z; self.writes = 0

    def make_skill(env, *, fail_after_write: bool, write_lands: bool):
        class _S:
            name = "fake_move"
            def intent(self, params, **kw):
                return TaskIntent(skill="fake_move", description="测试")
            def is_idempotent(self, params):
                return params.get("location") is not None
            def preflight(self, ctx, params):
                return {"transform": {"location": [0.0, 0.0, env.z]}}
            def perform(self, ctx, method, params, trace):
                if write_lands:
                    env.z += float(params["delta"])
                    env.writes += 1
                    trace.after["transform"] = {"location": [0.0, 0.0, env.z]}
                if fail_after_write:
                    raise ToolCallFailed("网络抖了一下，但写入可能已经落下")
                return {"ok": True}
            def verify(self, ctx, method, params, trace):
                want = env.z  # 期望 = 基线 + delta
                base = params["_base"]
                target = base + float(params["delta"])
                r = VerifyReport()
                r.add(CheckResult("transform.location", passed=abs(env.z - target) < 0.5,
                                  expected=[0, 0, target], actual=[0, 0, env.z]))
                return r
        return _S()

    def run_once(env, *, fail_after_write, write_lands, delta=-400.0, pin=None):
        log = _Log()
        ex = Executor(bundle=_Bundle(), config=isolated_config(), logger=log)  # type: ignore[arg-type]
        ex.router = _Router()
        skill = make_skill(env, fail_after_write=fail_after_write, write_lands=write_lands)
        res = ex.run(skill, {"delta": delta, "_base": env.z}, prefer_method=pin)
        return res, log

    # --- A. 写入已落下 + 抛错（歧义）-> 确认生效，判成功，**不再重试** ---
    env = _Env(640.0)
    res, _ = run_once(env, fail_after_write=True, write_lands=True)
    ok("A: 写入已落下 -> 判成功", res.ok is True, str(res.error))
    ok("A: 只写了 1 次（没有重复生效）", env.writes == 1, str(env.writes))
    ok("A: 状态恰好挪了 1 个 delta（-400）", abs(env.z - 240.0) < 1e-9, str(env.z))
    ok("A: 没有发生降级重试", len(res.attempts) == 1, str([a["method"] for a in res.attempts]))

    # --- B. 写入没落下 + 抛错 -> 真失败，允许降级；降级后总共也只生效一次 ---
    env = _Env(640.0)
    res, _ = run_once(env, fail_after_write=True, write_lands=False)
    ok("B: 真失败会降级（尝试 2 次）", len(res.attempts) == 2, str([a["method"] for a in res.attempts]))
    ok("B: 最终状态仍然是 640（没被多改）", abs(env.z - 640.0) < 1e-9, str(env.z))

    # B2：第二次尝试成功时，必须只挪一次 delta
    def make_second_succeeds(env):
        skill = make_skill(env, fail_after_write=True, write_lands=False)
        calls = {"n": 0}
        orig = skill.perform
        def perform(ctx, method, params, trace):
            calls["n"] += 1
            if calls["n"] >= 2:          # 第二次（降级后）真正写入
                env.z += float(params["delta"]); env.writes += 1
                trace.after["transform"] = {"location": [0.0, 0.0, env.z]}
                return {"ok": True}
            return orig(ctx, method, params, trace)
        skill.perform = perform
        return skill

    env = _Env(640.0)
    log = _Log()
    ex = Executor(bundle=_Bundle(), config=isolated_config(), logger=log)  # type: ignore[arg-type]
    ex.router = _Router()
    res = ex.run(make_second_succeeds(env), {"delta": -400.0, "_base": 640.0})
    ok("B2: 降级后成功", res.ok is True, str(res.error))
    ok("B2: 总共只写入 1 次", env.writes == 1, str(env.writes))
    ok("B2: 最终恰好 -400（不是 -800）", abs(env.z - 240.0) < 1e-9, str(env.z))

    # --- C. 幂等动作（绝对定位）不受影响：写入已落下 + 抛错 -> 确认后成功 ---
    env = _Env(640.0)

    class _AbsEnv:
        def __init__(self): self.z = 640.0
    class _AbsSkill:
        name = "fake_abs"
        def intent(self, params, **kw):
            return TaskIntent(skill="fake_abs", description="测试")
        def is_idempotent(self, params): return True          # 绝对定位
        def preflight(self, ctx, params): return {"transform": {"location": [0.0, 0.0, env.z]}}
        def perform(self, ctx, method, params, trace):
            env.z = float(params["location"][2])
            trace.after["transform"] = {"location": [0.0, 0.0, env.z]}
            raise ToolCallFailed("写入已落下但报错了")
        def verify(self, ctx, method, params, trace):
            r = VerifyReport()
            r.add(CheckResult("transform.location",
                              passed=abs(env.z - float(params["location"][2])) < 0.5,
                              expected=list(params["location"]), actual=[0, 0, env.z]))
            return r

    env = _Env(640.0)
    log = _Log()
    ex = Executor(bundle=_Bundle(), config=isolated_config(), logger=log)  # type: ignore[arg-type]
    ex.router = _Router()
    res = ex.run(_AbsSkill(), {"location": [0.0, 0.0, 100.0]})
    ok("C: 幂等动作抛错也会先确认再判失败", res.ok is True, str(res.error))
    ok("C: 最终落在目标值 100", abs(env.z - 100.0) < 1e-9, str(env.z))

    # --- D. skill 自己声明幂等性：相对位移 = 不幂等；绝对定位 = 幂等 ---
    from src.skills.actor_move import ActorMoveSkill
    mv = ActorMoveSkill()
    ok("D: actor_move 相对位移 -> 不幂等", mv.is_idempotent({"axis": "z", "delta": -400.0}) is False)
    ok("D: actor_move 绝对定位 -> 幂等", mv.is_idempotent({"location": [1, 2, 3]}) is True)


def test_health_not_double_counted() -> None:
    """回归：一次真实失败只应让 MethodHealth 连续失败 +1，而不是 +2。

    Executor 与 Router 共享同一个 MethodHealth。若 `health.record` 与
    `router.feedback→health.record` 都落账，阈值为 2 时**失败一次就会冷却**，
    把好通道提前隔离（F5 同类的隐蔽变体）。
    """
    section("健康度不双重记账（一次失败 = 连续失败+1）")
    import tempfile
    import time as _time
    from pathlib import Path as _P
    from src.router.fallback import MethodHealth
    from src.scheduler.executor import Executor

    cfg = isolated_config()
    with tempfile.TemporaryDirectory() as td:
        # 独立账本：避免套件内其它用例写共享 isolated 状态文件造成污染
        cfg.data.setdefault("router", {})["stats_file"] = str(_P(td) / "routing_stats.json")
        ex = Executor(bundle=None, config=cfg, logger=_StubLogger())  # type: ignore[arg-type]
        # 保持生产装配方式：Router 与 Executor 共享同一个 health
        assert ex.router.health is ex.health

        ex._record_failure("UNREAL_MCP", "TOOL_CALL_FAILED", "net blip", _time.perf_counter())
        slot = ex.health.stats()["UNREAL_MCP"]
        ok("一次真实失败 failed==1", slot["failed"] == 1, str(slot))
        ok("一次真实失败 consecutive_failures==1（不是 2）",
           slot["consecutive_failures"] == 1, str(slot))
        ok("阈值=2 时一次失败不应冷却", ex.health.in_cooldown("UNREAL_MCP") is False)

        ex._record_failure("UNREAL_MCP", "TOOL_CALL_FAILED", "net blip 2", _time.perf_counter())
        slot2 = ex.health.stats()["UNREAL_MCP"]
        ok("两次失败 consecutive_failures==2", slot2["consecutive_failures"] == 2, str(slot2))
        ok("两次失败才冷却", ex.health.in_cooldown("UNREAL_MCP") is True)

        ex.health.record("UNREAL_MCP", ok=True)
        ok("成功一次即解除冷却", ex.health.in_cooldown("UNREAL_MCP") is False)


def test_dry_run_does_not_perform() -> None:
    """回归：--dry-run 必须只选路，不得调用 skill.perform。"""
    section("dry-run 只选路，不执行 perform")
    from src.core.config import Config
    from src.router.intent import TaskIntent
    from src.router.router import Decision
    from src.scheduler.executor import Executor

    class _Rec:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set(self, **kw): pass
        def meta(self, **kw): pass

    class _Log:
        def __init__(self): self.events = []
        def event(self, kind, **kw): self.events.append((kind, kw))
        def info(self, *a, **kw): pass
        def warn(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def step(self, name, **kw): return _Rec()

    class _Bundle:
        def gui_available(self): return False
        def structured_backends(self): return {}

    class _Router:
        def decide(self, intent, **kw):
            return Decision(method="UNREAL_MCP", reason="test", rule="test",
                            alternatives=[{"method": "UE_PYTHON", "reason": "b", "score": 1.0}])
        def available_methods(self, **kw):
            return {"UNREAL_MCP": True, "UE_PYTHON": True}
        def feedback(self, *a, **kw): pass

    performed = {"n": 0}

    class _Skill:
        name = "fake"
        supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON"})
        def intent(self, params, **kw):
            return TaskIntent(skill="fake", description="dry-run 测试")
        def perform(self, ctx, method, params, trace):
            performed["n"] += 1
            raise AssertionError("dry-run 不应调用 perform")
        def preflight(self, ctx, params): return {}
        def verify(self, ctx, method, params, trace): return None

    cfg = isolated_config()
    cfg.data.setdefault("desktop", {})["dry_run"] = True
    log = _Log()
    ex = Executor(bundle=_Bundle(), config=cfg, logger=log)  # type: ignore[arg-type]
    ex.router = _Router()
    res = ex.run(_Skill(), {})
    ok("dry-run 结果 ok", res.ok is True, str(res.error))
    ok("dry-run 标记了 dry_run", (res.data or {}).get("dry_run") is True, str(res.data))
    ok("dry-run 记录了选中方式", res.method == "UNREAL_MCP", res.method)
    ok("dry-run 未调用 perform", performed["n"] == 0, str(performed))
    ok("发出 dry_run_stop 事件",
       any(k == "dry_run_stop" for k, _ in log.events), str([k for k, _ in log.events]))


def test_skill_supported_methods_filter() -> None:
    """skill 不支持的 method 不应进入降级尝试顺序。"""
    section("按 skill 能力过滤执行方式")
    from src.router.intent import TaskIntent
    from src.router.router import Decision
    from src.scheduler.executor import Executor

    class _Rec:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set(self, **kw): pass
        def meta(self, **kw): pass

    class _Log:
        def __init__(self): self.events = []; self.attempts = []
        def event(self, kind, **kw): self.events.append((kind, kw))
        def info(self, *a, **kw): pass
        def warn(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def step(self, name, **kw):
            self.attempts.append(kw)
            return _Rec()

    class _Bundle:
        def gui_available(self): return False
        def structured_backends(self): return {}

    class _Router:
        def decide(self, intent, **kw):
            return Decision(
                method="HYBRID", reason="router 误选", rule="test",
                alternatives=[
                    {"method": "KEYBOARD", "reason": "b", "score": 1.0},
                    {"method": "UNREAL_MCP", "reason": "b", "score": 2.0},
                ],
            )
        def available_methods(self, **kw):
            return {"HYBRID": True, "KEYBOARD": True, "UNREAL_MCP": True}
        def feedback(self, *a, **kw): pass

    class _Skill:
        name = "actor_find_like"
        supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON", "MOUSE"})
        def intent(self, params, **kw):
            return TaskIntent(skill="actor_find_like", description="t", read_only=True)
        def perform(self, ctx, method, params, trace):
            if method != "UNREAL_MCP":
                raise AssertionError(f"不应尝试 {method}")
            return {"ok": True}
        def preflight(self, ctx, params): return {}
        def verify(self, ctx, method, params, trace): return None
        def is_idempotent(self, params): return True

    log = _Log()
    ex = Executor(bundle=_Bundle(), config=isolated_config(), logger=log)  # type: ignore[arg-type]
    ex.router = _Router()
    res = ex.run(_Skill(), {})
    filt = [kw for kind, kw in log.events if kind == "method_filter"]
    ok("发出 method_filter 事件", bool(filt), str([k for k, _ in log.events]))
    ok("过滤掉 HYBRID/KEYBOARD",
       filt and set(filt[0].get("dropped") or []) >= {"HYBRID", "KEYBOARD"}, str(filt))
    ok("最终只尝试受支持的 UNREAL_MCP",
       res.ok is True and res.method == "UNREAL_MCP", f"{res.ok} {res.method}")


def test_skill_idempotent_declarations() -> None:
    """只读 / 可重复保存 的 skill 应显式声明幂等；写操作不得默认糊弄。"""
    section("skill 幂等性声明补齐")
    from src.skills.registry import SKILLS

    expect_true = {
        "actor_find": {},
        "actor_inspect": {"actor": "X"},
        "visual_inspect": {},
        "level_save": {},
    }
    for name, params in expect_true.items():
        ok(f"{name} 幂等", SKILLS[name].is_idempotent(params) is True, name)

    mv = SKILLS["actor_move"]
    ok("actor_move 相对位移不幂等", mv.is_idempotent({"axis": "z", "delta": -1}) is False)
    ok("actor_move 绝对定位幂等", mv.is_idempotent({"location": [0, 0, 1]}) is True)
    ok("actor_move place_on_ground 幂等", mv.is_idempotent({"place_on_ground": True}) is True)

    for name, skill in SKILLS.items():
        ok(f"{name} 声明了 supported_methods",
           bool(getattr(skill, "supported_methods", None)), name)
        info = skill.describe()
        ok(f"{name}.describe 含 supported_methods",
           "supported_methods" in info and info["supported_methods"], str(info))


def test_unreal_mcp_available_requires_ue_tcp() -> None:
    """available() 不能只看 MCP 工具面：UE 插件 TCP 不通时必须报 DOWN。"""
    section("Unreal MCP 可用性区分 server 与 UE 插件")
    from src.adapters.unreal.unreal_mcp import UnrealMCPBackend

    section_cfg = {
        "transport": "stdio",
        "stdio": {"command": "echo", "args": []},
        "ue_tcp": {"host": "127.0.0.1", "port": 1},  # 1 不可达
        "tool_contract": "domains",
    }
    be = UnrealMCPBackend(section_cfg)
    be.profile = "domains"
    be._tools = ["actor", "level", "util", "vision"]
    # 绕过真实 MCP 握手
    be._client = object()  # type: ignore[assignment]
    be._ready = True  # type: ignore[attr-defined]

    ok("UE TCP 不可达时 available=False", be.available() is False)
    ok("错误信息点出 TCP 端点",
       be._last_error and "12029" in be._last_error or (be._last_error and ":1" in be._last_error),
       str(be._last_error))

    be2 = UnrealMCPBackend(section_cfg, dry_run=True)
    be2.profile = "domains"
    ok("dry_run 时 available=True（便于只演示选路）", be2.available() is True)


def test_precise_ground_placement() -> None:
    """精确落回地面：射线命中中位数 → 绝对坐标（幂等）。"""
    section("精确落回地面（line_trace → 绝对 location）")
    from src.vision.ground_place import plan_ground_placement
    from src.skills.registry import SKILLS
    from src.planner import Planner

    # Demo2 形状：柱心 Z=240，extent 半高=140 → bottom=100；广场顶面 100
    hits = [
        {"impact_z": 100.0, "hit_actor_label": "EXT_Plaza_Central"},
        {"impact_z": 100.2, "hit_actor_label": "EXT_Plaza_Central"},
        {"impact_z": 99.8, "hit_actor_label": "EXT_Plaza_Central"},
        {"impact_z": 240.0, "hit_actor_label": "EXT_MarkerPost_3_W"},  # 打到自己，应排除
    ]
    plan = plan_ground_placement(
        location=[-7000.0, -3200.0, 240.0],
        extent=[40.0, 40.0, 140.0],
        hits=hits,
        exclude_actor="EXT_MarkerPost_3_W",
    )
    ok("精确落地 actionable", plan["actionable"] is True, str(plan.get("reason")))
    ok("支撑面 Z≈100（中位数）", abs(plan["support_z"] - 100.0) < 0.5, str(plan["support_z"]))
    ok("目标 Z = support + half_h = 240", abs(plan["location"][2] - 240.0) < 0.5, str(plan["location"]))
    ok("XY 保持不变", plan["location"][0] == -7000.0 and plan["location"][1] == -3200.0)
    ok("当前已贴地时 gap≈0", abs(plan["gap"]) < 1.0, str(plan["gap"]))
    ok("自己被 ignore 后未污染中位数", all(
        h.get("hit_actor_label") != "EXT_MarkerPost_3_W" for h in plan["hits"]
    ))

    # 浮空 400：底边 500，支撑 100 → 目标 Z = 240（回到贴地）
    plan2 = plan_ground_placement(
        location=[-7000.0, -3200.0, 640.0],
        extent=[40.0, 40.0, 140.0],
        hits=[{"impact_z": 100.0, "hit_actor_label": "Plaza"}],
        exclude_actor="Self",
    )
    ok("浮空时 gap=400", abs(plan2["gap"] - 400.0) < 1.0, str(plan2["gap"]))
    ok("浮空时目标 Z=240", abs(plan2["location"][2] - 240.0) < 1.0, str(plan2["location"]))
    ok("delta_z = -400", abs(plan2["delta_z"] + 400.0) < 1.0, str(plan2["delta_z"]))

    # 无命中 -> 拒绝动手
    bad = plan_ground_placement(location=[0, 0, 100], extent=[10, 10, 10], hits=[])
    ok("无命中 actionable=False", bad["actionable"] is False)
    ok("无命中不给出 location", "location" not in bad or bad.get("location") is None)

    # skill 幂等性：place_on_ground = True
    mv = SKILLS["actor_move"]
    ok("place_on_ground 幂等", mv.is_idempotent({"place_on_ground": True}) is True)
    ok("相对位移仍不幂等", mv.is_idempotent({"delta": -400}) is False)

    # 计划：precise 模式使用 place_on_ground
    p = Planner().plan_floating_repair(drop_by=400.0, ground_ref="EXT_Plaza_Central", precise=True)
    move = p.steps[1]
    ok("precise 计划带 place_on_ground", move.params.get("place_on_ground") is True, str(move.params))
    ok("precise 计划不再用相对 delta", "delta" not in move.params, str(move.params))
    ok("precise 计划名可区分", "precise" in p.name, p.name)


def main() -> int:
    test_domain_mapping()
    test_actor_ref_from_real_payload()
    test_extract_rows_real_shapes()
    test_floating_judge()
    test_floating_architecture_regression()
    test_verifiers()
    test_verifiers_accept_dict()
    test_fallback_counts_failures_not_successes()
    test_plan_and_placeholders()
    test_floating_reliability_guard()
    test_explicit_ground_detector()
    test_floating_verdict_paths()
    test_unexpected_error_does_not_poison_health()
    test_not_supported_does_not_poison_health()
    test_health_record_is_self_consistent()
    test_cooldown_is_surfaced_in_failure()
    test_method_health_clear()
    test_step_method_preference()
    test_non_idempotent_retry_guard()
    test_text_planner()
    test_health_not_double_counted()
    test_dry_run_does_not_perform()
    test_skill_supported_methods_filter()
    test_skill_idempotent_declarations()
    test_unreal_mcp_available_requires_ue_tcp()
    test_precise_ground_placement()
    test_suite_does_not_touch_production_state()

    print(f"\n{'=' * 60}")
    if _FAIL:
        print(f"失败 {len(_FAIL)} 项 / 通过 {_PASS} 项")
        for name in _FAIL:
            print(f"  - {name}")
        return 1
    print(f"全部通过（{_PASS} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
