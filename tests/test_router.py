"""Router 规则回归测试。

这是第一版最重要的测试：**证明 Router 是"按规则自动选"，而不是"写死流程"**。

做法：同一套 Router，喂不同的 TaskIntent，断言选出的方法不同且符合规则。
如果哪天有人把逻辑改成 `if task == "demo1"` 这种硬编码，这个测试会立刻红。

运行：
    python tests/test_router.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.router import KEYBOARD, MOUSE, UNREAL_MCP, VISION, Router, TaskIntent  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def eq(name: str, got, want, extra: str = "") -> None:
    global PASSES
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got={got!r} want={want!r}" + (f"   {extra}" if extra else ""))
    if ok:
        PASSES += 1
    else:
        FAILS.append(name)


def ok(name: str, cond: bool, extra: str = "") -> None:
    global PASSES
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra else ""))
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)


def within(name: str, got, allowed, extra: str = "") -> None:
    ok(name, got in allowed, f"got={got!r} allowed={sorted(allowed)!r}" + (f"  {extra}" if extra else ""))


class _Stub:
    """假的 UE 后端，只回答 available()。"""

    def __init__(self, is_available: bool):
        self._ok = is_available

    def available(self) -> bool:
        return self._ok


ALL = {
    "backends": {"UNREAL_MCP": _Stub(True), "UE_PYTHON": _Stub(True), "UE_COMMANDLET": _Stub(False)},
    "computer_use_available": True,
    "vision_available": True,
}


def main() -> int:
    router = Router()

    print("--- 1) MVP Demo：已知 Actor + 精确数值修改 ---")
    d = router.decide(
        TaskIntent(skill="actor_move", description="把 Roof_A 的 Z 坐标提高 20", known_actor="Roof_A", needs_exact_values=True),
        **ALL,
    )
    eq("known actor + exact value -> 结构化通道", d.method, UNREAL_MCP, d.reason)
    within("  命中规则", d.rule, {"known_actor", "exact_values"})
    ok("  提供了备选顺序", len(d.alternatives) > 0, f"{[a['method'] for a in d.alternatives]}")

    print("\n--- 2) 保存 Level：Phase4A 优先结构化可验证保存 ---")
    d = router.decide(
        TaskIntent(skill="level_save", description="保存当前 Level", known_shortcut="ctrl s", needs_exact_values=False,
                   context={"prefer_api_save": True}), **ALL
    )
    eq("save -> structured UNREAL_MCP first", d.method, UNREAL_MCP, d.reason)
    within("  命中规则", d.rule, {"structured_save"})
    # GUI shortcut remains a fallback candidate, not the first choice
    methods = [a["method"] for a in d.alternatives]
    ok("  KEYBOARD 仍在降级链", KEYBOARD in methods or any("KEYBOARD" in str(a) for a in d.considered), str(methods))

    print("\n--- 3) 悬空检查：需要视觉 + 需要精确修改 -> HYBRID ---")
    d = router.decide(
        TaskIntent(
            skill="visual_inspect",
            description="找到一个明显悬空的 Actor 并将其调整到合理位置",
            needs_vision=True,
            needs_exact_values=True,
        ),
        **ALL,
    )
    eq("vision + exact -> HYBRID", d.method, "HYBRID", d.reason)
    eq("  命中规则", d.rule, "hybrid_for_vision")

    print("\n--- 4) 纯审美判断（只读） -> VISION ---")
    d = router.decide(
        TaskIntent(skill="visual_inspect", description="看看画面比例是否合理", needs_vision=True, needs_exact_values=False, read_only=True),
        **ALL,
    )
    eq("pure aesthetic -> VISION", d.method, VISION, d.reason)

    print("\n--- 5) 批量 20 个 Actor -> 结构化通道 ---")
    d = router.decide(TaskIntent(skill="actor_move", description="把 20 个灯柱整体抬高 50", target_count=20, needs_exact_values=True), **ALL)
    eq("batch -> 结构化通道", d.method, UNREAL_MCP, d.reason)
    eq("  命中规则", d.rule, "batch")

    print("\n--- 6) 第三方插件面板操作 -> MOUSE ---")
    d = router.decide(
        TaskIntent(skill="actor_move", description="在插件面板里拖拽 gizmo 调整视角", ui_only=True, needs_exact_values=False), **ALL
    )
    eq("ui-only -> MOUSE", d.method, MOUSE, d.reason)

    print("\n--- 7) 降级：结构化通道全部不可用 -> 必须回落 GUI，而不是硬选 MCP ---")
    d = router.decide(
        TaskIntent(skill="actor_move", description="把 Roof_A 的 Z 提高 20", known_actor="Roof_A", needs_exact_values=True),
        backends={"UNREAL_MCP": _Stub(False), "UE_PYTHON": _Stub(False)},
        computer_use_available=True,
        vision_available=True,
    )
    within("全部结构化不可用 -> GUI 通道", d.method, {MOUSE, KEYBOARD, VISION, "HYBRID"}, d.reason)
    ok("  未选 UNREAL_MCP", d.method != UNREAL_MCP)

    print("\n--- 8) 没有任何通道可用 -> 必须明确报错，不许瞎选 ---")
    try:
        router.decide(
            TaskIntent(skill="actor_move", description="x"), backends={}, computer_use_available=False, vision_available=False
        )
        ok("no viable method raises", False, "竟然没抛异常")
    except Exception as exc:
        eq("no viable method raises", type(exc).__name__, "NoViableMethod")

    print("\n--- 9) 意图推断：从自然语言关键词 ---")
    t1 = TaskIntent.from_text("找到一个明显悬空的 Actor 并调整到合理位置")
    eq("关键词'悬空' -> needs_vision", t1.needs_vision, True)
    eq("关键词 -> skill", t1.skill, "visual_inspect")
    t2 = TaskIntent.from_text("保存当前关卡")
    eq("关键词'保存' -> shortcut", t2.known_shortcut, "ctrl s")
    t3 = TaskIntent.from_text("把 12 个灯柱抬高")
    eq("关键词'12 个' -> target_count", t3.target_count, 12)
    eq(" -> is_batch", t3.is_batch, True)
    t4 = TaskIntent.from_text("查看 Roof_A 的 transform")
    eq("只读意图", t4.read_only, True)

    print("\n--- 10) 成本明细必须可解释 ---")
    d = router.decide(TaskIntent(skill="actor_move", description="x", known_actor="A", needs_exact_values=True), **ALL)
    cd = d.cost.as_dict()
    ok("cost 含 tool_cost 分量", "tool_cost" in cd, f"tool_cost={cd.get('tool_cost')}")
    eq("cost 含 5 个加权分量", sorted(cd["components"]), ["cost", "latency", "precision", "risk", "success"])
    ok("considered 记录了被淘汰方案的原因", len(d.considered) >= 2 and any(c.get("rejected_because") for c in d.considered), f"{len(d.considered)} 条")

    print("\n--- 11) 同一意图、不同可用性 -> 决策应随环境变化（证明不是写死） ---")
    it = TaskIntent(skill="actor_move", description="抬高 Roof_A", known_actor="Roof_A", needs_exact_values=True)
    m_mcp = router.decide(it, backends={"UNREAL_MCP": _Stub(True), "UE_PYTHON": _Stub(True)}, computer_use_available=True, vision_available=True).method
    m_py = router.decide(it, backends={"UNREAL_MCP": _Stub(False), "UE_PYTHON": _Stub(True)}, computer_use_available=True, vision_available=True).method
    m_gui = router.decide(it, backends={"UNREAL_MCP": _Stub(False), "UE_PYTHON": _Stub(False)}, computer_use_available=True, vision_available=True).method
    print(f"       可用性变化 -> {m_mcp} / {m_py} / {m_gui}")
    ok("环境变化会改变决策", len({m_mcp, m_py, m_gui}) >= 2, f"{m_mcp}/{m_py}/{m_gui}")

    print(f"\n通过 {PASSES} 项，" + ("ROUTER TESTS OK" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
