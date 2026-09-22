"""P0 Computer Use Safety — independent safety controller + input gate.

Priority:
  USER CONTROL > CU SAFETY > TASK COMPLETION > P4B > UI

Fail-closed: unknown safety state => deny input injection.
Emergency Stop never auto-resumes.
Safety state outranks agent/task state.
"""

from __future__ import annotations

import enum
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


class SafetyState(str, enum.Enum):
    IDLE = "IDLE"
    REQUESTED = "REQUESTED"
    BANNER_VISIBLE = "BANNER_VISIBLE"
    CONTROL_ACQUIRED = "CONTROL_ACQUIRED"  # injection permission granted (NOT ownership)
    ACTIVE = "ACTIVE"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"


class InputOwner(str, enum.Enum):
    USER = "USER"
    AGENT = "AGENT"
    TRANSITION = "TRANSITION"
    UNKNOWN = "UNKNOWN"


# P0.1 §10: states that only an *explicit user* action may leave.
# Every mutating method below must honour this set, not just EMERGENCY_STOP.
TERMINAL_STATES: frozenset["SafetyState"] = frozenset(
    (SafetyState.EMERGENCY_STOP, SafetyState.HUMAN_OVERRIDE)
)

# P0.1 §34 / fail-closed: the only states in which agent injection may be permitted.
# Anything not listed (unknown, future, corrupted) => deny.
INJECTION_ACTIVE_STATES: frozenset[str] = frozenset(
    ("CONTROL_ACQUIRED", "ACTIVE")
)


class CuLifecycle(str, enum.Enum):
    REQUESTED = "REQUESTED"
    BANNER_VISIBLE = "BANNER_VISIBLE"
    CONTROL_ACQUIRED = "CONTROL_ACQUIRED"
    ACTIVE = "ACTIVE"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"


@dataclass
class SafetySnapshot:
    state: SafetyState = SafetyState.IDLE
    input_owner: InputOwner = InputOwner.USER  # physical owner is ALWAYS user
    agent_has_input_control: bool = False  # DEPRECATED alias — means injection permission
    agent_injection_permission: bool = False
    allow_input: bool = False
    lifecycle: CuLifecycle = CuLifecycle.RELEASED
    banner_visible: bool = False
    banner_state: str = "hidden"
    emergency_reason: str | None = None
    emergency_at: float | None = None
    human_override: bool = False
    human_override_at: float | None = None
    last_heartbeat: float | None = None
    heartbeat_ok: bool = True
    lease_expires_at: float | None = None
    task: str = ""
    hotkey: str = "ctrl+alt+f12"
    events: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    @property
    def user_control_restored(self) -> bool:
        return False  # Physical control is not observable from a software state snapshot.

    @property
    def human_input_blocked_by_uha(self) -> bool:
        """Always False by architecture — UHA never blocks physical user input."""
        return False

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["input_owner"] = self.input_owner.value
        d["lifecycle"] = self.lifecycle.value
        d["user_control_restored"] = self.user_control_restored
        d["user_control_verified"] = False
        d["human_input_blocked_by_uha"] = self.human_input_blocked_by_uha
        d["physical_input_owner"] = "USER_ALWAYS"
        return d


def default_state_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".state" / "safety_gate.json"


class SafetyController:
    """Independent safety layer. Minimal logic, no network/LLM/UE dependency."""

    def __init__(
        self,
        *,
        state_path: str | Path | None = None,
        hotkey: str = "ctrl+alt+f12",
        heartbeat_timeout_s: float = 8.0,
        injection_lease_s: float = 30.0,
        on_estop: list[Callable[[], None]] | None = None,
        on_human_override: list[Callable[[str], None]] | None = None,
        log_fn: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.state_path = Path(state_path) if state_path else default_state_path()
        self.hotkey = hotkey
        self.heartbeat_timeout_s = float(heartbeat_timeout_s)
        self.injection_lease_s = float(injection_lease_s)
        self._lock = threading.RLock()
        self._on_estop: list[Callable[[], None]] = list(on_estop or [])
        self._on_human_override: list[Callable[[str], None]] = list(on_human_override or [])
        self._log_fn = log_fn
        self._snapshot = SafetySnapshot(hotkey=hotkey)
        self._cancelled_actions = 0
        self._blocked_inputs = 0
        self._action_cancel_event = threading.Event()
        self._estop_latency_ms: list[float] = []
        self._hotkey_registered = False
        self._banner_ok = False
        self._banner_available = False
        self._banner_probe = None
        self._gate_reachable = False
        self._watchdog_pid: int | None = None
        self.human_override_detector = None
        # A process restart must not erase a human's latched stop from the shared gate.
        if self.state_path.exists():
            try:
                previous = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(previous, dict): raise ValueError("invalid safety state")
                prior_state = SafetyState(previous.get("state"))
            except (OSError, ValueError, TypeError):
                previous = {"emergency_reason": "unreadable_previous_safety_state"}
                prior_state = SafetyState.EMERGENCY_STOP
            if prior_state in TERMINAL_STATES:
                self._snapshot.state = prior_state
                self._snapshot.lifecycle = CuLifecycle.EMERGENCY_STOP
                self._snapshot.human_override = prior_state == SafetyState.HUMAN_OVERRIDE
                self._snapshot.emergency_reason = previous.get("emergency_reason")
                self._snapshot.banner_state = "human_override" if self._snapshot.human_override else "emergency"
                self._action_cancel_event.set()
        self._write_state("init")

    # -- logging / persistence -------------------------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        payload = {"event": event, "ts": time.time(), **fields}
        with self._lock:
            self._snapshot.events.append(event)
            self._snapshot.events = self._snapshot.events[-50:]
        if self._log_fn:
            try:
                self._log_fn(event, payload)
            except Exception:
                pass

    def _write_state(self, reason: str = "") -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            snap = self.snapshot()
            payload = snap.as_dict()
            payload["write_reason"] = reason
            temporary = self.state_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(temporary, self.state_path)
            self._gate_reachable = True
        except Exception:
            self._gate_reachable = False

    def _notify_estop_hooks(self) -> None:
        for fn in list(self._on_estop):
            try:
                fn()
            except Exception:
                pass

    # -- observation -------------------------------------------------------------

    def snapshot(self) -> SafetySnapshot:
        with self._lock:
            return SafetySnapshot(
                state=self._snapshot.state,
                input_owner=self._snapshot.input_owner,
                agent_has_input_control=self._snapshot.agent_injection_permission,
                agent_injection_permission=self._snapshot.agent_injection_permission,
                allow_input=self._snapshot.allow_input,
                lifecycle=self._snapshot.lifecycle,
                banner_visible=self._snapshot.banner_visible,
                banner_state=self._snapshot.banner_state,
                emergency_reason=self._snapshot.emergency_reason,
                emergency_at=self._snapshot.emergency_at,
                human_override=self._snapshot.human_override,
                human_override_at=self._snapshot.human_override_at,
                last_heartbeat=self._snapshot.last_heartbeat,
                heartbeat_ok=self._snapshot.heartbeat_ok,
                lease_expires_at=self._snapshot.lease_expires_at,
                task=self._snapshot.task,
                hotkey=self._snapshot.hotkey,
                events=list(self._snapshot.events[-20:]),
                updated_at=time.time(),
            )

    @property
    def allow_input(self) -> bool:
        """Agent **injection permission** gate. Never blocks human physical input."""
        with self._lock:
            if self._snapshot.state in (SafetyState.EMERGENCY_STOP, SafetyState.HUMAN_OVERRIDE):
                return False
            if self._snapshot.state.value not in INJECTION_ACTIVE_STATES:
                return False
            if self.human_override_detector is not None and not self.human_override_detector.hook_alive:
                return False
            if self._banner_probe is not None and not self._banner_is_visible():
                return False
            if not self._snapshot.heartbeat_ok:
                return False
            if self._snapshot.lease_expires_at and time.time() > self._snapshot.lease_expires_at:
                return False
            return bool(self._snapshot.allow_input) and self._snapshot.agent_injection_permission

    @property
    def injection_permission(self) -> bool:
        return self.allow_input

    @property
    def state(self) -> SafetyState:
        return self._snapshot.state

    # -- lifecycle ---------------------------------------------------------------

    def request_control(self, *, task: str = "") -> SafetySnapshot:
        """Banner must become visible BEFORE control acquisition.

        P0.1 §10: a terminal state (EMERGENCY_STOP / HUMAN_OVERRIDE) may not be left
        by anyone except the user, so requesting control while one is active is refused.
        Refusing here is what stops the executor's per-attempt retry loop from
        silently re-opening the gate after the human took over.
        """
        with self._lock:
            if self._snapshot.state in TERMINAL_STATES:
                self._log("CU_CONTROL_REQUEST_BLOCKED_TERMINAL", state=self._snapshot.state.value)
                raise RuntimeError(
                    f"{self._snapshot.state.value} active; Computer Use disabled until "
                    f"the user explicitly resumes"
                )
            self._snapshot.state = SafetyState.REQUESTED
            self._snapshot.lifecycle = CuLifecycle.REQUESTED
            self._snapshot.task = task or self._snapshot.task
            self._snapshot.allow_input = False
            self._snapshot.input_owner = InputOwner.TRANSITION
            self._snapshot.agent_has_input_control = False
            self._action_cancel_event.clear()
            self._log("CU_CONTROL_REQUESTED", task=task)
            self._write_state("request")
            return self.snapshot()

    def set_banner_available(self, ok: bool) -> None:
        """Declare whether a *real* banner can be shown at all (P0.1 §7 banner-first).

        Without this, `banner_visible(ok=True)` is just a boolean the agent writes to
        itself — a banner-first guarantee that holds even when no banner exists.
        """
        with self._lock:
            self._banner_available = bool(ok)
            if not ok:
                self._snapshot.banner_visible = False
                self._snapshot.banner_state = "unavailable"
            self._log("CU_BANNER_AVAILABILITY", available=bool(ok))

    def set_banner_probe(self, probe):
        self._banner_probe = probe

    def _banner_is_visible(self):
        try:
            return bool(self._banner_probe()) if self._banner_probe is not None else self._banner_available
        except Exception:
            return False

    @property
    def banner_available(self) -> bool:
        return self._banner_available

    def banner_visible(self, *, ok: bool = True) -> SafetySnapshot:
        with self._lock:
            # fail-closed: an unavailable banner can never be "visible"
            effective = bool(ok) and self._banner_available and self._banner_is_visible()
            self._snapshot.banner_visible = effective
            if not ok:
                self._snapshot.banner_state = "failed"
            elif not self._banner_available:
                self._snapshot.banner_state = "unavailable"
            else:
                self._snapshot.banner_state = "active"
            self._banner_ok = effective
            if effective and self._snapshot.state == SafetyState.REQUESTED:
                self._snapshot.state = SafetyState.BANNER_VISIBLE
                self._snapshot.lifecycle = CuLifecycle.BANNER_VISIBLE
            self._log("CU_BANNER_VISIBLE", ok=ok, effective=effective,
                      available=self._banner_available)
            self._write_state("banner")
            return self.snapshot()

    def grant_control(self) -> SafetySnapshot:
        """Grant **injection permission** — NOT exclusive physical ownership."""
        with self._lock:
            if self._snapshot.state in (SafetyState.EMERGENCY_STOP, SafetyState.HUMAN_OVERRIDE):
                raise RuntimeError(
                    f"{self._snapshot.state.value} active; cannot grant injection permission"
                )
            if not self._snapshot.banner_visible or not self._banner_is_visible():
                self._log("CU_CONTROL_GRANT_REFUSED_NO_BANNER")
                if not self._banner_available:
                    raise RuntimeError(
                        "No safety banner can be shown (banner backend unavailable). "
                        "Fail closed: injection permission refused. Call "
                        "set_banner_available(True) only once a banner really exists."
                    )
                raise RuntimeError("Safety banner must be visible before granting injection permission")
            if not self._snapshot.heartbeat_ok:
                self._log("CU_CONTROL_GRANT_REFUSED_HEARTBEAT")
                raise RuntimeError("Safety heartbeat unhealthy; fail closed")
            self._snapshot.state = SafetyState.CONTROL_ACQUIRED
            self._snapshot.lifecycle = CuLifecycle.CONTROL_ACQUIRED
            self._snapshot.agent_injection_permission = True
            self._snapshot.agent_has_input_control = True  # legacy alias
            self._snapshot.allow_input = True
            self._snapshot.input_owner = InputOwner.USER  # physical stays USER
            self._snapshot.human_override = False
            self._snapshot.lease_expires_at = time.time() + self.injection_lease_s
            self._snapshot.last_heartbeat = time.time()
            self._log("CU_INJECTION_PERMISSION_GRANTED", lease_s=self.injection_lease_s)
            self._write_state("grant")
            return self.snapshot()

    def mark_active(self, *, step: str = "") -> None:
        with self._lock:
            if self._snapshot.state == SafetyState.EMERGENCY_STOP:
                return
            if self._snapshot.state in (SafetyState.CONTROL_ACQUIRED, SafetyState.ACTIVE, SafetyState.BANNER_VISIBLE):
                self._snapshot.state = SafetyState.ACTIVE
                self._snapshot.lifecycle = CuLifecycle.ACTIVE
                if step:
                    self._snapshot.task = step
                self._snapshot.last_heartbeat = time.time()
                self._write_state("active")

    def heartbeat(self) -> bool:
        with self._lock:
            if self._snapshot.state in (SafetyState.EMERGENCY_STOP, SafetyState.HUMAN_OVERRIDE):
                return False
            self._snapshot.last_heartbeat = time.time()
            self._snapshot.heartbeat_ok = True
            if self._snapshot.agent_injection_permission:
                self._snapshot.lease_expires_at = time.time() + self.injection_lease_s
            self._write_state("heartbeat")
            return self.allow_input

    def check_heartbeat(self) -> bool:
        with self._lock:
            if self._snapshot.state == SafetyState.EMERGENCY_STOP:
                return False
            if self._snapshot.last_heartbeat is None:
                # no CU yet — not a heartbeat failure
                return True
            if time.time() - self._snapshot.last_heartbeat > self.heartbeat_timeout_s:
                if self._snapshot.agent_has_input_control:
                    self._snapshot.heartbeat_ok = False
                    self._snapshot.allow_input = False
                    self._snapshot.state = SafetyState.EMERGENCY_STOP
                    self._snapshot.lifecycle = CuLifecycle.EMERGENCY_STOP
                    self._snapshot.input_owner = InputOwner.USER
                    self._snapshot.agent_has_input_control = False
                    self._snapshot.emergency_reason = "heartbeat_timeout"
                    self._snapshot.emergency_at = time.time()
                    self._log("CU_HEARTBEAT_TIMEOUT")
                    self._write_state("heartbeat_timeout")
                    self._notify_estop_hooks()
                return False
            return True

    def release_control(self, *, reason: str = "released") -> SafetySnapshot:
        """Give up injection permission.

        P0.1 §10 — **must not clear a terminal state.** Releasing is routine: it happens
        in the ``finally`` of every ``ComputerUseAdapter.control()`` block. If releasing
        also cleared HUMAN_OVERRIDE, the agent would silently regain injection permission
        milliseconds after the human took over, and the human would have to fight the
        cursor again. So in a terminal state we only re-assert "no injection" and leave
        the state alone.
        """
        with self._lock:
            if self._snapshot.state in TERMINAL_STATES:
                self._snapshot.allow_input = False
                self._snapshot.agent_injection_permission = False
                self._snapshot.agent_has_input_control = False
                self._snapshot.input_owner = InputOwner.USER
                self._snapshot.lease_expires_at = None
                self._action_cancel_event.set()
                if self._snapshot.state == SafetyState.EMERGENCY_STOP:
                    self._snapshot.lifecycle = CuLifecycle.EMERGENCY_STOP
                self._log("CU_RELEASE_IGNORED_TERMINAL", state=self._snapshot.state.value, reason=reason)
                self._write_state("release_while_terminal")
                return self.snapshot()
            self._snapshot.agent_injection_permission = False
            self._snapshot.lease_expires_at = None
            self._snapshot.state = SafetyState.RELEASING
            self._snapshot.lifecycle = CuLifecycle.RELEASING
            self._snapshot.allow_input = False
            self._snapshot.agent_has_input_control = False
            self._snapshot.input_owner = InputOwner.USER
            self._snapshot.banner_visible = False
            self._snapshot.banner_state = "finished"
            self._snapshot.state = SafetyState.RELEASED
            self._snapshot.lifecycle = CuLifecycle.RELEASED
            self._log("CU_CONTROL_RELEASED", reason=reason)
            self._write_state("release")
            return self.snapshot()

    def clear_cancel(self) -> None:
        self._action_cancel_event.clear()

    @property
    def action_cancelled(self) -> bool:
        """True whenever pending agent actions must be abandoned (P0.1 §4)."""
        return self._action_cancel_event.is_set() or self.state in TERMINAL_STATES

    def emergency_stop(self, *, reason: str = "EMERGENCY_STOP", source: str = "hotkey") -> dict[str, Any]:
        """Close agent injection gate immediately. Does not block human input."""
        t0 = time.perf_counter()
        with self._lock:
            already = self._snapshot.state in (SafetyState.EMERGENCY_STOP, SafetyState.HUMAN_OVERRIDE)
            self._snapshot.state = SafetyState.EMERGENCY_STOP
            self._snapshot.lifecycle = CuLifecycle.EMERGENCY_STOP
            self._snapshot.allow_input = False
            self._snapshot.agent_injection_permission = False
            self._snapshot.agent_has_input_control = False
            self._snapshot.input_owner = InputOwner.USER
            self._snapshot.emergency_reason = reason
            self._snapshot.emergency_at = time.time()
            self._snapshot.lease_expires_at = None
            self._snapshot.banner_state = "emergency"
            self._snapshot.banner_visible = True
            self._action_cancel_event.set()
            self._cancelled_actions += 1
            self._log("EMERGENCY_STOP_TRIGGERED", source=source, reason=reason)
            self._write_state("estop")
        latency_ms = (time.perf_counter() - t0) * 1000
        self._estop_latency_ms.append(latency_ms)
        self._notify_estop_hooks()
        self._log("EMERGENCY_STOP_COMPLETED", latency_ms=latency_ms)
        return {
            "ok": True,
            "already_stopped": already,
            "latency_ms": latency_ms,
            "user_control_restored": False,
            "user_control_verified": False,
            "physical_input_owner": "USER_ALWAYS",
            "human_input_blocked_by_uha": False,
            "agent_injection_permission": False,
            "state": SafetyState.EMERGENCY_STOP.value,
        }

    def trigger_human_override(self, kind: str = "physical_input") -> dict[str, Any]:
        """Physical human activity revokes agent injection permission immediately."""
        t0 = time.perf_counter()
        with self._lock:
            if self._snapshot.state in (SafetyState.EMERGENCY_STOP, SafetyState.HUMAN_OVERRIDE):
                return {"ok": True, "already": True, "state": self._snapshot.state.value}
            self._snapshot.state = SafetyState.HUMAN_OVERRIDE
            self._snapshot.lifecycle = CuLifecycle.EMERGENCY_STOP
            self._snapshot.allow_input = False
            self._snapshot.agent_injection_permission = False
            self._snapshot.agent_has_input_control = False
            self._snapshot.input_owner = InputOwner.USER
            self._snapshot.human_override = True
            self._snapshot.human_override_at = time.time()
            self._snapshot.lease_expires_at = None
            self._snapshot.banner_state = "human_override"
            self._snapshot.banner_visible = True
            self._snapshot.emergency_reason = f"HUMAN_OVERRIDE:{kind}"
            self._action_cancel_event.set()
            self._cancelled_actions += 1
            self._log("HUMAN_OVERRIDE_TRIGGERED", kind=kind)
            self._write_state("human_override")
        latency_ms = (time.perf_counter() - t0) * 1000
        for fn in list(self._on_human_override):
            try:
                fn(kind)
            except Exception:
                pass
        self._notify_estop_hooks()
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "kind": kind,
            "state": SafetyState.HUMAN_OVERRIDE.value,
            "message": "HUMAN OVERRIDE — new agent input denied; physical control not verified.",
            "user_control_verified": False,
        }

    def resume_safety(self, *, explicit: bool = False) -> SafetySnapshot:
        """Only explicit user action may leave EMERGENCY_STOP / HUMAN_OVERRIDE."""
        with self._lock:
            if self._snapshot.state not in (SafetyState.EMERGENCY_STOP, SafetyState.HUMAN_OVERRIDE):
                return self.snapshot()
            if not explicit:
                raise RuntimeError("Human Override / Emergency Stop requires explicit user resume")
            self._snapshot.state = SafetyState.IDLE
            self._snapshot.lifecycle = CuLifecycle.RELEASED
            self._snapshot.allow_input = False
            self._snapshot.agent_injection_permission = False
            self._snapshot.agent_has_input_control = False
            self._snapshot.input_owner = InputOwner.USER
            self._snapshot.emergency_reason = None
            self._snapshot.human_override = False
            self._snapshot.banner_state = "hidden"
            self._snapshot.banner_visible = False
            self._snapshot.heartbeat_ok = True
            self._snapshot.lease_expires_at = None
            self._action_cancel_event.clear()
            self._log("SAFETY_RESUME_EXPLICIT")
            self._write_state("resume")
            return self.snapshot()

    def mark_blocked_input(self, action: str = "") -> None:
        with self._lock:
            self._blocked_inputs += 1
        self._log("SAFETY_GATE_BLOCKED_INPUT", action=action)

    def raise_if_blocked(self, action: str = "") -> None:
        if not self.allow_input:
            self.mark_blocked_input(action)
            raise PermissionError(
                f"Safety gate blocked input injection: state={self.state.value} owner={self.snapshot().input_owner.value}"
            )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "blocked_inputs": self._blocked_inputs,
                "cancelled_actions": self._cancelled_actions,
                "estop_latency_ms": list(self._estop_latency_ms[-20:]),
                "hotkey_registered": self._hotkey_registered,
                "banner_ok": self._banner_ok,
                "gate_reachable": self._gate_reachable,
                "watchdog_pid": self._watchdog_pid,
            }

    def set_hotkey_registered(self, ok: bool) -> None:
        self._hotkey_registered = bool(ok)

    def set_watchdog_pid(self, pid: int | None) -> None:
        self._watchdog_pid = pid

    def startup_selfcheck(self) -> dict[str, Any]:
        """If EStop safety layer failed => Computer Use must stay disabled."""
        ok = True
        reasons = []
        if not self._gate_reachable:
            try:
                self._write_state("selfcheck")
            except Exception:
                pass
        if not self._gate_reachable:
            ok = False
            reasons.append("input_gate_unreachable")
        # hotkey registration is checked by caller after EmergencyHotkey.start()
        if not self._hotkey_registered:
            # not fatal for selfcheck object; overall system check uses this flag
            reasons.append("hotkey_not_registered_yet")
        if not self._banner_available:
            ok = False
            reasons.append("no_safety_banner_available")
        self._log("SAFETY_SELFCHECK", ok=ok, reasons=reasons)
        return {
            "ok": ok,
            "hotkey_registered": self._hotkey_registered,
            "gate_reachable": self._gate_reachable,
            "banner_available": self._banner_available,
            "reasons": reasons,
            "computer_use_allowed": ok and self._hotkey_registered,
        }


# Process-wide controller (watchdog + UHA both can open same state file)
_CONTROLLER: SafetyController | None = None
_LOCK = threading.Lock()


def get_safety_controller(**kwargs: Any) -> SafetyController:
    global _CONTROLLER
    with _LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = SafetyController(**kwargs)
            try:
                from .human_override import HumanOverrideDetector

                def _on_phys(kind: str) -> None:
                    # Physical human input revokes agent injection permission (P0.1 §4).
                    if _CONTROLLER is not None and _CONTROLLER.allow_input:
                        _CONTROLLER.trigger_human_override(kind=kind)
                        det = getattr(_CONTROLLER, "human_override_detector", None)
                        if det is not None:
                            try:
                                det.human_override_count += 1
                            except Exception:
                                pass

                det = HumanOverrideDetector(on_physical_input=_on_phys)
                _CONTROLLER.human_override_detector = det
                try:
                    det.start()
                except Exception:
                    pass
            except Exception:
                pass
        return _CONTROLLER


def reset_safety_controller_for_tests() -> None:
    """Drop the process-wide controller. Must also unhook the detector, otherwise
    every test run leaks a live low-level hook (and a stalled hook blocks input)."""
    global _CONTROLLER
    with _LOCK:
        old = _CONTROLLER
        _CONTROLLER = None
    if old is not None:
        det = getattr(old, "human_override_detector", None)
        if det is not None:
            try:
                det.stop()
            except Exception:
                pass


def read_external_gate_state(state_path: str | Path | None = None) -> dict[str, Any]:
    p = Path(state_path) if state_path else default_state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "UNKNOWN", "allow_input": False, "fail_closed": True}


def external_gate_allows_input(state_path: str | Path | None = None) -> bool:
    """Independent check used by CU adapter / watchdog consumers.

    P0.1 §10 / fail-closed: an on-disk signal may only ever **close** the gate.
    It must never be able to open one, otherwise a stale file left behind by a
    crashed process out-votes the live safety controller. Hence a strict whitelist
    of injection-active states, plus an explicit non-terminal check.
    """
    data = read_external_gate_state(state_path)
    if not isinstance(data, dict): return False
    state = str(data.get("state") or "UNKNOWN")
    if state not in INJECTION_ACTIVE_STATES:
        return False
    if not (data.get("allow_input") is True and data.get("agent_has_input_control") is True):
        return False
    try:
        lease = data.get("lease_expires_at")
        if lease is not None and time.time() >= float(lease): return False
    except (ValueError, TypeError): return False
    return True


__all__ = [
    "SafetyState",
    "InputOwner",
    "CuLifecycle",
    "SafetySnapshot",
    "SafetyController",
    "TERMINAL_STATES",
    "INJECTION_ACTIVE_STATES",
    "get_safety_controller",
    "reset_safety_controller_for_tests",
    "read_external_gate_state",
    "external_gate_allows_input",
    "default_state_path",
]
