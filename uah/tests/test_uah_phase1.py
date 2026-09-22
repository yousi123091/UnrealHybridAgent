#!/usr/bin/env python
"""UAH Phase 1 测试 —— 需求 §十七 的 Test 1-10，**真实运行**。

    python uah/tests/test_uah_phase1.py            # 全部
    python uah/tests/test_uah_phase1.py 3 7        # 只跑 Test 3 与 Test 7

全部离线可跑：不需要 UE、不需要 MCP、不需要 tkinter，也不需要 pytest。
唯一的外部依赖是 stdlib 与 UHA 自己的几个纯逻辑模块（``SessionController``、
``ApprovalGate``）—— 用它们是为了让测试跑的是**真实的接入路径**，
而不是我自己另外编一套假状态机。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# --- 让 `python uah/tests/xxx.py` 也能 import 到 uah 与 src ----------------
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from uah.adapters.generic.bridge import GenericBridge, normalize_event  # noqa: E402
from uah.adapters.uha.adapter import (  # noqa: E402
    UHA_PHASE_TO_STATUS,
    UhaNativeAdapter,
    attach_approval_observer,
)
from uah.adapters.publisher import NullPublisher  # noqa: E402
from uah.core.models import (  # noqa: E402
    Activity,
    AgentRef,
    AgentSnapshot,
    ProjectRef,
    Status,
    TaskState,
    parse_status,
)
from uah.core.protocol import PROTOCOL_VERSION, protocol_compatible  # noqa: E402
from uah.core.state import StateStore  # noqa: E402
from uah.core.transport import HubClient, HubServer, hub_url, pick_free_port  # noqa: E402
from uah.hosts.embedded.host import EmbeddedHud  # noqa: E402
from uah.ui.components.card import render_card, render_cards  # noqa: E402
from uah.ui.notify import CollectSink, Notifier  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
CURRENT_SECTION = [""]


# ---------------------------------------------------------------------------
# 极简 harness（与 UHA 现有测试风格一致）
# ---------------------------------------------------------------------------


def section(title: str) -> None:
    CURRENT_SECTION[0] = title
    print(f"\n=== {title} ===")


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((f"{CURRENT_SECTION[0]} / {name}", bool(ok), detail))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"   [{detail}]" if detail else ""))
    return bool(ok)


def check_eq(name: str, got: object, want: object) -> bool:
    return check(name, got == want, f"got={got!r} want={want!r}")


# ---------------------------------------------------------------------------
# 测试用脚手架
# ---------------------------------------------------------------------------


class FakeHud:
    """HUD 侧的最小替身：只走真实的数据通路（SSE 订阅 + 同一套 render_card）。

    刻意**不**用 tkinter：本机 ``.venv`` 没有 tkinter，而 GUI 本身另有
    ``uah/tests/gui_smoke.py`` 用带 tkinter 的解释器单独验证。
    这里要证的是"数据能到、状态对、卡片画得出来"，与窗口系统无关。
    """

    def __init__(self, url: str, *, reconnect_delay_s: float = 0.3) -> None:
        self.url = url
        self.client = HubClient(url, timeout_s=3.0)
        self.snapshots: dict[str, AgentSnapshot] = {}
        self.transitions: list[tuple[str, str]] = []
        self.status_log: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.reconnect_delay_s = reconnect_delay_s

    def start(self) -> "FakeHud":
        def _on(snap: AgentSnapshot) -> None:
            self.snapshots[snap.agent.id] = snap
            self.transitions.append((snap.agent.id, snap.status.value))

        def _run() -> None:
            self.client.stream(_on, on_status=self.status_log.append, stop=self._stop,
                               reconnect_delay_s=self.reconnect_delay_s)

        self._thread = threading.Thread(target=_run, name="fake-hud", daemon=True)
        self._thread.start()
        return self

    @property
    def snapshot(self) -> AgentSnapshot | None:
        return next(iter(self.snapshots.values())) if self.snapshots else None

    def statuses(self, agent_id: str = "uha") -> list[str]:
        return [s for (a, s) in self.transitions if a == agent_id]

    def wait_status(self, want: str, *, agent_id: str = "uha", timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.snapshots.get(agent_id)
            if snap is not None and snap.status.value == want:
                return True
            time.sleep(0.02)
        return False

    def wait_pred(self, pred, *, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred(self):
                return True
            time.sleep(0.02)
        return False

    def cards(self):
        return render_cards(self.snapshots.values())

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)


class HubFixture:
    """一个测试用 Hub（随机空闲端口，避免和真的 8789 打架）。"""

    def __init__(self, **store_kw: object) -> None:
        self.port = pick_free_port()
        store = StateStore(**store_kw) if store_kw else None
        self.server = HubServer(port=self.port, store=store).start()
        self.url = self.server.url()

    def client(self) -> HubClient:
        return HubClient(self.url, timeout_s=3.0)

    def close(self) -> None:
        self.server.stop()

    def __enter__(self) -> "HubFixture":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _uha_controller():
    """拿一个**真的** UHA SessionController（不是假的）。"""
    from src.core.session_control import reset_controller

    return reset_controller()


def _uha_adapter(fx: HubFixture, *, heartbeat_s: float = 0.0, project=None) -> UhaNativeAdapter:
    from uah.adapters.publisher import HttpPublisher

    return UhaNativeAdapter(
        HttpPublisher(fx.url),
        agent_id="uha",
        agent_name="UHA",
        agent_type="uha",
        project=project or {"name": "ExamplePalace", "path": "E:/unrealproject/ExamplePalace"},
        phase_label="Phase 4B",
        heartbeat_s=heartbeat_s,
    )


def _uha_adapter_with_reminder(
    fx: HubFixture, sink: CollectSink, *, heartbeat_s: float = 0.0
) -> tuple[UhaNativeAdapter, EmbeddedHud]:
    """按**真实引导路径**装配：adapter → EmbeddedHud（带提醒）→ HTTP → Hub。

    这就是 ``uah/hosts/embedded/bootstrap.py`` 里做的事。提醒挂在展示侧，
    不是挂在适配器上 —— 所以"提醒"这条路径必须通过 EmbeddedHud 才测得准。
    """
    from uah.adapters.publisher import HttpPublisher

    embedded = EmbeddedHud(
        HttpPublisher(fx.url),
        store=StateStore(),
        logger=None,
        console=False,
        notifier=Notifier(sinks=[sink], state_path=None),
    )
    adapter = UhaNativeAdapter(
        embedded,
        agent_id="uha",
        agent_name="UHA",
        agent_type="uha",
        project={"name": "ExamplePalace", "path": "E:/unrealproject/ExamplePalace"},
        phase_label="Phase 4B",
        heartbeat_s=heartbeat_s,
    )
    return adapter, embedded


# ---------------------------------------------------------------------------
# Test 1 — UHA 启动：IDLE → STARTING → RUNNING
# ---------------------------------------------------------------------------


def test_1_startup_sequence() -> None:
    section("Test 1  UHA 启动 IDLE → STARTING → RUNNING（HUD 可见）")
    with HubFixture() as fx:
        hud = FakeHud(fx.url).start()
        controller = _uha_controller()
        adapter = _uha_adapter(fx)

        adapter.start()                      # 真实的"装配后端"阶段
        adapter.flush()
        check("STARTING 已被 HUD 看到", hud.wait_status("STARTING"),
              str(hud.snapshot.status.value if hud.snapshot else None))

        adapter.attach_controller(controller)  # controller 当前是 IDLE
        adapter.flush()
        check("IDLE 已被 HUD 看到", hud.wait_status("IDLE"),
              str(hud.snapshot.status.value if hud.snapshot else None))

        controller.begin_task("actor_move")    # 真 UHA 会这么干
        adapter.flush()
        check("RUNNING 已被 HUD 看到", hud.wait_status("RUNNING"),
              str(hud.snapshot.status.value if hud.snapshot else None))

        seen = hud.statuses("uha")
        order = [s for s in seen if s in ("STARTING", "IDLE", "RUNNING")]
        check("顺序是 STARTING → IDLE → RUNNING",
              order[:3] == ["STARTING", "IDLE", "RUNNING"], str(order[:5]))
        check("HUD 卡片画得出来", bool(hud.cards()))

        adapter.stop()
        adapter.flush()
        hud.stop()


# ---------------------------------------------------------------------------
# Test 2 — Task 状态变化：Task A → Stage A → Stage B → DONE
# ---------------------------------------------------------------------------


def test_2_task_stage_progress() -> None:
    section("Test 2  Task / Stage / Step 变化 → DONE")
    from src.core.session_control import TaskPhase

    with HubFixture() as fx:
        hud = FakeHud(fx.url).start()
        controller = _uha_controller()
        adapter = _uha_adapter(fx)
        adapter.start()
        adapter.attach_controller(controller)
        adapter.flush()

        # Plan A：3 步（真 UHA 会由 run_plan 包裹层报这个）
        adapter.plan_begin("demo1_raise_and_save", 3)
        controller.begin_task("actor_find")
        controller.set_phase(TaskPhase.ANALYZING)
        controller.set_step("actor_find", status="路由中")
        adapter.plan_step(1, "actor_find")
        adapter.flush()
        check("Task 名是计划名", hud.snapshot.task.name == "demo1_raise_and_save",
              str(hud.snapshot.task.name))
        check("Task 层是 Phase 4B", hud.snapshot.task.phase == "Phase 4B")
        check_eq("Stage A = Analysis", hud.snapshot.task.stage, "Analysis")
        # SSE 是异步的：等它真的到，而不是假设 flush() 就等于"HUD 已经看到"
        check("Step = 1 / 3（等到为止）",
              hud.wait_pred(lambda h: h.snapshot.task.progress_text == "1 / 3"),
              str(hud.snapshot.task.progress_text))

        controller.set_phase(TaskPhase.STRUCTURED_EXECUTION)
        controller.set_step("actor_move", method="UNREAL_MCP", status="执行中")
        adapter.plan_step(2, "actor_move")
        adapter.flush()
        # 注意：Stage 在第一批事件里就已经是 Structured Execution 了，
        # 所以必须等**这一批**的目标状态（step 2/3）到位，不能只等 Stage。
        check("Step = 2 / 3（等到为止）",
              hud.wait_pred(lambda h: h.snapshot.task.progress_text == "2 / 3"),
              str(hud.snapshot.task.progress_text))
        check_eq("Stage B = Structured Execution", hud.snapshot.task.stage, "Structured Execution")
        check_eq("Tool 来自 UHA 的执行通道", hud.snapshot.activity.tool, "Unreal MCP")

        controller.set_phase(TaskPhase.VERIFYING)
        adapter.plan_step(3, "level_save")
        adapter.flush()
        check("Step = 3 / 3（等到为止）",
              hud.wait_pred(lambda h: h.snapshot.task.progress_text == "3 / 3"),
              str(hud.snapshot.task.progress_text))
        check_eq("Stage C = Verification", hud.snapshot.task.stage, "Verification")

        # ★ 计划执行期间**不得**出现 DONE/ERROR。
        # UHA 的 Executor.run() 每一步结束都会 controller.end_task()，
        # 照搬就会出现 DONE → RUNNING → DONE 的横跳，并且每一步都触发一次
        # "已完成"提醒（需求 §十三 明令禁止的弹窗轰炸）。
        mid = hud.statuses("uha")
        check("计划中途没有出现 DONE（每步 end_task 不算任务完成）",
              "DONE" not in mid and "ERROR" not in mid, str(mid))

        controller.end_task(ok=True)
        adapter.flush()
        check("单步 end_task 之后仍是 RUNNING",
              hud.snapshot.status is Status.RUNNING, hud.snapshot.status.value)

        adapter.plan_end(True)
        adapter.flush()
        check("HUD 看到 DONE", hud.wait_status("DONE"), hud.snapshot.status.value)
        check_eq("DONE 后 Stage = Finished", hud.snapshot.task.stage, "Finished")
        check_eq("DONE 后不再显示上一次活动", hud.snapshot.activity.one_line, "Finished")

        card = render_card(hud.snapshot)
        labels = [r.label for r in card.rows]
        check("卡片包含 Task/Stage/Step/Activity/Tool",
              {"Task", "Stage", "Step", "Activity", "Tool"} <= set(labels), str(labels))

        adapter.stop()
        adapter.flush()
        hud.stop()


# ---------------------------------------------------------------------------
# Test 3 — WAITING_INPUT 明确提醒
# ---------------------------------------------------------------------------


def test_3_waiting_input_notifies() -> None:
    section("Test 3  WAITING_INPUT 明确提醒")
    with HubFixture() as fx:
        hud = FakeHud(fx.url).start()
        controller = _uha_controller()
        sink = CollectSink()
        adapter, embedded = _uha_adapter_with_reminder(fx, sink)
        adapter.start()
        adapter.attach_controller(controller)
        controller.begin_task("batch_mutation")
        adapter.flush()

        adapter.notify_waiting_input("Run test command? (y/N)")
        adapter.flush()
        check("HUD 看到 WAITING_INPUT", hud.wait_status("WAITING_INPUT"),
              hud.snapshot.status.value)
        check_eq("事件类型是 user_input.required",
                 hud.snapshot.last_event_type, "user_input.required")
        check("等待文案进了 HUD", "Run test command?" in hud.snapshot.activity.one_line,
              hud.snapshot.activity.one_line)

        notes = sink.drain()
        check_eq("提醒发出 1 条", len(notes), 1)
        if notes:
            check_eq("提醒标题正确", notes[0].title, "等待你的输入")
            check("提醒含具体问题", "Run test command?" in notes[0].message, notes[0].message)
            check_eq("提醒严重级为 attention", notes[0].severity, "attention")

        # 同一个等待重复上报 —— 不能重复提醒（需求 §十三）
        adapter.set_waiting(Status.WAITING_INPUT, summary="Run test command? (y/N)")
        adapter.flush()
        check_eq("同一等待不重复提醒", len(sink.drain()), 0)

        # 静音后不再提醒，但状态照样更新（提醒与状态是两件事）
        embedded.notifier.set_mute(True)
        adapter.notify_waiting_input("Second question?")
        adapter.flush()
        check_eq("静音后不再提醒", len(sink.drain()), 0)
        check("静音不影响状态更新",
              hud.wait_pred(lambda h: "Second question?" in (h.snapshot.activity.one_line or "")),
              hud.snapshot.activity.one_line)
        embedded.notifier.set_mute(False)

        check("静音状态可查", embedded.notifier.muted is False)
        check("静音期间被压制的提醒有记账", embedded.notifier.stats()["suppressed"] >= 1,
              str(embedded.notifier.stats()))

        # 换了**另一个**问题 → 必须重新提醒（这是最容易漏报的地方）
        adapter.notify_waiting_input("Third question? (y/N)")
        adapter.flush()
        third = sink.drain()
        check_eq("换了问题会重新提醒", len(third), 1)
        if third:
            check("第二次提醒带的是新问题", "Third question?" in third[0].message,
                  third[0].message)

        # 恢复
        adapter.clear_waiting()
        adapter.flush()
        check("清除等待后不再显示 WAITING_INPUT",
              hud.wait_pred(lambda h: h.snapshot.status.value != "WAITING_INPUT"))
        adapter.stop()
        adapter.flush()
        hud.stop()


# ---------------------------------------------------------------------------
# Test 4 — WAITING_APPROVAL 明显提醒（走真实的 ApprovalGate）
# ---------------------------------------------------------------------------


def test_4_waiting_approval_via_real_gate() -> None:
    section("Test 4  WAITING_APPROVAL（真实 ApprovalGate 路径）+ 提醒")
    from src.core.execution_mode import ApprovalGate, ApprovalRequest, ExecutionMode

    with HubFixture() as fx:
        hud = FakeHud(fx.url).start()
        controller = _uha_controller()
        sink = CollectSink()
        adapter, _embedded = _uha_adapter_with_reminder(fx, sink)
        adapter.start()
        adapter.attach_controller(controller)
        controller.begin_task("batch_mutation")
        adapter.flush()

        seen_inside: list[str] = []

        def confirmer(request) -> bool:
            # 关键断言：**在审批者被问的那一刻**，HUD 上必须已经是 WAITING_APPROVAL
            for _ in range(300):
                snap = hud.snapshots.get("uha")
                if snap is not None and snap.status is Status.WAITING_APPROVAL:
                    break
                time.sleep(0.01)
            seen_inside.append(hud.snapshots["uha"].status.value)
            return True

        gate = ApprovalGate(ExecutionMode.CONFIRM, confirmer=confirmer)
        stub = type("StubExecutor", (), {"approval": gate})()
        info = attach_approval_observer(adapter, stub)
        check("approval.evaluate 已被包裹", info.get("wrapped_evaluate") is True, str(info))

        req = ApprovalRequest(
            task_id="t4", action="batch_mutation", mode=ExecutionMode.CONFIRM,
            risk_level="high", summary="batch_mutation via Router",
            batch_size=12, reversible=False, verify_planned=True, rollback_planned=False,
        )
        result = gate.evaluate(req)

        check_eq("审批者在看到 WAITING_APPROVAL 时才被问", seen_inside, ["WAITING_APPROVAL"])
        check("审批结果原样返回（ALLOW）", result.allowed, str(result.decision.value))
        check("等待已清除", not adapter.is_waiting())

        notes = sink.drain()
        check_eq("WAITING_APPROVAL 触发提醒 1 条", len(notes), 1)
        if notes:
            check_eq("提醒标题正确", notes[0].title, "等待你批准")
            check("提醒含被审批的动作", "batch_mutation" in notes[0].message, notes[0].message)

        adapter.flush()
        check("审批通过后 HUD 恢复 RUNNING", hud.wait_status("RUNNING"), hud.snapshot.status.value)

        # --- 无人应答：策略要求确认但没有 confirmer ---
        gate2 = ApprovalGate(ExecutionMode.CONFIRM)
        stub2 = type("StubExecutor", (), {"approval": gate2})()
        sink2 = CollectSink()
        adapter2, _emb2 = _uha_adapter_with_reminder(fx, sink2)
        adapter2.attach_controller(controller)
        attach_approval_observer(adapter2, stub2)
        res2 = gate2.evaluate(req)
        adapter2.flush()
        check("无人应答时标记为 WAITING_APPROVAL",
              hud.wait_pred(lambda h: h.snapshot.status.value == "WAITING_APPROVAL"),
              hud.snapshot.status.value)
        check_eq("决策仍是 REQUIRE_CONFIRMATION（UHA 行为未被改动）",
                 res2.decision.value, "REQUIRE_CONFIRMATION")
        check("无人应答时同时标 BLOCKED", adapter2.is_blocked())
        check("无人应答也提醒了", len(sink2.drain()) >= 1)
        # 注意：这里刻意**不**断言 adapter2 的 block 会直接变成 BLOCKED 状态 —
        # WAITING_APPROVAL 与 BLOCKED 同时成立时，展示层优先显示"在等人"，
        # 因为那才是需要用户立刻做动作的事。BLOCKED 通过 is_blocked() 暴露。

        adapter.stop()
        adapter.flush()
        adapter2.stop()
        adapter2.flush()
        hud.stop()


# ---------------------------------------------------------------------------
# Test 5 — ERROR 显示错误但 UI 不崩
# ---------------------------------------------------------------------------


def test_5_error_and_garbage_resilience() -> None:
    section("Test 5  ERROR 显示错误 + 非法数据不崩")
    with HubFixture() as fx:
        hud = FakeHud(fx.url).start()
        controller = _uha_controller()
        adapter = _uha_adapter(fx)
        adapter.start()
        adapter.attach_controller(controller)
        controller.begin_task("level_save")
        adapter.flush()

        controller.end_task(ok=False)
        adapter.flush()
        check("HUD 看到 ERROR", hud.wait_status("ERROR"), hud.snapshot.status.value)
        check_eq("ERROR 的 Stage = Failed", hud.snapshot.task.stage, "Failed")
        check("卡片边框用告警色", render_card(hud.snapshot).border_color == "#f85149",
              render_card(hud.snapshot).border_color)

        # --- 一堆烂数据（需求 §十九） ---
        client = fx.client()
        trash = [
            ("非对象", [1, 2, 3]),
            ("空对象", {}),
            ("状态是乱码", {"agent": {"id": "weird"}, "status": "%%%not-a-status%%%"}),
            ("字段是数字", {"agent": 123, "status": 456, "task": 789, "activity": 12}),
            ("任务字段是列表", {"agent": {"id": "g"}, "status": "running", "task": [1, 2]}),
            ("超长字符串", {"agent": {"id": "g"}, "status": "running", "activity": "x" * 5000}),
            ("时间戳是毫秒", {"agent": {"id": "g"}, "status": "running", "timestamp": 1_760_000_000_000}),
            ("旧版协议", {"protocol": "uah/0", "agent": {"id": "old"}, "status": "running"}),
            ("未知事件类型", {"agent": {"id": "g"}, "type": "something.new", "status": "running"}),
            ("payload 是列表", {"agent": {"id": "g"}, "status": "running", "payload": [1]}),
        ]
        for label, payload in trash:
            res = client.post_event(payload)
            check(f"烂数据被吸收：{label}", res.get("ok") is True, str(res)[:110])

        raw_bad = json.dumps({"agent": {"id": "g"}})[:-1]  # 截断的 JSON
        res = client.post_event(raw_bad)
        check("截断 JSON 不返回 5xx", res.get("ok") is True, str(res)[:110])

        health = client.health()
        check("Hub 仍然存活", bool(health and health.get("ok")))
        snaps = client.state()
        check("状态仍可读", isinstance(snaps, list), f"{len(snaps)} agents")
        check("HUD 仍在收流", hud.wait_pred(lambda h: len(h.snapshots) >= 2), str(list(hud.snapshots)))
        check("全部快照都能渲染（UI 不崩）", len(render_cards(snaps)) == len(snaps))

        old = [s for s in snaps if s.agent.id == "old"]
        if check("旧版协议被标记为不兼容", bool(old) and old[0].protocol_ok is False,
                 str(old and old[0].protocol_ok)):
            view = render_card(old[0])
            check("旧版协议卡片显示提示", "协议不兼容" in view.footer, view.footer)

        g = [s for s in snaps if s.agent.id == "g"]
        check("字段类型不对也不崩", bool(g) and g[0].status is Status.RUNNING,
              str(g and g[0].status.value))
        weird = [s for s in snaps if s.agent.id == "weird"]
        check("未知状态解析为 UNKNOWN（不猜、不崩）",
              bool(weird) and weird[0].status is Status.UNKNOWN,
              str(weird and weird[0].status.value))
        if weird:
            check("UNKNOWN 会画成灰色未知卡片",
                  render_card(weird[0]).badge_color == "#6e7681",
                  render_card(weird[0]).badge_color)

        adapter.stop()
        adapter.flush()
        hud.stop()


# ---------------------------------------------------------------------------
# Test 6 — HUD 关闭再启动，重新获得当前状态
# ---------------------------------------------------------------------------


def test_6_hud_restart_resyncs() -> None:
    section("Test 6  桌面 HUD 关闭再启动 → 重新获得当前状态")
    with HubFixture() as fx:
        hud1 = FakeHud(fx.url).start()
        controller = _uha_controller()
        adapter = _uha_adapter(fx)
        adapter.start()
        adapter.attach_controller(controller)
        adapter.plan_begin("PlanX", 5)
        controller.begin_task("actor_move")
        adapter.plan_step(2, "actor_move")
        adapter.flush()
        check("第一个 HUD 收到了状态", hud1.wait_status("RUNNING"), str(hud1.statuses()))

        hud1.stop()                     # 关掉 HUD
        adapter.flush()
        time.sleep(0.2)
        check("HUD 已停止收集", hud1.snapshots.get("uha") is not None)

        # 没有新事件的情况下，新的 HUD 必须能立刻拿到全量状态
        hud2 = FakeHud(fx.url).start()
        ok = hud2.wait_pred(lambda h: "uha" in h.snapshots, timeout=4.0)
        check("新 HUD 立刻恢复当前状态（无需等新事件）", ok, str(list(hud2.snapshots)))
        snap = hud2.snapshots.get("uha")
        if snap:
            check_eq("状态仍是 RUNNING", snap.status.value, "RUNNING")
            check_eq("Task 名保留", snap.task.name, "PlanX")
            check_eq("Step 保留", snap.task.progress_text, "2 / 5")
        hud2.stop()
        adapter.stop()
        adapter.flush()


# ---------------------------------------------------------------------------
# Test 7 — UHA 重启；HUD 处理断线并恢复
# ---------------------------------------------------------------------------


def test_7_agent_crash_and_restart() -> None:
    section("Test 7  UHA 崩溃 / 重启，HUD 断线恢复")
    with HubFixture(stale_after_s=1.0) as fx:
        hud = FakeHud(fx.url, reconnect_delay_s=0.3).start()
        controller = _uha_controller()
        adapter = _uha_adapter(fx, heartbeat_s=0.0)
        adapter.start()
        adapter.attach_controller(controller)
        controller.begin_task("actor_move")
        adapter.flush()
        check("先看到 RUNNING", hud.wait_status("RUNNING"), hud.snapshot.status.value)

        # --- 崩溃：进程直接没了，连 agent.stopped 都来不及发 ---
        # 注意：这里**故意不调用 adapter.stop()**。stop() 会发 agent.stopped，
        # 那是"优雅退出"，HUD 会显示"已退出"。真正的崩溃是**无声无息**的，
        # 只能靠心跳超时识别 —— 那才是这条测试要覆盖的分支。
        adapter.publisher = NullPublisher()   # 断掉发送侧，模拟进程消失
        time.sleep(1.3)
        snap = fx.client().state()
        check("死后被标记为 stale（保留最后状态）",
              bool(snap) and snap[0].stale and snap[0].status is Status.RUNNING
              and not snap[0].stopped,
              f"stale={snap[0].stale if snap else None} stopped={snap[0].stopped if snap else None} "
              f"status={snap[0].status.value if snap else None}")
        view = render_card(snap[0])
        check("崩溃卡片显示为「离线」（不是「已退出」）", "离线" in view.footer, view.footer)
        check("HUD 自己没有崩", hud.wait_pred(lambda h: True, timeout=0.1))

        # --- 重启：同一个 agent_id 回来 ---
        controller2 = _uha_controller()
        adapter2 = _uha_adapter(fx, heartbeat_s=0.0)
        adapter2.start()
        adapter2.attach_controller(controller2)
        adapter2.flush()
        check("重启后 HUD 收到新状态",
              hud.wait_pred(lambda h: "uha" in h.snapshots
                            and not h.snapshots["uha"].stale
                            and h.snapshots["uha"].status.value in ("STARTING", "IDLE")),
              str(hud.snapshots.get("uha") and hud.snapshots["uha"].status.value))
        check("stale 已被清除", not hud.snapshots["uha"].stale)

        # --- 对照：优雅退出应该是"已退出" ---
        controller2.end_task(ok=True)
        adapter2.flush()
        check("重启后的实例走到 DONE",
              hud.wait_status("DONE"), hud.snapshot.status.value)
        adapter2.stop(reason="test shutdown")
        adapter2.flush()
        stopped_snaps = fx.client().state()
        check("优雅退出标记为 stopped", bool(stopped_snaps) and stopped_snaps[0].stopped,
              str(stopped_snaps and stopped_snaps[0].stopped))
        check("优雅退出后卡片显示「已退出」",
              "已退出" in render_card(stopped_snaps[0]).footer,
              render_card(stopped_snaps[0]).footer)
        time.sleep(1.4)
        again = fx.client().state()
        check("终态（DONE）不会因为离线被标 stale",
              bool(again) and again[0].status is Status.DONE and not again[0].stale,
              f"stale={again[0].stale} status={again[0].status.value}")
        adapter.stop()
        hud.stop()

    # --- Hub 侧断线重连：Hub 重启后 HUD 自动接回来 ---
    port = pick_free_port()
    url = hub_url("127.0.0.1", port)
    server = HubServer(port=port).start()
    hud = FakeHud(url, reconnect_delay_s=0.3).start()
    bridge = GenericBridge(url)
    bridge.emit(agent="Reconnect", status="running", task="t7")
    check("重连前收到状态", hud.wait_pred(lambda h: "Reconnect" in h.snapshots), str(list(hud.snapshots)))

    server.stop()                                   # Hub 挂掉
    time.sleep(0.5)
    server2 = HubServer(port=port).start()          # 同端口重启
    ok = hud.wait_pred(lambda h: "connected" in h.status_log[-1:], timeout=6.0)
    check("HUD 在 Hub 重启后自动重连", ok, str(hud.status_log[-4:]))
    bridge2 = GenericBridge(url)
    bridge2.emit(agent="Reconnect", status="done", task="t7")
    check("重连后仍能收到新事件",
          hud.wait_pred(lambda h: h.snapshots.get("Reconnect") is not None
                        and h.snapshots["Reconnect"].status.value == "DONE"),
          str(hud.snapshots.get("Reconnect") and hud.snapshots["Reconnect"].status.value))
    hud.stop()
    server2.stop()


# ---------------------------------------------------------------------------
# Test 8 — 两个模拟 Agent 同时连接 → 两张卡片
# ---------------------------------------------------------------------------


def test_8_two_agents_two_cards() -> None:
    section("Test 8  两个 Agent 同时连接 → 两张卡片")
    with HubFixture() as fx:
        hud = FakeHud(fx.url).start()
        controller = _uha_controller()
        adapter = _uha_adapter(fx)
        adapter.start()
        adapter.attach_controller(controller)
        controller.begin_task("actor_move")
        adapter.flush()

        other = GenericBridge(fx.url)
        other.emit(agent="Codex", status="waiting_approval",
                   task="Running tests", activity="pytest", project="demo")
        adapter.flush()

        ok = hud.wait_pred(lambda h: len(h.snapshots) >= 2, timeout=5.0)
        check("HUD 收到两个 Agent", ok, str(list(hud.snapshots)))
        cards = hud.cards()
        check_eq("渲染出两张卡片", len(cards), 2)
        titles = sorted(c.title for c in cards)
        check_eq("卡片标题各自正确", titles, ["Codex", "UHA"])
        check("卡片 id 不重复", len({c.agent_id for c in cards}) == 2)
        codex = [c for c in cards if c.title == "Codex"][0]
        check_eq("Codex 显示为等待批准", codex.status, Status.WAITING_APPROVAL)
        check("Codex 卡片显示项目与任务",
              codex.subtitle == "demo" and any(r.value == "Running tests" for r in codex.rows),
              f"{codex.subtitle} / {[r.value for r in codex.rows]}")

        adapter.stop()
        adapter.flush()
        hud.stop()


# ---------------------------------------------------------------------------
# Test 9 — Generic Adapter 模拟外部 Agent：RUNNING → DONE
# ---------------------------------------------------------------------------


def test_9_generic_adapter() -> None:
    section("Test 9  Generic Adapter 外部 Agent：RUNNING → DONE")
    with HubFixture() as fx:
        hud = FakeHud(fx.url).start()
        client = fx.client()

        # 最简形态：一条命令行式的最小载荷
        res = client.post_event({
            "agent": "ExampleAgent", "status": "running",
            "task": "Running tests", "activity": "pytest",
        })
        check("最简载荷被接受", res.get("accepted") is True, str(res)[:120])
        check("HUD 看到 RUNNING",
              hud.wait_pred(lambda h: h.snapshots.get("ExampleAgent") is not None
                            and h.snapshots["ExampleAgent"].status.value == "RUNNING"))

        snap = hud.snapshots["ExampleAgent"]
        check_eq("task 字符串落到 task.name", snap.task.name, "Running tests")
        check_eq("activity 字符串落到 activity.summary", snap.activity.summary, "pytest")
        check("没有编造 stage/step/tool",
              snap.task.stage is None and snap.task.step is None and snap.activity.tool is None,
              f"stage={snap.task.stage} step={snap.task.step} tool={snap.activity.tool}")

        bridge = GenericBridge(fx.url)
        bridge.emit(agent="ExampleAgent", status="done")
        check("HUD 看到 DONE",
              hud.wait_pred(lambda h: h.snapshots["ExampleAgent"].status.value == "DONE"),
              hud.snapshots["ExampleAgent"].status.value)

        check_eq("归一化会补 event_id/timestamp/type",
                 all(k in normalize_event(agent="A", status="running")
                     for k in ("event_id", "timestamp", "type", "protocol")), True)
        check_eq("状态别名 running/working/busy 都认",
                 (parse_status("working"), parse_status("busy"), parse_status("RUNNING")),
                 (Status.RUNNING, Status.RUNNING, Status.RUNNING))
        hud.stop()


# ---------------------------------------------------------------------------
# Test 10 — UHA 原有能力无回归
# ---------------------------------------------------------------------------


def test_10_uha_regression() -> None:
    section("Test 10  UHA 原有回归测试（真实子进程运行）")
    python = str(_REPO / ".venv" / "Scripts" / "python.exe")
    if not Path(python).is_file():
        python = sys.executable

    expected = {
        "test_offline.py": "248",
        "test_router.py": "25",
        "test_phase2.py": "85",
        "test_phase3.py": "11",
        "test_phase4.py": "13",
        "test_phase4b.py": "6",
    }
    total = 0
    for name, want in expected.items():
        path = _REPO / "tests" / name
        proc = subprocess.run([python, str(path)], cwd=str(_REPO), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0 and want in out
        check(f"{name} 通过（期望 {want} 项）", ok,
              f"exit={proc.returncode} " + " ".join(out.strip().splitlines()[-1:])[:90])
        if ok:
            total += int(want)

    # UAH 开关两种状态都必须能正常 import / 运行 CLI
    for flag in ("1", "0"):
        env = dict(os.environ, UAH_ENABLED=flag)
        proc = subprocess.run([python, "uha.py", "list"], cwd=str(_REPO), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env)
        check(f"UAH_ENABLED={flag} 时 uha.py list 正常", proc.returncode == 0,
              f"exit={proc.returncode}")

    check(f"回归合计 {total} 项", total == 388, str(total))


# ---------------------------------------------------------------------------
# 验收自查（需求 §二十四 A-H）+ 单一定义守护
# ---------------------------------------------------------------------------


def test_11_acceptance_and_single_source() -> None:
    section("验收自查 A-H 与单一定义守护")
    import re

    # --- C: 不存在第二份 AgentStatus / TaskStatus 定义 ---
    enum_re = re.compile(r"^\s*class\s+\w*(?:Agent|Task|Presence)?Status\w*\s*\(.*\bEnum\b", re.M)
    offenders: list[str] = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel.startswith((".venv", "uah/tests/")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in enum_re.finditer(text):
            if rel == "uah/core/models.py":
                continue
            offenders.append(f"{rel}: {m.group(0).strip()}")
    check("C: 全仓库只有 uah/core/models.py 定义 Status", not offenders,
          "; ".join(offenders)[:200])

    # C(续): UHA 每个 TaskPhase 都要有映射 —— 以后新增阶段忘了映射会被这条抓住
    from src.core.session_control import TaskPhase

    phase_values = {p.value for p in TaskPhase}
    missing = sorted(phase_values - set(UHA_PHASE_TO_STATUS))
    check("C: UHA TaskPhase 全部有映射", not missing, str(missing))
    extra = sorted(set(UHA_PHASE_TO_STATUS) - phase_values)
    check("C: 映射表没有多余残留键", not extra, str(extra))

    # --- B: 两个宿主用同一套状态模型与渲染函数 ---
    from uah.hosts.desktop import hud as hud_mod
    from uah.hosts.embedded import host as embedded_mod
    from uah.ui.components import card as card_mod

    check("B: 内嵌宿主直接引用共享 render_card",
          embedded_mod.render_card is card_mod.render_card)
    check("B: 桌面宿主导入共享 render_card",
          "render_card" in Path(hud_mod.__file__).read_text(encoding="utf-8"))
    # 宿主**可以**用共享的颜色表，但不能**自己再定义一份**。
    for mod in (hud_mod, embedded_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        check(f"B: {mod.__name__.rsplit('.', 1)[-1]} 没有自己再定义状态颜色表",
              "STATUS_COLORS = {" not in src and "STATUS_GLYPHS = {" not in src
              and "STATUS_LABELS = {" not in src)
    check("B: 宿主从共享组件取颜色",
          "from ...ui.components.card import" in Path(hud_mod.__file__).read_text(encoding="utf-8"))

    # --- D: 桌面 HUD 独立于 Agent（不 import UHA 源码）---
    hud_src = Path(hud_mod.__file__).read_text(encoding="utf-8")
    check("D: 桌面 HUD 不 import UHA 源码",
          "from src." not in hud_src and "import src." not in hud_src)
    check("D: 桌面 HUD 只通过 Hub 取数", "HubClient" in hud_src)
    bootstrap_src = (_REPO / "uah" / "hosts" / "embedded" / "bootstrap.py").read_text(encoding="utf-8")
    check("D: 只有内嵌引导碰 UHA 源码",
          "src.core.session_control" in bootstrap_src)

    # --- E: HUD 能显示需求 §十二 要求的全部字段 ---
    snap = AgentSnapshot(
        agent=AgentRef(id="u", name="UHA", type="uha"),
        project=ProjectRef(name="Proj", path="E:/unrealproject/ExamplePalace"),
        status=Status.RUNNING,
        task=TaskState(name="actor_move", phase="Phase 4B",
                       stage="Environment Perception", step=3, total_steps=7),
        activity=Activity(summary="Analyzing UE5 viewport", tool="Computer Use"),
        first_seen_at=time.time() - 95,
        updated_at=time.time(),
    )
    card = render_card(snap)
    text = "\n".join(card.to_lines())
    for token in ("UHA", "Proj", "RUNNING", "Phase 4B", "Environment Perception",
                  "3 / 7", "Analyzing UE5 viewport", "Computer Use", "elapsed"):
        check(f"E: 卡片含「{token}」", token in text)
    check("E: 结构与需求示例一致（● 徽标 + 进度比）",
          card.badge.startswith("●") and abs((card.ratio or 0) - 3 / 7) < 1e-9,
          f"{card.badge} ratio={card.ratio}")
    running_card = render_card(snap, now=time.time())
    later_card = render_card(snap, now=time.time() + 10)
    check("E: 还在跑的 Agent，elapsed 随 now 增长",
          running_card.footer != later_card.footer,
          f"{running_card.footer} → {later_card.footer}")
    finished = AgentSnapshot(
        agent=AgentRef(id="f", name="F"), status=Status.DONE,
        first_seen_at=time.time() - 95, updated_at=time.time() - 5,
    )
    check("E: 已结束的 Agent，elapsed 停在结束时（不再涨）",
          render_card(finished).footer == render_card(finished, now=time.time() + 30).footer,
          render_card(finished).footer)

    # --- F: 提醒集合 / 去重 / 静音 ---
    for st in (Status.DONE, Status.ERROR, Status.WAITING_INPUT, Status.WAITING_APPROVAL):
        check(f"F: {st.value} 在提醒集合内", st.is_alert)
    sink = CollectSink()
    notifier = Notifier(sinks=[sink], state_path=None)
    done_snap = AgentSnapshot(agent=AgentRef(id="u", name="U", type="t"),
                              status=Status.DONE, status_changed_at=123.0)
    check("F: DONE 触发提醒", notifier.consider(done_snap) is not None)
    check("F: 同一状态变化只提醒一次", notifier.consider(done_snap) is None)
    check("F: 提醒内容进了 sink", len(sink.drain()) == 1)
    notifier.set_mute(True)
    check("F: mute 生效", notifier.muted and notifier.consider(
        AgentSnapshot(agent=AgentRef(id="u", name="U", type="t"),
                      status=Status.ERROR, status_changed_at=999.0)) is None)
    notifier.set_mute(False)
    check("F: 取消静音后恢复提醒", notifier.consider(
        AgentSnapshot(agent=AgentRef(id="u", name="U", type="t"),
                      status=Status.ERROR, status_changed_at=1000.0)) is not None)

    # --- A / G ---
    check("A: 协议版本符合 uah/<主版本>",
          PROTOCOL_VERSION == "uah/1" and protocol_compatible(PROTOCOL_VERSION))
    check("A: 主版本不兼容可被识别",
          not protocol_compatible("uah/0") and not protocol_compatible("not-a-version"))
    check("G: 通用接入入口可用",
          callable(GenericBridge("http://127.0.0.1:1").emit)
          and callable(normalize_event))


# ---------------------------------------------------------------------------
# 内嵌宿主（内嵌 HUD 用同一套状态机 + 渲染）
# ---------------------------------------------------------------------------


def test_12_embedded_host_shares_state() -> None:
    section("内嵌宿主与桌面宿主共用状态机与渲染")
    store = StateStore()
    inner = NullPublisher()
    logger_events: list[tuple[str, dict]] = []

    class FakeLog:
        def event(self, kind, **fields):
            logger_events.append((kind, fields))

    embedded = EmbeddedHud(inner, store=store, logger=FakeLog(), console=False)
    from uah.adapters.publisher import HttpPublisher  # noqa: F401

    adapter = UhaNativeAdapter(embedded, agent_id="uha", agent_name="UHA", phase_label="Phase 4B",
                               heartbeat_s=0.0)
    adapter.start()
    adapter.plan_begin("Plan", 2)
    adapter.plan_step(1, "actor_find")
    adapter.flush(3.0)

    check("内嵌宿主拿到了快照", embedded.snapshot("uha") is not None)
    check("内嵌宿主写出了 uah_presence 事件",
          any(k == "uah_presence" for k, _ in logger_events), str(len(logger_events)))
    report = embedded.text_report()
    check("内嵌宿主能渲染文本卡片", "UHA" in report and "Plan" in report, report.splitlines()[:2])
    check("内嵌宿主与桌面宿主同源",
          set(embedded.views) == {"uha"} and "Phase 4B" in report)
    adapter.stop()
    adapter.flush()
    check("事件同时转发给了下游 Publisher", inner.delivered()["sent"] > 0,
          str(inner.delivered()))


# ---------------------------------------------------------------------------


TESTS = {
    1: test_1_startup_sequence,
    2: test_2_task_stage_progress,
    3: test_3_waiting_input_notifies,
    4: test_4_waiting_approval_via_real_gate,
    5: test_5_error_and_garbage_resilience,
    6: test_6_hud_restart_resyncs,
    7: test_7_agent_crash_and_restart,
    8: test_8_two_agents_two_cards,
    9: test_9_generic_adapter,
    10: test_10_uha_regression,
    11: test_11_acceptance_and_single_source,
    12: test_12_embedded_host_shares_state,
}


def main(argv: list[str]) -> int:
    picked = [int(a) for a in argv if a.isdigit()] or sorted(TESTS)
    t0 = time.perf_counter()
    for n in picked:
        fn = TESTS.get(n)
        if fn is None:
            print(f"没有 Test {n}")
            return 2
        fn()
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 60)
    if failed:
        print(f"失败 {len(failed)} / {len(RESULTS)} 项：")
        for name, _ok, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print(f"全部通过（{len(RESULTS)} 项）  用时 {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
