"""执行器 —— 把 Router / Fallback / Validation / Logging 缝成一个闭环。

一次任务的完整生命周期（每一步都会进日志）：

    1. skill.intent()            把参数变成结构化意图
    2. router.decide()           选首选方式 + 给出理由 + 给出备用顺序
    3. skill.preflight()         采基线（用来做前后对比）
    4. skill.perform()           执行
    5. skill.verify()            独立复验（重新读状态，不信任写入返回值）
    6. 不通过 / 抛错 -> fallback  换下一种方式，回到 4
    7. 全部失败 -> 如实返回失败，并把每一轮的原因都留在 attempts 里

三个刻意选择：

* **验证不通过 = 失败**，和"抛异常"同级对待。自动化里最危险的状态是
  "接口说成功了、实际没生效"，所以验证不通过必须触发降级，而不是记个警告就算了。
* **降级有硬预算**（每方法 2 次、总 5 次），避免在一条注定不通的路上耗到超时。
* **失败原因结构化**（用 error.code 而不是错误字符串），跨版本、跨语言都稳定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core.config import Config
from ..core.errors import NoViableMethod, PreconditionFailed, UHAError
from ..core.log import RunLogger
from ..core.session_control import TaskPhase
from ..router.fallback import FallbackManager, MethodHealth
from ..router.router import Decision, Router
from ..runtime import BackendBundle
from ..skills.base import Skill, SkillContext, SkillResult, SkillTrace
from ..skills.registry import get_skill


@dataclass
class _AttemptOutcome:
    """一次尝试的内部产出：给日志看的记录 + 给调用方看的数据与验证报告。"""

    record: "ExecutionRecord"
    data: dict[str, Any] | None = None
    report: Any = None


@dataclass
class ExecutionRecord:
    """一轮尝试的记录。"""

    method: str
    ok: bool
    stage: str = ""            # perform / verify
    error_code: str | None = None
    error: str | None = None
    verification: dict[str, Any] | None = None
    verification_summary: str | None = None
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "ok": self.ok,
            "stage": self.stage,
            "error_code": self.error_code,
            "error": self.error,
            "verification": self.verification,
            "verification_summary": self.verification_summary,
            "duration_ms": round(self.duration_ms, 2),
        }


class Executor:
    """按 skill 执行一次任务，自动选路 + 自动降级 + 自动验证。"""

    def __init__(
        self,
        bundle: BackendBundle,
        config: Config,
        logger: RunLogger,
        *,
        router: Router | None = None,
        fallback: FallbackManager | None = None,
    ):
        self._progress_listeners = []
        self._progress_plan_depth = 0
        self.bundle = bundle
        self.config = config
        self.logger = logger
        if getattr(bundle, "gateway", None) is not None:
            bundle.gateway.bind_logger(logger)
        health_path = config.path("router.stats_file")
        self.health = MethodHealth(
            health_path,
            cooldown_s=float(config.get("router.method_cooldown_s", 60)),
            fail_threshold=int(config.get("router.max_failures_per_method", 2)),
        )
        self.router = router or Router(
            weights=config.get("router.weights"),
            health=self.health,
            capability_ttl_s=float(config.get("router.capability_ttl_s") or 20.0),
        )
        self.fallback = fallback or FallbackManager(
            max_attempts_per_method=int(config.get("router.max_attempts_per_method", 2)),
            max_total_fallbacks=int(config.get("router.max_total_fallbacks", 5)),
        )
        self.artifacts_dir = config.path("workspace.artifact_dir", ensure_parent=True)
        # --- Phase 4A: capability cache + approval gate ---
        from ..core.execution_mode import ExecutionMode, build_approval_gate
        from ..router.capability_cache import get_capability_cache
        from ..router.quality import RouterQualityTracker
        from ..core.session_control import get_controller
        from ..core.session_locks import get_coordinator, requires_write_lock

        self.capability_cache = get_capability_cache(
            float(config.get("router.capability_ttl_s") or 20.0)
        )
        if getattr(self.router, "capability_cache", None) is None:
            self.router.capability_cache = self.capability_cache
        self.execution_mode = ExecutionMode.parse(config.get("execution.mode"))
        self.approval = build_approval_gate(config, mode=self.execution_mode)
        self.quality = RouterQualityTracker(config.path("router.quality_file"))
        self.controller = get_controller()
        self.coordinator = get_coordinator()
        self._requires_write_lock = requires_write_lock
        self.performance = {
            "router_decision_ms": [],
            "mcp_latency_ms": [],
            "write_lock_wait_ms": [],
        }

    def add_progress_listener(self, listener):
        if listener not in self._progress_listeners:
            self._progress_listeners.append(listener)

    def _progress(self, event, **fields):
        for listener in tuple(self._progress_listeners):
            try:
                listener(event, **fields)
            except Exception:
                pass  # Observers cannot change execution results.

    def set_execution_mode(self, mode: str) -> None:
        from ..core.execution_mode import ExecutionMode

        self.execution_mode = ExecutionMode.parse(mode)
        self.approval.set_mode(self.execution_mode)

    # -- 探测 ----------------------------------------------------------------

    def probe(self, *, use_cache: bool = True, force: bool = False) -> dict[str, bool]:
        gui = self.bundle.gui_available()
        return self.router.available_methods(
            backends=self.bundle.structured_backends(),
            computer_use_available=gui,
            vision_available=gui,
            use_cache=use_cache,
            force_probe=force,
        )

    # -- 主流程 --------------------------------------------------------------

    def run(
        self,
        skill: Skill,
        params: Mapping[str, Any],
        *,
        intent: Any = None,
        prefer_method: str | None = None,
    ) -> SkillResult:
        if not self._progress_plan_depth:
            self._progress("single", skill=skill.name)
        t0 = time.perf_counter()
        params = dict(params or {})
        intent = intent if intent is not None else skill.intent(params)
        task_cat = getattr(skill, "task_category", lambda p=None: "general")(params)

        # 会话控制：任务级标注；中止则不再路由
        if self.controller.should_abort():
            return SkillResult(
                skill=skill.name, ok=False,
                error=f"任务已中止（{self.controller.snapshot().abort_reason}）",
                duration_ms=0.0,
            )
        # Pause/Stop 状态下禁止开启新的 mutation / GUI 写任务
        write_like = (
            task_cat in ("actor_mutation", "batch_mutation", "level_save", "gui_interaction")
            or not bool(getattr(skill, "is_idempotent", True))
        )
        if write_like and self.controller.blocks_writes():
            snap = self.controller.snapshot()
            return SkillResult(
                skill=skill.name, ok=False,
                error=f"控制状态阻止写/键鼠：{snap.phase.value}/{snap.command.value}",
                duration_ms=0.0,
            )
        # Phase 4A: risk-based approval gate before write-like execution
        if write_like and not self.config.is_dry_run():
            from ..core.execution_mode import ApprovalRequest, ExecutionMode

            mode = ExecutionMode.parse(params.get("mode") or params.get("execution_mode"))
            if params.get("mode") or params.get("execution_mode"):
                self.approval.set_mode(mode)
            n_batch = int(params.get("batch_size") or len(params.get("targets") or params.get("actors") or []) or 1)
            if task_cat == "batch_mutation":
                risk_level = "medium" if n_batch <= int(self.approval.auto_allow_max_batch) else "high"
            else:
                risk_level = str(params.get("risk_level") or "medium")
            req = ApprovalRequest(
                task_id=str(params.get("task_id") or skill.name),
                action=skill.name,
                mode=self.approval.mode,
                risk_level=risk_level,
                reasons=[],
                summary=f"{skill.name} via Router",
                batch_size=n_batch,
                reversible=True if bool(params.get("checkpoint", True)) else bool(
                    getattr(skill, "is_idempotent", lambda p=None: False)(params)
                ),
                rollback_planned=bool(params.get("checkpoint", True)),
                verify_planned=True,
                semantic_modes=[str(x) for x in (params.get("semantic_modes") or [])],
                extras={
                    "read_only": False,
                    "large_batch": n_batch > int(self.config.get("safety.approval_batch_threshold") or 5),
                    "delete_actor": bool(params.get("delete")),
                },
            )
            try:
                approval = self.approval.require(req)
                params = dict(params)
                params["_approval_gate"] = self.approval
                params["_approval"] = approval.as_dict()
                params.setdefault("execution_mode", self.approval.mode.value)
            except PreconditionFailed as exc:
                # Approval reject is not a session-wide abort; do not leave FAILED sticky.
                self.controller.acknowledge_control()
                return SkillResult(
                    skill=skill.name, ok=False,
                    error=str(exc),
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    data={"approval": getattr(exc, "details", None)},
                )
        self.controller.begin_task(skill.name)
        self.controller.set_step(skill.name, status="路由中")

        ctx = SkillContext(
            bundle=self.bundle,
            config=self.config,
            logger=self.logger,
            artifacts_dir=self.artifacts_dir,
            dry_run=self.config.is_dry_run(),
        )

        attempts: list[ExecutionRecord] = []
        availability: dict[str, bool] = {}
        selected_method = ""
        primary = ""

        # --- 1. 路由 ---------------------------------------------------------
        try:
            t_route = time.perf_counter()
            availability = self.probe(use_cache=True)
            decision = self.router.decide(
                intent,
                backends=self.bundle.structured_backends(),
                computer_use_available=availability.get("MOUSE", False),
                vision_available=availability.get("VISION", False),
                task_category=task_cat,
                use_capability_cache=True,
            )
            self.performance["router_decision_ms"].append((time.perf_counter() - t_route) * 1000)
        except NoViableMethod as exc:
            self.logger.error(f"没有可用执行方式：{exc}")
            note = self._cooldown_note(availability)
            self.controller.end_task(ok=False)
            return SkillResult(
                skill=skill.name, ok=False, error=str(exc) + note,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

        selected_method = decision.method
        order = [decision.method] + decision.fallback_order
        self.logger.event(
            "routing",
            skill=skill.name,
            intent=intent.as_dict(),
            selected_method=decision.method,
            reason=decision.reason,
            rule=decision.rule,
            score=decision.score,
            alternatives=decision.alternatives,
            order=order,
            availability=availability,
        )

        # --- 按 skill 能力过滤候选 ------------------------------------------
        # Router 只知道"通道是否在线"，不知道"这个 skill 认不认这种 method"。
        # 实测：actor_find 的降级链会排进 HYBRID/KEYBOARD，然后全部 NOT_SUPPORTED
        # ——白耗预算、日志噪音大，还容易被误读成通道故障。
        supported = getattr(skill, "supported_methods", None)
        if supported:
            filtered = [m for m in order if m in supported]
            dropped = [m for m in order if m not in supported]
            if dropped:
                self.logger.event(
                    "method_filter",
                    skill=skill.name,
                    dropped=dropped,
                    why="skill 不支持这些执行方式（结构性，不是通道故障）",
                )
            order = filtered
            if not order:
                # fail-open to any currently-available method the skill supports
                inject = [m for m, ok in availability.items() if ok and m in supported]
                if inject:
                    self.logger.event(
                        "method_filter_reinject",
                        skill=skill.name,
                        injected=inject,
                        why="Router 首选/降级链均不受 skill 支持，改用探测可用且 skill 支持的通道",
                    )
                    order = inject
                else:
                    note = self._cooldown_note(availability)
                    return SkillResult(
                        skill=skill.name,
                        ok=False,
                        error=(
                            f"skill {skill.name} 没有任何可用且受支持的执行方式"
                            f"（Router 候选均已过滤；支持：{sorted(supported)}）" + note
                        ),
                        duration_ms=(time.perf_counter() - t0) * 1000,
                    )

        # --- 步骤级的"建议方式" ----------------------------------------------
        # 只重排顺序，**不删除**降级链：建议失败后照样按 Router 的顺序继续降级。
        # 建议的方式不在候选里（不可用/冷却中/不被 skill 支持）就忽略并记账，绝不硬闯。
        primary = order[0]
        primary_reason = decision.reason
        if prefer_method:
            preferred = str(prefer_method)
            if preferred == decision.method:
                self.logger.event("method_preference", wanted=preferred, result="already_selected")
            elif preferred in order:
                order = [preferred] + [m for m in order if m != preferred]
                primary = order[0]
                primary_reason = (
                    f"步骤显式建议 {preferred}（Router 本来会选 {decision.method}）"
                )
                self.logger.event(
                    "method_preference", wanted=preferred, result="reordered",
                    router_would_have_chosen=decision.method, order=order,
                )
            else:
                self.logger.event(
                    "method_preference", wanted=preferred, result="ignored_not_available",
                    router_would_have_chosen=decision.method, order=order,
                    availability=availability,
                )

        # --- dry-run：只选路，不 perform ------------------------------------
        # CLI 文档写的是「--dry-run 只走选路不真执行」。此前只把标志塞进
        # config/skill ctx，但 skill.perform 与后端并未统一短路，
        # dry-run 仍会发起真实 MCP 调用，甚至落到 UE_COMMANDLET 去起引擎。
        if self.config.is_dry_run():
            self.logger.event(
                "dry_run_stop",
                skill=skill.name,
                selected_method=primary,
                order=order,
                reason=primary_reason,
                note="dry-run：已完成选路，未执行 perform",
            )
            self.controller.set_method(primary)
            self.controller.end_task(ok=True)
            return SkillResult(
                skill=skill.name,
                ok=True,
                method=primary,
                data={
                    "dry_run": True,
                    "decision": decision.as_dict(),
                    "order": order,
                    "availability": availability,
                },
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

        # --- 2. 依次尝试 ------------------------------------------------------
        self.fallback.reset()
        self.controller.set_method(primary)

        last_error: str | None = None
        last_attempted: str | None = None
        for index, method in enumerate(order):
            if self.controller.should_abort():
                self.controller.end_task(ok=False)
                return SkillResult(
                    skill=skill.name, ok=False,
                    error=f"任务已中止（{self.controller.snapshot().abort_reason}）",
                    attempts=[a.as_dict() for a in attempts],
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
            if not self.controller.wait_if_paused(timeout=1.0):
                self.controller.end_task(ok=False)
                return SkillResult(
                    skill=skill.name, ok=False,
                    error="任务在暂停期间被停止",
                    attempts=[a.as_dict() for a in attempts],
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
            if not self.fallback.can_try(method):
                self.logger.event("fallback_skip", method=method, why="该方法已判定不可再试或超出重试上限")
                continue
            if last_attempted is not None:
                if not self.fallback.has_budget():
                    self.logger.event("fallback_budget_exhausted", method=method)
                    break
                self.fallback.note_fallback()
                self.logger.event(
                    "fallback",
                    skill=skill.name,
                    from_method=last_attempted,
                    to_method=method,
                    attempt=index,
                )

            self.controller.set_method(method)
            self.controller.set_step(skill.name, method=method, status="执行中")
            outcome = self._attempt(
                ctx, skill, method, params, decision,
                primary=primary, primary_reason=primary_reason,
                task_category=task_cat,
            )
            attempts.append(outcome.record)
            last_attempted = method

            if outcome.record.ok:
                self._record_quality(skill.name, task_cat, selected_method or primary, method, True, len(attempts))
                self.controller.end_task(ok=True)
                return SkillResult(
                    skill=skill.name,
                    ok=True,
                    method=method,
                    attempts=[a.as_dict() for a in attempts],
                    data=outcome.data or {},
                    verification=outcome.report,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )

            record = outcome.record
            last_error = record.error or record.verification_summary or "未知失败"
            if self.fallback.is_fatal(record.error_code):
                self.logger.event("method_exhausted", method=method, error_code=record.error_code)

        self._record_quality(skill.name, task_cat, selected_method or primary, last_attempted, False, len(attempts))
        self.controller.end_task(ok=False)
        return SkillResult(
            skill=skill.name,
            ok=False,
            method="",
            attempts=[a.as_dict() for a in attempts],
            error=(last_error or "所有执行方式均失败") + self._cooldown_note(availability),
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    def _record_quality(
        self, skill_name: str, task_cat: str, selected: str, final: str | None,
        ok: bool, attempts: int,
    ) -> None:
        try:
            self.quality.record_outcome(
                skill_type=task_cat or skill_name,
                selected_method=selected,
                final_method=final,
                ok=ok,
                attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.event("quality_record_error", error=str(exc)[:200])

    def _cooldown_note(self, availability: Mapping[str, bool]) -> str:
        """失败时如果"更该干这活"的通道正被冷却，把这件事说出来。

        否则失败信息会兴师问罪到最后一个尝试的方法头上——实测过一次：
        真正的病因是"UNREAL_MCP 在冷却期"，但报出来的是最后兜底的 GUI 通道
        在抱怨"world_outliner 的 ROI 没标定"，完全带偏排查方向。
        """
        try:
            cooled = sorted(m for m in (availability or {}) if self.health.in_cooldown(m))
        except Exception:  # noqa: BLE001 - 诊断信息不该影响失败本身
            return ""
        if not cooled:
            return ""
        self.logger.warn(
            f"以下通道正处于冷却期（近期连续失败），本次根本没用上：{', '.join(cooled)}"
            " —— 失败原因可能只是通道健康度，而不是本步骤参数不对"
        )
        return (
            f"（注：{', '.join(cooled)} 正处于冷却期被排除，"
            "本次失败可能只是通道健康度问题，而不是参数不对；"
            "可先 `uha stats` 看账、必要时 `uha stats --reset` 销账）"
        )

    # -- 单轮 ----------------------------------------------------------------

    def _attempt(
        self,
        ctx: SkillContext,
        skill: Skill,
        method: str,
        params: Mapping[str, Any],
        decision: Decision,
        *,
        primary: str | None = None,
        primary_reason: str | None = None,
        task_category: str | None = None,
    ) -> "_AttemptOutcome":
        head = primary or decision.method
        reason = (
            (primary_reason or decision.reason) if method == head
            else f"{method}: 上一次方式失败后的降级尝试"
        )
        trace = SkillTrace()
        report = None
        t0 = time.perf_counter()
        idempotent = self._is_idempotent(skill, params)
        is_write = self._requires_write_lock(skill.name) or not idempotent
        is_gui = method in ("MOUSE", "KEYBOARD")
        if is_gui:
            # P0 safety: banner + gate before any CU input
            try:
                from ..safety.controller import get_safety_controller

                safety = getattr(self, "_safety", None) or get_safety_controller()
                if safety.state.value == "EMERGENCY_STOP":
                    raise PreconditionFailed("Emergency Stop active; Computer Use blocked")
                snap = safety.snapshot()
                if snap.state.value in ("IDLE", "RELEASED"):
                    safety.request_control(task=f"{skill.name}:{method}")
                if not safety.snapshot().banner_visible:
                    safety.banner_visible(ok=True)
            except PreconditionFailed:
                raise
            except Exception:
                pass

        self.controller.set_step(skill.name, method=method, status="执行中")

        def _phase_for(m: str) -> str:
            if m in ("MOUSE", "KEYBOARD"):
                return "COMPUTER_CONTROL"
            return "STRUCTURED_EXECUTION"

        with self.logger.step(skill.name, method=method, reason=reason) as rec:
            try:
                if self.controller.blocks_writes() and (is_write or is_gui):
                    raise RuntimeError(f"控制状态阻止写/键鼠：{self.controller.snapshot().phase.value}")
                # 分层锁：写串行；GUI 写再加桌面锁
                lock_cm = None
                if is_gui and is_write:
                    self.controller.set_phase(TaskPhase.COMPUTER_CONTROL, controls_mouse=True)
                    lock_cm = self.coordinator.gui_mutation(op=skill.name, holder=f"{skill.name}:{method}")
                elif is_write:
                    self.controller.set_phase(TaskPhase.STRUCTURED_EXECUTION)
                    tw = time.perf_counter()
                    lock_cm = self.coordinator.write(op=skill.name, holder=f"{skill.name}:{method}")
                elif is_gui:
                    self.controller.set_phase(TaskPhase.COMPUTER_CONTROL, controls_mouse=True)
                    lock_cm = self.coordinator.desktop(holder=f"{skill.name}:{method}")
                else:
                    self.controller.set_phase(TaskPhase.ANALYZING)
                    lock_cm = self.coordinator.read(op=skill.name)

                try:
                    trace.before = skill.preflight(ctx, params) or {}
                    rec.set(arguments=_clip(params))
                except Exception as exc:  # noqa: BLE001
                    rec.meta(preflight_error=f"{type(exc).__name__}: {exc}")
                    trace.note(f"基线采集失败：{type(exc).__name__}: {exc}")

                perform_error: Exception | None = None
                data: dict[str, Any] | None = None
                try:
                    with lock_cm:
                        if is_write or is_gui:
                            # 会话控制器二次闸门
                            self.controller.ensure_writable(f"{skill.name}:{method}")
                        data = skill.perform(ctx, method, params, trace)
                        rec.set(tool=_short_transport(data), result="success")
                except UHAError as exc:
                    perform_error = exc
                    rec.set(result="error", error=exc.to_dict())
                except Exception as exc:  # noqa: BLE001
                    perform_error = exc
                    rec.set(result="error", error={"type": type(exc).__name__, "message": str(exc)})

                if perform_error is not None:
                    code = getattr(perform_error, "code", "UNEXPECTED")
                    if self.controller.should_abort():
                        return _AttemptOutcome(ExecutionRecord(
                            method=method, ok=False, stage="perform",
                            error_code="ABORTED_BY_USER", error=str(perform_error),
                            duration_ms=(time.perf_counter() - t0) * 1000,
                        ))
                    confirmed = self._confirm_side_effect(ctx, skill, method, params, trace, rec)
                    if confirmed is not None:
                        report = confirmed
                        rec.meta(confirmed_after_error=f"{method} 抛错但状态已满足 —— 判成功，不重试")
                        return self._success(skill, method, report, data=trace.after.get("transform") or {}, t0=t0, rec=rec, task_category=task_category)
                    self._record_failure(method, code, str(perform_error), t0, task_category=task_category)
                    return _AttemptOutcome(ExecutionRecord(
                        method=method, ok=False, stage="perform",
                        error_code=code, error=str(perform_error),
                        duration_ms=(time.perf_counter() - t0) * 1000,
                    ))

                self.controller.set_phase(TaskPhase.VERIFYING)
                try:
                    report = skill.verify(ctx, method, params, trace)
                except Exception as exc:  # noqa: BLE001
                    report = None
                    rec.meta(verify_error=f"{type(exc).__name__}: {exc}")

                if report is not None:
                    rec.set(verification=report.as_dict())

                if report is not None and not report.passed:
                    confirmed = None if idempotent else self._confirm_side_effect(
                        ctx, skill, method, params, trace, rec
                    )
                    if confirmed is not None:
                        report = confirmed
                        rec.set(verification=report.as_dict())
                        rec.meta(non_idempotent_confirmed=f"{method} 验证不通过但状态已生效 —— 判成功")
                        return self._success(skill, method, report, data=data, t0=t0, rec=rec, task_category=task_category)
                    rec.set(result="unverified")
                    self._record_failure(method, "VERIFICATION_FAILED", report.summary(), t0, task_category=task_category)
                    return _AttemptOutcome(
                        ExecutionRecord(
                            method=method, ok=False, stage="verify",
                            error_code="VERIFICATION_FAILED",
                            error="验证不通过：" + "；".join(c.line() for c in report.failures[:3]),
                            verification=report.as_dict(),
                            verification_summary=report.summary(),
                            duration_ms=(time.perf_counter() - t0) * 1000,
                        ),
                        data=data,
                    )

                return self._success(skill, method, report, data=data or {}, t0=t0, rec=rec, task_category=task_category)
            finally:
                if is_gui:
                    self.controller.set_phase(TaskPhase.ANALYZING, controls_mouse=False)

    # -- 辅助 ----------------------------------------------------------------

    @staticmethod
    def _is_idempotent(skill: Skill, params: Mapping[str, Any]) -> bool:
        """问 skill：重复执行是否与一次相同。拿不准当作**不可幂等**（False）。"""
        fn = getattr(skill, "is_idempotent", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(params))
        except Exception:  # noqa: BLE001
            return False

    def _confirm_side_effect(
        self,
        ctx: SkillContext,
        skill: Skill,
        method: str,
        params: Mapping[str, Any],
        trace: SkillTrace,
        rec: Any,
    ) -> Any:
        """重新读一次状态，确认目标是否达成。返回通过的报告或 None。"""
        try:
            again = skill.verify(ctx, method, params, trace)
        except Exception as exc:  # noqa: BLE001
            rec.meta(confirm_error=f"{type(exc).__name__}: {exc}")
            return None
        if again is not None and again.passed:
            return again
        rec.meta(
            confirm_result="重新读状态后仍不满足目标状态 —— 视为真失败，允许降级重试"
        )
        return None

    def _success(
        self, skill: Skill, method: str, report: Any, *, data: dict[str, Any], t0: float, rec: Any,
        task_category: str | None = None,
    ) -> "_AttemptOutcome":
        self.health.record(method, ok=True, task_category=task_category)
        self.router.feedback(method, ok=True)
        self.fallback.record(method, ok=True, duration_ms=(time.perf_counter() - t0) * 1000)
        return _AttemptOutcome(
            ExecutionRecord(
                method=method, ok=True,
                verification=report.as_dict() if report else None,
                verification_summary=report.summary() if report else "未做验证",
                duration_ms=(time.perf_counter() - t0) * 1000,
            ),
            data=data,
            report=report,
        )

    def _record_failure(
        self, method: str, code: str, detail: str, t0: float, *, task_category: str | None = None
    ) -> None:
        if code == "UNEXPECTED":
            self.logger.error(
                f"{method} 抛出非预期异常 —— 疑似本地代码缺陷，不计入通道健康度：{detail}"
            )
            return

        # ② `NOT_SUPPORTED` = "这个通道本来就不做这类事"（例如让 HYBRID 去做只读巡检）。
        # 那是**任务与通道不匹配**，不是通道坏。记账进健康度会自我放大：
        # 优选通道被冷却 -> 只能退到不匹配的通道 -> 它们也逐个被判"不健康"
        # -> 下一次连一个可用方式都选不出来（真实踩过，见 docs/FINDINGS.md#f5）。
        # 所以：本次任务内它照样不能再用（避免原地重试），但不进健康度、不拉低成功率。
        if code == "NOT_SUPPORTED":
            self.logger.event(
                "health_skip", method=method, error_code=code,
                why="任务与通道不匹配（结构性），不计入通道健康度与成功率",
                detail=str(detail)[:200],
            )
            self.fallback.record(
                method, ok=False, error_code=code, detail=detail,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
            return
        # 健康度只写一次；Router.feedback 只是通知钩子，不再落账。
        self.health.record(method, ok=False, error_code=code, task_category=task_category)
        self.router.feedback(method, ok=False, error_code=code)
        self.fallback.record(
            method, ok=False, error_code=code, detail=detail,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    # -- 计划执行 ------------------------------------------------------------

    def run_plan(self, plan: Any) -> "PlanResult":
        self._progress_plan_depth += 1
        self._progress("begin", name=plan.name, total=len(plan.steps))
        ok = False
        try:
            result = self._run_plan(plan)
            ok = result.ok
            return result
        finally:
            self._progress_plan_depth -= 1
            self._progress("end", ok=ok)

    def _run_plan(self, plan: Any) -> "PlanResult":
        """按顺序跑完一个计划。

        步骤之间共享一个 ``shared`` 上下文，支持 ``@名字`` 占位符：

            visual_inspect 产出 floating_top -> 下一步的 actor 写 "@floating_top"

        这样"先看再改"的链路不需要人肉把结论抄进参数里。
        """
        shared: dict[str, Any] = {}
        outcomes: list[StepOutcome] = []
        t0 = time.perf_counter()

        self.logger.event("plan_start", plan=plan.as_dict())
        for warning in (plan.meta or {}).get("warnings", []):
            self.logger.warn(f"plan 提示：{warning}")
        self.logger.info(plan.describe())

        for index, step in enumerate(plan.steps, 1):
            self._progress("step", index=index, skill=step.skill)
            params = _resolve_placeholders(step.params, shared, step=step)
            self.logger.info(f"[{index}/{len(plan.steps)}] {step.skill} {params}")
            skill = get_skill(step.skill)
            result = self.run(skill, params, prefer_method=step.prefer_method)
            outcomes.append(StepOutcome(
                step_id=step.id, skill=step.skill, ok=result.ok,
                method=result.method, summary=result.summary(), result=result,
            ))
            self._absorb(shared, step.skill, result)

            if not result.ok and not step.optional:
                self.logger.error(f"计划在第 {index} 步失败：{result.summary()}")
                return PlanResult(
                    plan_name=plan.name, ok=False, steps=outcomes, shared=shared,
                    error=f"step {index} ({step.skill}) failed: {result.error}",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
            if not result.ok:
                self.logger.warn(f"可选步骤 {step.skill} 失败，继续执行：{result.error}")

        return PlanResult(
            plan_name=plan.name, ok=True, steps=outcomes, shared=shared,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )

    @staticmethod
    def _absorb(shared: dict[str, Any], skill_name: str, result: SkillResult) -> None:
        """把一步的产出摊进共享上下文，供后续步骤用 ``@占位符`` 引用。"""
        shared[f"{skill_name}.ok"] = result.ok
        shared[f"{skill_name}.method"] = result.method
        if not result.ok:
            return

        evidence = (result.data or {}).get("evidence") or {}
        floating = evidence.get("floating")
        if floating:
            shared["floating"] = floating
            candidates = floating.get("candidates") or []
            # 可信度闸门：判浮空的方法本身可能失准（见 vision/judge.py 的实测说明）。
            # 结论不可执行时，**照样把数据摊出来供人看**，但给占位符上闸——
            # 否则"整栋楼被判浮空"会真的触发自动搬运，把场景改坏。
            #
            # `actionable` 比 `reliable` 更严：统计地面即便算出低占比也不算可执行，
            # 因为无法确认场景是不是多层的。老数据没有 `actionable` 就退回看 `reliable`。
            allowed = floating.get("actionable", floating.get("reliable", True))
            if allowed:
                if candidates:
                    shared["floating_top"] = candidates[0].get("label")
                    shared["floating_top_gap"] = candidates[0].get("gap")
            else:
                reason = floating.get("warning") or "浮空判定不可执行（被判浮空占比过高，或地面是猜的）"
                blocked = shared.setdefault("blocked_placeholders", {})
                for name in ("floating_top", "floating_top_gap"):
                    blocked[name] = reason
                shared["floating_unreliable"] = True

        actor = (result.data or {}).get("actor")
        if isinstance(actor, Mapping):
            shared["actor"] = actor.get("label") or actor.get("name")
            shared["actor.location"] = actor.get("location")

    def fallback_state(self) -> dict[str, Any]:
        return self.fallback.state.as_dict()


@dataclass
class StepOutcome:
    """计划里一步的结果。"""

    step_id: str
    skill: str
    ok: bool
    method: str = ""
    summary: str = ""
    result: SkillResult | None = None

    def as_dict(self, *, full: bool = False) -> dict[str, Any]:
        out = {
            "step": self.step_id,
            "skill": self.skill,
            "ok": self.ok,
            "method": self.method,
            "summary": self.summary,
        }
        if full and self.result is not None:
            out["result"] = self.result.as_dict()
        return out


@dataclass
class PlanResult:
    """整个计划的结果。"""

    plan_name: str
    ok: bool
    steps: list[StepOutcome] = field(default_factory=list)
    shared: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan_name,
            "ok": self.ok,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "steps": [s.as_dict() for s in self.steps],
            "shared": self.shared,
        }

    def describe(self) -> str:
        lines = [f"计划 {self.plan_name}：{'全部成功' if self.ok else '失败'}"
                 + (f"（{self.error}）" if self.error else "")]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"  {i}. [{'OK' if s.ok else 'FAIL'}] {s.summary}")
        return "\n".join(lines)


def _resolve_placeholders(
    params: Mapping[str, Any], shared: Mapping[str, Any], *, step: Any = None
) -> dict[str, Any]:
    """把 ``"@floating_top"`` / ``"@shared.x"`` 换成共享上下文里的真实值。

    占位符解析不出来就**报错**，不做"默默用 0 代替"——
    那会变成一次静默的错误操作。

    另外，来自"不可信判断"的占位符会先被 :data:`shared["blocked_placeholders"]`
    拦下：宁可让这一步失败得清清楚楚，也不能拿一个已知失准的结论去改场景。
    """
    out: dict[str, Any] = {}
    blocked = shared.get("blocked_placeholders") or {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith("@"):
            ref = value[1:]
            if ref in blocked:
                raise PreconditionFailed(
                    f"占位符 {value!r} 被安全闸拦下：{blocked[ref]}",
                    details={"placeholder": value, "reason": blocked[ref], "blocked": sorted(blocked)},
                )
            if ref not in shared:
                raise PreconditionFailed(
                    f"占位符 {value!r} 无法解析（共享上下文里没有 {ref!r}）"
                    + (f"；步骤 {getattr(step, 'id', '?')}" if step else "")
                    + f"；当前可用：{sorted(shared)}",
                    details={"placeholder": value, "available": sorted(shared)},
                )
            out[key] = shared[ref]
        elif value is None:
            continue
        else:
            out[key] = value
    return out


def _clip(params: Mapping[str, Any], limit: int = 300) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        text = repr(v)
        out[k] = v if len(text) <= limit else text[:limit] + "…"
    return out


def _short_transport(data: Any) -> str:
    if isinstance(data, Mapping):
        for key in ("transport", "_transport", "_tool", "action", "transport"):
            val = data.get(key)
            if val:
                return str(val)
        if data.get("ok") is False:
            return "unknown"
    return "-"


__all__ = ["Executor", "ExecutionRecord", "StepOutcome", "PlanResult"]
