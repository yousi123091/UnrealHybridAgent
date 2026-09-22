"""UHA Native Adapter —— 把 UHA 的运行时状态翻译成 UAH 事件。

## 接入点

UHA **唯一**的状态真源是 ``src/core/session_control.py::SessionController``，
而且它**已经有**观察者钩子 ``on_change(fn)``。所以这个适配器：

* **不**碰 Executor / Router / Skills / Adapters 的业务逻辑；
* **不**碰 ``ControlOverlay``；
* **不**新增第二个状态定义 —— ``TaskPhase`` 是 UHA 的执行阶段，``Status`` 是
  对外存在状态，两者之间**只有本文件这一张映射表**。

## 为什么映射表用字符串而不是 import UHA 的枚举

映射表用 UHA 阶段名的**字符串**做键（``"ANALYZING"`` 等），这样：

1. 适配器不 import UHA 的类型，HUD 侧单独跑也不会因为 import 不到 UHA 而崩；
2. 新增一个 ``TaskPhase`` 而忘了加映射时，结果是 ``UNKNOWN``（**可见的错**），
   而不是静默落进某个默认分支（**看不见的错**）；
3. ``uah/tests`` 里有一条断言要求映射表覆盖 ``TaskPhase`` 的全部成员 —— 漂移会被测试抓住。

## 非阻塞（这一条是硬的）

``on_change`` 是**在 ``SessionController`` 的锁内同步触发**的。如果在这里直接做
HTTP 发送，就会拿 UHA 的会话锁去等网络 —— 一次 HUD 侧的网络卡顿能把 Executor 卡住。
所以所有事件都先进本对象的**出站队列**，由独立线程发送。队列满时丢**最旧**的并计数：
丢一条活动更新，远好过阻塞 Agent。

## 非侵入的运行时埋点（全部是"旁路观察"，不改 UHA 行为）

* 计划进度（Step i / N）：**包裹** ``executor.run_plan`` / ``executor.run``，只在边界上
  记账，返回值原样透传，异常原样抛出。与 ``uha.py`` 里已有的
  ``executor.approval._confirmer = ...`` 是同一手法。
* 审批等待（WAITING_APPROVAL）：**包裹** ``executor.approval.evaluate``。返回值原样返回，
  所以 UHA 的审批语义（谁能执行、什么被拒）与 UAH 是否存在**完全无关**。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Mapping

from ...core.models import Activity, AgentRef, ProjectRef, Status, TaskState
from ...core.protocol import (
    EV_ACTIVITY_CHANGED,
    EV_AGENT_HEARTBEAT,
    EV_AGENT_STARTED,
    EV_AGENT_STOPPED,
    EV_APPROVAL_REQUIRED,
    EV_STATUS_CHANGED,
    EV_TASK_CANCELLED,
    EV_TASK_COMPLETED,
    EV_TASK_FAILED,
    EV_TASK_STAGE_CHANGED,
    EV_TASK_STEP_CHANGED,
    EV_USER_INPUT_REQUIRED,
    PROTOCOL_VERSION,
)
from ...core.transport import new_event_id
from ..publisher import NullPublisher, Publisher

# ---------------------------------------------------------------------------
# ★ 唯一的映射表：UHA 执行阶段 → UAH 存在状态
# ---------------------------------------------------------------------------
#
# 这张表就是需求 §六（Single Source of Truth）在 UHA 侧的落点。
# 它把 UHA 的**实现细节**（ANALYZING / STRUCTURED_EXECUTION / COMPUTER_CONTROL /
# VERIFYING 这四种"正在干活"的方式）收敛成一个对外的 RUNNING。
# 泄露细节的代价是：以后 UHA 内部多一个阶段，所有 UI 都得跟着改。

UHA_PHASE_TO_STATUS: dict[str, Status] = {
    "IDLE": Status.IDLE,
    "ANALYZING": Status.RUNNING,
    "STRUCTURED_EXECUTION": Status.RUNNING,
    "COMPUTER_CONTROL": Status.RUNNING,
    "VERIFYING": Status.RUNNING,
    "PAUSED": Status.PAUSED,
    # STOPPING 映射成 RUNNING 而不是 CANCELLED：它还在收尾，没结束。
    # 提前报 CANCELLED 会让 HUD 亮起"已取消"，而任务其实还在释放锁。
    "STOPPING": Status.RUNNING,
    "ABORTED": Status.CANCELLED,
    "DONE": Status.DONE,
    "FAILED": Status.ERROR,
}

#: UHA 阶段 → 卡片上 "Stage" 那一行（需求 §五 的语义分层第二层）
UHA_PHASE_TO_STAGE: dict[str, str] = {
    "IDLE": "Idle",
    "ANALYZING": "Analysis",
    "STRUCTURED_EXECUTION": "Structured Execution",
    "COMPUTER_CONTROL": "Computer Control",
    "VERIFYING": "Verification",
    "PAUSED": "Paused",
    "STOPPING": "Stopping",
    "ABORTED": "Aborted",
    "DONE": "Finished",
    "FAILED": "Failed",
}

#: UHA 执行通道 → UAH ``activity.tool``。键就是 UHA Router 的真实方法名。
UHA_METHOD_TO_TOOL: dict[str, str] = {
    "UNREAL_MCP": "Unreal MCP",
    "UE_PYTHON": "UE Python",
    "UE_COMMANDLET": "UE Commandlet",
    "MOUSE": "Mouse",
    "KEYBOARD": "Keyboard",
    "VISION": "Vision",
    "HYBRID": "Hybrid",
}

#: 出站队列上限。满了丢最旧的：宁可少一条活动更新，也不能阻塞 Agent。
DEFAULT_QUEUE_SIZE = 512


class UhaNativeAdapter:
    """UHA → UAH。线程安全，非阻塞。"""

    def __init__(
        self,
        publisher: Publisher | None = None,
        *,
        agent_id: str = "uha",
        agent_name: str = "UHA",
        agent_type: str = "uha",
        project: ProjectRef | Mapping[str, Any] | None = None,
        phase_label: str | None = None,
        heartbeat_s: float = 10.0,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.publisher: Publisher = publisher or NullPublisher()
        self.agent = AgentRef(id=agent_id, name=agent_name, type=agent_type)
        if isinstance(project, ProjectRef):
            self.project = project
        elif isinstance(project, Mapping):
            self.project = ProjectRef.from_wire(dict(project))
        else:
            self.project = ProjectRef()

        #: 卡片上 "Task" 层的大阶段标签（例如 "Phase 4B"）。
        #: 这是 UHA 的开发里程碑，来自配置，**不是运行时推断** —— 不编造。
        self.phase_label = phase_label

        self._lock = threading.RLock()
        self._seq = 0
        self._last_event_id: str | None = None
        self._last_key: tuple = ()
        self._emitted = 0
        self._skipped_dup = 0
        self._dropped = 0
        self._started = False
        self._stopped = False

        # 会话控制状态
        self._controller: Any | None = None
        self._plan_name: str | None = None
        self._plan_total: int | None = None
        self._plan_index: int | None = None
        self._skill: str | None = None
        self._last_step_key: tuple = ()
        #: 计划正在执行中（plan_begin..plan_end）。用来把"单步跑完"与"任务完成"分开。
        self._plan_active = False

        # 瞬态覆盖：等待 / 卡住时优先级高于 controller 映射
        self._override: Status | None = None
        self._override_activity: Activity | None = None
        self._blocked_reason: str | None = None

        self._raw_phase: str = "IDLE"

        self._heartbeat_s = float(heartbeat_s)
        self._heartbeat_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()
        self._input_fn = input_fn

        # 出站队列 + 发送线程
        self._outbox: "queue.Queue[dict[str, Any] | None]" = queue.Queue(maxsize=max(8, int(queue_size)))
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()

    # ------------------------------------------------------------------
    # 出站（非阻塞的关键）
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker_stop.clear()
        self._worker = threading.Thread(target=self._drain_loop, name="uah-uha-publisher", daemon=True)
        self._worker.start()

    def _enqueue(self, event: Mapping[str, Any]) -> None:
        self._ensure_worker()
        payload = dict(event)
        try:
            self._outbox.put_nowait(payload)
        except queue.Full:
            # 丢最旧的：出站积压说明下游慢，继续堆只会让状态越来越陈旧
            try:
                self._outbox.get_nowait()
                self._outbox.task_done()
                with self._lock:
                    self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._outbox.put_nowait(payload)
            except queue.Full:
                with self._lock:
                    self._dropped += 1
                return

    def _drain_loop(self) -> None:
        while True:
            try:
                item = self._outbox.get(timeout=0.2)
            except queue.Empty:
                if self._worker_stop.is_set():
                    return
                continue
            try:
                if item is None:
                    return
                self.publisher.publish(item)
            finally:
                self._outbox.task_done()

    def flush(self, timeout: float = 2.0) -> bool:
        """等出站队列排空（测试与优雅退出用）。"""
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if self._outbox.empty():
                return True
            time.sleep(0.005)
        return self._outbox.empty()

    def _stop_worker(self) -> None:
        self.flush(1.0)
        self._worker_stop.set()
        try:
            self._outbox.put_nowait(None)
        except queue.Full:
            pass
        worker = self._worker
        if worker is not None:
            worker.join(timeout=1.5)
        self._worker = None

    # ------------------------------------------------------------------
    # 事件构造
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _emit(
        self,
        etype: str,
        *,
        status: Status | None = None,
        task: TaskState | None = None,
        activity: Activity | None = None,
        project: ProjectRef | None = None,
        payload: Mapping[str, Any] | None = None,
        dedupe_key: tuple | None = None,
    ) -> bool:
        """构造并入队一条事件。返回是否真的入队（被去重则 False）。"""
        with self._lock:
            if self._stopped and etype != EV_AGENT_STOPPED:
                return False
            if dedupe_key is not None:
                if dedupe_key == self._last_key:
                    self._skipped_dup += 1
                    return False
                self._last_key = dedupe_key

        event: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "event_id": new_event_id(),
            "seq": self._next_seq(),
            "type": etype,
            "timestamp": time.time(),
            "agent": self.agent.to_wire(),
        }
        proj = project if project is not None else self.project
        if not proj.is_empty:
            event["project"] = proj.to_wire()
        if status is not None:
            event["status"] = status.value
        if task is not None and not task.is_empty:
            event["task"] = task.to_wire()
        if activity is not None and not activity.is_empty:
            event["activity"] = activity.to_wire()
        if payload:
            event["payload"] = dict(payload)

        with self._lock:
            self._emitted += 1
            self._last_event_id = event["event_id"]
        self._enqueue(event)
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> "UhaNativeAdapter":
        """UHA 正在装配后端（建 MCP / Computer Use 连接）——这段是真实的 STARTING。"""
        with self._lock:
            if self._started:
                return self
            self._started = True
        self._ensure_worker()
        self._emit(
            EV_AGENT_STARTED,
            status=Status.STARTING,
            activity=Activity(summary="UHA 正在装配执行通道"),
            payload={"core": "uah-core", "phase_label": self.phase_label},
            dedupe_key=("start", Status.STARTING.value),
        )
        self._start_heartbeat()
        return self

    def stop(self, *, reason: str = "process exit") -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._hb_stop.set()
        self._emit(
            EV_AGENT_STOPPED,
            status=None,
            activity=Activity(summary="UHA 进程退出", detail=reason),
            dedupe_key=None,
        )
        self._stop_worker()

    def _start_heartbeat(self) -> None:
        if self._heartbeat_s <= 0:
            return
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._hb_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="uah-uha-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self._heartbeat_s):
            self._emit(EV_AGENT_HEARTBEAT, status=self._current_status(), dedupe_key=None)
            if self._stopped:
                return

    # ------------------------------------------------------------------
    # 接入 UHA 的运行时
    # ------------------------------------------------------------------

    def attach_controller(self, controller: Any, *, emit_now: bool = True) -> None:
        """订阅 ``SessionController.on_change``。

        用 ``on_change`` 而不是轮询 ``snapshot()``：需求 §十八 禁止高频轮询，
        而 UHA 这个钩子本来就是事件驱动的，直接复用。
        """
        self._controller = controller
        try:
            controller.on_change(self._on_change)
        except Exception:  # noqa: BLE001 - 万一老版本没有 on_change 也不该炸
            pass
        if emit_now:
            try:
                self._on_change(controller.snapshot())
            except Exception:  # noqa: BLE001
                pass

    def detach_controller(self) -> None:
        self._controller = None

    def _on_change(self, state: Any) -> None:
        """**必须快**：只做映射与入队。这里绝不等待 I/O。"""
        try:
            phase_raw = getattr(state, "phase", None)
            phase = getattr(phase_raw, "value", phase_raw)
            phase = str(phase or "IDLE")
            with self._lock:
                self._raw_phase = phase
            self._emit_full(phase)
        except Exception:  # noqa: BLE001 - 观察者绝不能把 Agent 拖挂
            pass

    # -- 观察到的 UHA 原始信息 ---------------------------------------------

    def _observed(self) -> tuple[str, str, str]:
        """从 controller 读回 UHA 自己的原始文案：task / status_text / method。"""
        ctrl = self._controller
        if ctrl is None:
            return "", "", ""
        try:
            snap = ctrl.snapshot()
        except Exception:  # noqa: BLE001
            return "", "", ""
        return (
            str(getattr(snap, "task", "") or ""),
            str(getattr(snap, "status_text", "") or ""),
            str(getattr(snap, "method", "") or ""),
        )

    def _emit_full(self, phase: str, *, extra_detail: str | None = None) -> bool:
        """把当前(阶段 + 计划进度 + 覆盖状态)整形成一条事件发出去。"""
        mapped = UHA_PHASE_TO_STATUS.get(phase, Status.UNKNOWN)
        stage = UHA_PHASE_TO_STAGE.get(phase)
        ctrl_task, status_text, method = self._observed()

        with self._lock:
            override = self._override
            override_act = self._override_activity
            blocked = self._blocked_reason
            step = self._plan_index
            total = self._plan_total
            plan_name = self._plan_name
            plan_active = self._plan_active
            skill = self._skill

        # ★ 计划执行期间，"一步跑完"不等于"任务完成"。
        #
        # 实测：UHA 的 Executor.run() 在**每一步**结束后都会 controller.end_task()，
        # 于是 TaskPhase 在计划中途就变成 DONE。直接照搬的后果是 HUD 显示
        # DONE → RUNNING → DONE → RUNNING，而且每一步都触发一次"已完成"提醒
        # （需求 §十三 明令禁止的弹窗轰炸）。
        #
        # 语义上正确的做法：计划 = 任务。计划没跑完，任务就没完成。
        # 单步 DONE 折成 RUNNING，并把这一步的完成写进 Activity。
        if plan_active and mapped is Status.DONE:
            mapped = Status.RUNNING
            stage = "Between Steps"

        final_status = override or mapped
        if (
            blocked
            and override is None
            and final_status not in (Status.DONE, Status.ERROR, Status.CANCELLED)
        ):
            final_status = Status.BLOCKED

        # Task 那一层的名字：**有计划时以计划为准**。
        # 计划就是这次的任务；当前 skill 只是它的一步 —— 那一步该出现在
        # Activity 里（以及 Step 计数里），不该把 Task 名顶掉。
        # 反过来（skill 优先）会让卡片在每一步都换一个 Task 名，
        # 用户就没法回答"它到底在做什么任务"这个最基本的问题。
        task_name = plan_name or skill or ctrl_task or None
        tool = UHA_METHOD_TO_TOOL.get(method.upper(), method or None)

        detail_bits = [b for b in (status_text, extra_detail) if b]
        if blocked:
            detail_bits.insert(0, f"阻塞：{blocked}")

        if override_act is not None:
            activity = override_act
        else:
            # 终态时用 Stage 文案（"Finished"/"Failed"），不要继续显示"路由中"——
            # 那是**上一次**活动，放在 DONE 旁边会读成"还在路由"。
            if final_status in (Status.DONE, Status.ERROR, Status.CANCELLED) and stage:
                summary = stage
            elif plan_name:
                # 计划模式下，Activity 显示"正在跑哪一步"
                summary = skill or plan_name
            else:
                summary = status_text or stage or phase
            activity = Activity(summary=summary, detail=" · ".join(detail_bits) or None, tool=tool)

        task = TaskState(
            name=task_name,
            phase=self.phase_label,
            stage=stage,
            step=step,
            total_steps=total,
        )

        step_key = (step, total)
        with self._lock:
            step_changed = step_key != self._last_step_key
            self._last_step_key = step_key

        etype = self._event_type_for(phase, final_status, stage, step_changed=step_changed)
        return self._emit(
            etype,
            status=final_status,
            task=task,
            activity=activity,
            dedupe_key=(
                final_status, stage, step, total, activity.summary, activity.detail,
                activity.tool, blocked, phase,
            ),
        )

    @staticmethod
    def _event_type_for(phase: str, status: Status, stage: str | None, *, step_changed: bool = False) -> str:
        if status is Status.WAITING_APPROVAL:
            return EV_APPROVAL_REQUIRED
        if status is Status.WAITING_INPUT:
            return EV_USER_INPUT_REQUIRED
        if status is Status.DONE:
            return EV_TASK_COMPLETED
        if status is Status.ERROR:
            return EV_TASK_FAILED
        if status is Status.CANCELLED:
            return EV_TASK_CANCELLED
        if step_changed:
            return EV_TASK_STEP_CHANGED
        if phase in ("ANALYZING", "VERIFYING", "STRUCTURED_EXECUTION", "COMPUTER_CONTROL"):
            return EV_TASK_STAGE_CHANGED
        if phase == "IDLE":
            return EV_ACTIVITY_CHANGED
        return EV_STATUS_CHANGED

    # ------------------------------------------------------------------
    # 计划进度（旁路埋点用）
    # ------------------------------------------------------------------

    def plan_begin(self, name: str, total_steps: int | None) -> None:
        with self._lock:
            self._plan_name = name
            self._plan_total = total_steps
            self._plan_index = 0 if total_steps else None
            self._skill = None
            self._blocked_reason = None
            self._last_step_key = ()
            self._plan_active = True
        self._emit_full(self._current_phase())

    def plan_step(self, index: int, skill: str) -> None:
        with self._lock:
            self._plan_index = index
            self._skill = skill
            self._plan_active = True
        self._emit_full(self._current_phase())

    def plan_end(self, ok: bool = True) -> None:
        # 先解除"计划进行中"，再发最后一条 —— 这样这一步的 DONE 才是真的 DONE。
        with self._lock:
            self._plan_active = False
            self._plan_index = None
            self._plan_total = None
            self._skill = None
            self._last_step_key = ()
            if ok:
                self._blocked_reason = None
        self._emit_full(self._current_phase())

    def plan_is_active(self) -> bool:
        with self._lock:
            return self._plan_active

    def note_single_run(self, skill: str) -> None:
        """单 skill 运行（没有计划）：task 名就是 skill 名，没有 step 计数。"""
        with self._lock:
            self._skill = skill
            self._plan_name = None       # 上一个计划的残留必须清掉，否则 Task 名会串
            self._plan_index = None
            self._plan_total = None
            self._last_step_key = ()
            self._plan_active = False
        self._emit_full(self._current_phase())

    def _current_phase(self) -> str:
        with self._lock:
            return self._raw_phase

    def has_plan(self) -> bool:
        """当前是否处在"计划执行中"。

        不能只看 ``_plan_name``：它在 ``plan_end`` 之后仍要保留（最后那条 DONE
        事件还要用计划名当 Task），于是"跑完一个计划、接着跑单个 skill"会
        被误判成计划的又一步。用 ``_plan_active`` 才是准的。
        """
        with self._lock:
            return self._plan_active

    def next_plan_index(self) -> int:
        with self._lock:
            return (self._plan_index or 0) + 1

    # ------------------------------------------------------------------
    # 瞬态状态
    # ------------------------------------------------------------------

    def set_waiting(self, status: Status, *, summary: str, detail: str | None = None) -> None:
        """进入"等人"状态（WAITING_INPUT / WAITING_APPROVAL）。

        UHA 侧真实触发点是 CONFIRM 模式下的 ApprovalGate。
        """
        if status not in (Status.WAITING_INPUT, Status.WAITING_APPROVAL):
            raise ValueError("set_waiting 只接受 WAITING_INPUT / WAITING_APPROVAL")
        activity = Activity(summary=summary, detail=detail)
        with self._lock:
            self._override = status
            self._override_activity = activity
            step = self._plan_index
            total = self._plan_total
            name = self._skill or self._plan_name
        self._emit(
            EV_APPROVAL_REQUIRED if status is Status.WAITING_APPROVAL else EV_USER_INPUT_REQUIRED,
            status=status,
            activity=activity,
            task=TaskState(name=name, phase=self.phase_label, stage="Waiting",
                           step=step, total_steps=total),
            dedupe_key=("wait", status.value, summary, detail),
        )

    def clear_waiting(self) -> bool:
        with self._lock:
            if self._override is None:
                return False
            self._override = None
            self._override_activity = None
        self._emit_full(self._current_phase())
        return True

    def is_waiting(self) -> bool:
        with self._lock:
            return self._override is not None

    def notify_waiting_input(self, prompt: str, *, detail: str | None = None) -> None:
        self.set_waiting(Status.WAITING_INPUT, summary=prompt, detail=detail)

    def notify_waiting_approval(self, summary: str, *, detail: str | None = None) -> None:
        self.set_waiting(Status.WAITING_APPROVAL, summary=summary, detail=detail)

    def block(self, reason: str) -> None:
        with self._lock:
            self._blocked_reason = reason
        self._emit_full(self._current_phase())

    def unblock(self) -> None:
        with self._lock:
            had = self._blocked_reason is not None
            self._blocked_reason = None
        if had:
            self._emit_full(self._current_phase())

    def is_blocked(self) -> bool:
        with self._lock:
            return self._blocked_reason is not None

    def set_project(self, project: ProjectRef) -> None:
        self.project = project
        self._emit_full(self._current_phase())

    def _current_status(self) -> Status | None:
        """当前对外状态（不产生事件）。测试与心跳用。"""
        with self._lock:
            if self._override is not None:
                return self._override
            if self._blocked_reason is not None:
                return Status.BLOCKED
            phase = self._raw_phase
        if phase not in UHA_PHASE_TO_STATUS:
            return Status.UNKNOWN
        return UHA_PHASE_TO_STATUS[phase]

    def status_now(self) -> Status:
        """强制返回一个 Status（心跳 API 用）。"""
        return self._current_status() or Status.IDLE

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agent": self.agent.to_wire(),
                "seq": self._seq,
                "emitted": self._emitted,
                "skipped_duplicate": self._skipped_dup,
                "dropped": self._dropped,
                "queued": self._outbox.qsize(),
                "started": self._started,
                "stopped": self._stopped,
                "phase": self._raw_phase,
                "plan": {"name": self._plan_name, "step": self._plan_index,
                         "total": self._plan_total, "active": self._plan_active},
                "override": self._override.value if self._override else None,
                "blocked": self._blocked_reason,
                "last_event_id": self._last_event_id,
                "publisher": self.publisher.delivered(),
            }


# ---------------------------------------------------------------------------
# 运行时埋点（旁路观察，行为零改动）
# ---------------------------------------------------------------------------


def attach_plan_progress(adapter: UhaNativeAdapter, executor: Any) -> dict[str, Any]:
    """Subscribe to the Executor's public progress API; never replace methods."""
    subscribe = getattr(executor, "add_progress_listener", None)
    if not callable(subscribe):
        return {"progress_hook": False, "reason": "Executor has no progress API"}
    def progress(event, **fields):
        if event == "begin":
            adapter.plan_begin(fields["name"], fields["total"])
        elif event == "step":
            adapter.plan_step(fields["index"], fields["skill"])
        elif event == "end":
            adapter.plan_end(fields["ok"])
        elif event == "single":
            adapter.note_single_run(fields["skill"])
    subscribe(progress)
    return {"progress_hook": True, "wrapped_run_plan": False, "wrapped_run": False}


def attach_approval_observer(adapter: UhaNativeAdapter, executor: Any) -> dict[str, Any]:
    """包裹 ``ApprovalGate.evaluate``，把"正在等人批"这件事报出来。

    **行为零改动**：``evaluate`` 的返回值原样返回，异常原样抛出。
    """
    gate = getattr(executor, "approval", None)
    original = getattr(gate, "evaluate", None)
    if not callable(original):
        return {"wrapped_evaluate": False}

    confirmer = getattr(gate, "_confirmer", None)
    if confirmer is not None:
        def observed_confirm(request):
            summary = str(getattr(request, "summary", "") or getattr(request, "action", "approval"))
            adapter.set_waiting(Status.WAITING_APPROVAL, summary=f"等待批准：{summary}")
            try:
                return confirmer(request)
            finally:
                adapter.clear_waiting()
        gate._confirmer = observed_confirm

    def wrapped_evaluate(request: Any) -> Any:
        has_confirmer = getattr(gate, "_confirmer", None) is not None
        summary = str(
            getattr(request, "summary", None) or getattr(request, "action", "") or "approval"
        )
        result = original(request)
        decision = getattr(getattr(result, "decision", None), "value", "")
        if has_confirmer:
            adapter.clear_waiting()
            if decision == "DENY":
                adapter.block(f"审批被拒绝：{summary}")
        elif decision == "REQUIRE_CONFIRMATION":
            # 策略要求确认、但没有人能应答。这是真的"卡在等人"，
            # 而且用户非常需要看到它 —— UHA 自己只会把任务判失败。
            adapter.set_waiting(
                Status.WAITING_APPROVAL,
                summary=f"需要批准但无人应答：{summary}",
                detail="未配置 confirmer：任务将被判失败（CONFIRM 模式的已知限制）",
            )
            adapter.block("等待人工批准（当前无交互式审批通道）")
        return result

    wrapped_evaluate._uah_original = original  # type: ignore[attr-defined]
    gate.evaluate = wrapped_evaluate
    return {"wrapped_evaluate": True, "gate_mode": getattr(getattr(gate, "mode", None), "value", None)}


__all__ = [
    "UhaNativeAdapter",
    "attach_plan_progress",
    "attach_approval_observer",
    "UHA_PHASE_TO_STATUS",
    "UHA_PHASE_TO_STAGE",
    "UHA_METHOD_TO_TOOL",
]
