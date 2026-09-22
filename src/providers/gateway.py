"""Shared lifecycle/audit facade over existing adapters, not a second router.

Transport deadlines stay in adapters. Cancellation only revokes future dispatch;
uncancellable remote calls are explicitly reported as such, never called stopped.
"""
from __future__ import annotations

import time
import threading
from typing import Any

from ..core.errors import BackendUnavailable, NotSupported


class Provider:
    def __init__(self, name: str, adapter: Any, *, family: str = 'unreal', log=None):
        self.name, self.adapter, self.family, self.log = name, adapter, family, log
        self.state = 'unavailable'
        self.reason = 'not_initialized'
        self.generation = 0
        self.cancelled = False
        self._lock = threading.RLock()
        self.active = 0
        self.calls = 0

    def _record(self, event, **fields):
        if self.log:
            self.log(event, provider=self.name, generation=self.generation, **fields)

    def capabilities(self):
        if self.family == 'desktop':
            raw = self.adapter.capabilities()
            return set(raw.get('tools', []))
        if self.family == 'vision':
            return {'assess_gui_confidence', 'verify_gui_action'}
        return set(self.adapter.capabilities())

    def available(self):
        return not self.cancelled and bool(self.adapter.available())

    def health(self):
        try:
            if self.family == 'desktop':
                self.adapter.status()  # Fresh RPC, not cached connect success.
                available = True
            elif self.family == 'vision':
                available = True
            else:
                available = bool(self.adapter.available())
            self.state = 'healthy' if available else 'disconnected'
            self.reason = '' if available else 'live probe unavailable'
            caps = sorted(self.capabilities()) if available else []
            if self.name == 'UE_COMMANDLET' and available:
                self.state = 'degraded'
                self.reason = 'offline editor process; not the live unsaved scene'
        except Exception as exc:
            self.state = 'misconfigured' if isinstance(exc, (ValueError, NotSupported)) else 'disconnected'
            self.reason = f'{type(exc).__name__}: {exc}'
            caps = []
        return {'provider':self.name,'family':self.family,'state':self.state,
                'reason':self.reason,'capabilities':caps,'generation':self.generation,
                'can_reconnect':self.family!='vision','active_calls':self.active,
                'timeout_s':getattr(self.adapter,'timeout',None),
                'cancel_scope':'guarded_input' if self.family=='desktop' else 'future_dispatch_only'}

    def initialize(self):
        with self._lock:
            if self.cancelled:raise PermissionError('Provider cancelled; explicit reconnect required')
            return self.health()

    def execute(self, operation, *args, **kwargs):
        with self._lock:
            if self.cancelled:raise PermissionError('Provider dispatch cancelled')
            self.active += 1
        start=time.perf_counter()
        self.calls += 1
        self._record('provider_execute',operation=operation)
        try:
            result=getattr(self.adapter,operation)(*args,**kwargs)
            # A transport response is never an independent verification.
            self._record('provider_response',operation=operation,elapsed_ms=(time.perf_counter()-start)*1000,
                         verification='not_performed_by_gateway')
            return result
        except Exception as exc:
            self.state='degraded';self.reason=f'{type(exc).__name__}: {exc}'
            self._record('provider_error',operation=operation,error_type=type(exc).__name__,reason=str(exc))
            from ..router.capability_cache import get_capability_cache
            get_capability_cache().invalidate('provider_failure:'+self.name)
            # Never retry mutations here: an uncertain write may have succeeded.
            raise
        finally:
            with self._lock:self.active -= 1

    def cancel(self):
        with self._lock:self.cancelled=True
        if self.family=='desktop':self.adapter.release_control()
        self._record('provider_cancel',in_flight=self.active)
        return {'future_dispatch_cancelled':True,'in_flight':self.active,
                'remote_execution_stopped':self.family=='desktop' and self.active==0}

    def reconnect(self):
        with self._lock:
            if self.active:raise RuntimeError('Cannot reconnect with an in-flight operation')
            close=getattr(self.adapter,'close',None)
            if close:close()
            self.cancelled=False;self.generation+=1
            result=self.health()
            self._record('provider_reconnect',state=self.state)
            return result

    def shutdown(self):
        with self._lock:
            self.cancelled=True
            if self.active:raise RuntimeError('Cannot close an active provider')
            close=getattr(self.adapter,'close',None)
            if close:close()
            self.state='unavailable';self.reason='shutdown'

    def __getattr__(self, name):
        attr=getattr(self.adapter,name)
        if not callable(attr) or name in ('available','diagnostics','supports','has_native'):
            return attr
        if name=='close':return self.shutdown
        return lambda *args,**kwargs:self.execute(name,*args,**kwargs)


class ProviderGateway:
    def __init__(self):
        self.providers: dict[str,Provider]={}
        self.assembly_errors: dict[str,str]={}

    def register(self,name,adapter,*,family='unreal'):
        if name in self.providers:raise ValueError('Duplicate provider: '+name)
        self.providers[name]=Provider(name,adapter,family=family)
        return self.providers[name]

    def bind_logger(self,logger):
        for provider in self.providers.values():provider.log=logger.event

    def doctor(self):
        rows={name:p.health() for name,p in self.providers.items()}
        for name,error in self.assembly_errors.items():
            rows[name]={'provider':name,'state':'misconfigured','reason':error,'capabilities':[],'can_reconnect':False}
        healthy=[name for name,row in rows.items() if row['state']=='healthy']
        return {'providers':rows,'healthy_candidates':healthy,
                'fallback_policy':'existing Router capability/risk/budget checks; never automatic mutation replay'}

    def candidates(self,capability):
        return [name for name,p in self.providers.items()
                if p.health()['state']=='healthy' and capability in p.capabilities() and not p.cancelled]
