"""Session-level capability cache (Phase 4A).

Avoids re-probing MCP/CU/UE endpoints on every Router decision.
Failures invalidate cache — never hide real faults.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass
class CapabilitySnapshot:
    ready: dict[str, bool] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)  # READY/DOWN/UNKNOWN
    errors: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    source: str = "probe"
    session_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": dict(self.ready),
            "status": dict(self.status),
            "errors": dict(self.errors),
            "created_at": self.created_at,
            "age_s": round(time.time() - self.created_at, 3),
            "source": self.source,
            "session_fingerprint": self.session_fingerprint,
        }


class CapabilityCache:
    def __init__(self, ttl_s: float = 20.0):
        self.ttl_s = float(ttl_s)
        self._lock = threading.RLock()
        self._snap: CapabilitySnapshot | None = None
        self.hits = 0
        self.misses = 0
        self.invalidations = 0
        self.latencies_ms: list[float] = []

    def get(
        self,
        probe: Callable[[], Mapping[str, Any]],
        *,
        force: bool = False,
        fingerprint: str | None = None,
    ) -> CapabilitySnapshot:
        with self._lock:
            now = time.time()
            if not force and self._snap is not None:
                age = now - self._snap.created_at
                fp_ok = (
                    fingerprint is None
                    or self._snap.session_fingerprint is None
                    or fingerprint == self._snap.session_fingerprint
                )
                if age <= self.ttl_s and fp_ok:
                    self.hits += 1
                    return self._snap
            self.misses += 1
            t0 = time.perf_counter()
            try:
                raw = dict(probe())
            except Exception as exc:  # noqa: BLE001
                self.invalidations += 1
                self._snap = CapabilitySnapshot(
                    ready={}, status={"_probe": "ERROR"}, errors={"_probe": f"{type(exc).__name__}: {exc}"},
                    source="probe_error", session_fingerprint=fingerprint,
                )
                self.latencies_ms.append((time.perf_counter() - t0) * 1000)
                return self._snap
            ready: dict[str, bool] = {}
            status: dict[str, str] = {}
            errors: dict[str, str] = {}
            for k, v in raw.items():
                if isinstance(v, Mapping):
                    ok = bool(v.get("available", v.get("ok", False)))
                    ready[k] = ok
                    status[k] = "READY" if ok else "DOWN"
                    if v.get("error"):
                        errors[k] = str(v["error"])
                else:
                    ok = bool(v)
                    ready[k] = ok
                    status[k] = "READY" if ok else "DOWN"
            self._snap = CapabilitySnapshot(
                ready=ready, status=status, errors=errors, source="probe", session_fingerprint=fingerprint,
            )
            self.latencies_ms.append((time.perf_counter() - t0) * 1000)
            return self._snap

    def invalidate(self, reason: str = "") -> None:
        with self._lock:
            self._snap = None
            self.invalidations += 1

    def peek(self) -> CapabilitySnapshot | None:
        return self._snap

    def stats(self) -> dict[str, Any]:
        lat = list(self.latencies_ms)
        return {
            "hits": self.hits,
            "misses": self.misses,
            "invalidations": self.invalidations,
            "ttl_s": self.ttl_s,
            "probe_ms_avg": round(sum(lat) / len(lat), 2) if lat else None,
            "probe_ms_last": round(lat[-1], 2) if lat else None,
            "snapshot": self._snap.as_dict() if self._snap else None,
        }


_CACHE: CapabilityCache | None = None
_LOCK = threading.Lock()


def get_capability_cache(ttl_s: float | None = None) -> CapabilityCache:
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = CapabilityCache(ttl_s=ttl_s if ttl_s is not None else 20.0)
        return _CACHE


def reset_capability_cache() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


__all__ = ["CapabilityCache", "CapabilitySnapshot", "get_capability_cache", "reset_capability_cache"]
