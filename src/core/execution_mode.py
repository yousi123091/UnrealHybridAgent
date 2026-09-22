"""ExecutionMode + Risk-based ApprovalGate (Phase 4A).

AUTO: auto-execute safe, reversible, deterministic ops; pause on high risk.
CONFIRM: human confirms risky/batch mutations; reads still auto.

Approval decisions: ALLOW | REQUIRE_CONFIRMATION | DENY
Gate is risk-based, not per-step popups.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class ExecutionMode(str, enum.Enum):
    AUTO = "AUTO"
    CONFIRM = "CONFIRM"

    @classmethod
    def parse(cls, raw: str | None, default: "ExecutionMode | None" = None) -> "ExecutionMode":
        d = default or ExecutionMode.CONFIRM
        if raw is None or str(raw).strip() == "":
            return d
        val = str(raw).strip().upper()
        aliases = {
            "AUTO": cls.AUTO,
            "FULL_AUTO": cls.AUTO,
            "AUTONOMOUS": cls.AUTO,
            "FULL_AUTONOMOUS": cls.AUTO,
            "CONFIRM": cls.CONFIRM,
            "HUMAN": cls.CONFIRM,
            "HUMAN_CONFIRMATION": cls.CONFIRM,
            "MANUAL": cls.CONFIRM,
        }
        return aliases.get(val, d)


class ApprovalDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    DENY = "DENY"


@dataclass
class ApprovalRequest:
    task_id: str
    action: str
    mode: ExecutionMode
    risk_level: str  # low | medium | high | critical
    reasons: list[str] = field(default_factory=list)
    summary: str = ""
    diff: dict[str, Any] | None = None
    batch_size: int = 1
    reversible: bool = True
    semantic_modes: list[str] = field(default_factory=list)
    verify_planned: bool = True
    rollback_planned: bool = True
    method_switch: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action": self.action,
            "mode": self.mode.value,
            "risk_level": self.risk_level,
            "reasons": list(self.reasons),
            "summary": self.summary,
            "diff": self.diff,
            "batch_size": self.batch_size,
            "reversible": self.reversible,
            "semantic_modes": list(self.semantic_modes),
            "verify_planned": self.verify_planned,
            "rollback_planned": self.rollback_planned,
            "method_switch": self.method_switch,
            "extras": self.extras,
        }


@dataclass
class ApprovalResult:
    decision: ApprovalDecision
    request: ApprovalRequest
    granted_by: str = "policy"
    note: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == ApprovalDecision.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "granted_by": self.granted_by,
            "note": self.note,
            "request": self.request.as_dict(),
        }


class ApprovalGate:
    """Risk-based approval gate.

    Never blocks ordinary reads. Never allows UNKNOWN auto-mutation.
    AUTO still fails closed on irreversible / unverifiable / UNKNOWN ops.
    """

    def __init__(
        self,
        mode: ExecutionMode = ExecutionMode.CONFIRM,
        *,
        batch_confirm_threshold: int = 5,
        auto_allow_max_batch: int = 3,
        confirmer: Callable[[ApprovalRequest], bool] | None = None,
        safety: Mapping[str, Any] | None = None,
    ):
        self.mode = mode
        self.batch_confirm_threshold = int(batch_confirm_threshold)
        self.auto_allow_max_batch = int(auto_allow_max_batch)
        self._confirmer = confirmer
        self.safety = dict(safety or {})
        self.history: list[ApprovalResult] = []

    def set_mode(self, mode: ExecutionMode) -> None:
        self.mode = mode

    def evaluate(self, req: ApprovalRequest) -> ApprovalResult:
        # 1) Pure reads / non-mutations: allow
        if req.action in ("read", "inspect", "query", "screenshot", "doctor") or req.extras.get("read_only"):
            return self._record(ApprovalResult(ApprovalDecision.ALLOW, req, "policy", "只读操作无需确认"))

        # 1b) Low-risk structured save is safe & idempotent — allow in both modes
        if req.action == "level_save" and req.reversible and req.risk_level in ("low", "medium"):
            return self._record(ApprovalResult(
                ApprovalDecision.ALLOW, req, "policy", "结构化保存：低风险且可重复，允许执行",
            ))

        # 2) Hard deny cases (both modes)
        if req.extras.get("delete_actor") or req.extras.get("delete_asset") or req.action == "delete":
            return self._record(ApprovalResult(
                ApprovalDecision.DENY, req, "policy",
                "删除 Actor/Asset 在本阶段禁止自动执行",
            ))
        if req.extras.get("overwrite_critical_asset"):
            return self._record(ApprovalResult(
                ApprovalDecision.DENY, req, "policy", "覆盖关键资源需要人工流程，Gate 拒绝自动执行",
            ))

        reasons = list(req.reasons)
        needs_confirm = False
        deny = False

        # 3) Semantic / confidence fail-closed
        modes = {m.upper() for m in req.semantic_modes}
        if "UNKNOWN" in modes or req.extras.get("low_confidence"):
            reasons.append("存在 UNKNOWN/低置信语义，禁止自动修改")
            if self.mode == ExecutionMode.AUTO:
                # AUTO means "auto safe decisions", not "ignore safety"
                return self._record(ApprovalResult(
                    ApprovalDecision.REQUIRE_CONFIRMATION, req, "policy",
                    "；".join(reasons),
                ))
            needs_confirm = True

        # rollback plan can make a non-idempotent op recoverable (absolute restore)
        reversible = bool(req.reversible) or bool(req.rollback_planned)
        if not reversible:
            reasons.append("回滚不可保证")
            if self.mode == ExecutionMode.AUTO:
                return self._record(ApprovalResult(
                    ApprovalDecision.REQUIRE_CONFIRMATION, req, "policy", "；".join(reasons),
                ))
            needs_confirm = True

        if not req.verify_planned or req.extras.get("verify_blocked"):
            reasons.append("无法完成独立验证")
            needs_confirm = True
            if self.mode == ExecutionMode.AUTO:
                return self._record(ApprovalResult(
                    ApprovalDecision.REQUIRE_CONFIRMATION, req, "policy", "；".join(reasons),
                ))

        if req.risk_level in ("high", "critical"):
            reasons.append(f"风险等级 {req.risk_level}")
            needs_confirm = True
            if self.mode == ExecutionMode.AUTO and req.risk_level == "critical":
                return self._record(ApprovalResult(
                    ApprovalDecision.REQUIRE_CONFIRMATION, req, "policy", "；".join(reasons),
                ))

        if req.batch_size > self.batch_confirm_threshold:
            reasons.append(f"批量 {req.batch_size} > 确认阈值 {self.batch_confirm_threshold}")
            needs_confirm = True
        if req.extras.get("large_batch"):
            reasons.append("大批量 mutation")
            needs_confirm = True
        if req.extras.get("impact_beyond_plan"):
            reasons.append("影响范围超出原计划")
            needs_confirm = True
        if req.method_switch and req.extras.get("risky_fallback"):
            reasons.append(f"fallback 将切换到更高风险方法 {req.method_switch}")
            needs_confirm = True

        # 4) AUTO path for safe mutations
        if self.mode == ExecutionMode.AUTO and not needs_confirm:
            if req.batch_size <= self.auto_allow_max_batch and req.reversible and req.risk_level in ("low", "medium"):
                if "UNKNOWN" not in modes:
                    return self._record(ApprovalResult(
                        ApprovalDecision.ALLOW, req, "policy",
                        "AUTO：低/中风险且可回滚，自动执行",
                    ))
            if req.batch_size > self.auto_allow_max_batch:
                return self._record(ApprovalResult(
                    ApprovalDecision.REQUIRE_CONFIRMATION, req, "policy",
                    f"AUTO 下批量 {req.batch_size} 超过自动上限 {self.auto_allow_max_batch}",
                ))

        # 5) CONFIRM mode / needs_confirm
        if self.mode == ExecutionMode.CONFIRM or needs_confirm:
            if not reasons:
                reasons.append("CONFIRM 模式：执行修改前需要批准")
            result = ApprovalResult(
                ApprovalDecision.REQUIRE_CONFIRMATION, req, "policy", "；".join(reasons),
            )
            if self._confirmer is not None:
                try:
                    ok = bool(self._confirmer(req))
                except Exception:
                    ok = False
                if ok:
                    result = ApprovalResult(ApprovalDecision.ALLOW, req, "human", "用户批准")
                else:
                    result = ApprovalResult(ApprovalDecision.DENY, req, "human", "用户拒绝")
            return self._record(result)

        return self._record(ApprovalResult(
            ApprovalDecision.ALLOW, req, "policy", "满足自动执行条件",
        ))

    def require(self, req: ApprovalRequest) -> ApprovalResult:
        """Evaluate and raise if not allowed."""
        from ..core.errors import PreconditionFailed

        res = self.evaluate(req)
        if not res.allowed:
            code = "APPROVAL_DENIED" if res.decision == ApprovalDecision.DENY else "APPROVAL_REQUIRED"
            raise PreconditionFailed(
                f"ApprovalGate {res.decision.value}: {res.note}",
                details={"approval": res.as_dict(), "code": code},
            )
        return res

    def _record(self, res: ApprovalResult) -> ApprovalResult:
        self.history.append(res)
        return res


def build_approval_gate(cfg: Any, *, mode: ExecutionMode | None = None, confirmer=None) -> ApprovalGate:
    m = mode or ExecutionMode.parse(cfg.get("execution.mode") if cfg else None)
    safety = (cfg.get("safety") or {}) if cfg else {}
    batch_thr = int((cfg.get("execution.batch_confirm_threshold") if cfg else None) or safety.get("approval_batch_threshold") or 5)
    auto_max = int((cfg.get("execution.auto_allow_max_batch") if cfg else None) or 3)
    return ApprovalGate(m, batch_confirm_threshold=batch_thr, auto_allow_max_batch=auto_max,
                        confirmer=confirmer, safety=safety)


__all__ = [
    "ExecutionMode",
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalResult",
    "ApprovalGate",
    "build_approval_gate",
]
