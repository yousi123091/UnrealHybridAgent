"""Opt-in loopback approval transport. All missing/expired/disconnected replies deny."""
from __future__ import annotations
import hashlib
import json
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def token_path(url):
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('Approvals require a loopback HTTP Hub')
    return Path(__file__).resolve().parents[2]/'.state/uah'/f'approval-{parsed.port or 80}.token'


class ApprovalBroker:
    def __init__(self):
        self.lock=threading.Lock();self.requests={};self.spent=set()

    def handle(self,operation,data):
        with self.lock:
            now=time.monotonic()
            for req in self.requests.values():
                if now>=req['deadline']:req['decision']=False
            if operation=='create':
                rid=str(data['req_id'])
                if rid in self.spent or rid in self.requests:return {'ok':False,'error':'duplicate req_id'}
                if len(self.spent)+len(self.requests)>=10000:return {'ok':False,'error':'approval capacity reached'}
                ttl=max(0.05,min(float(data.get('timeout_s',60)),300))
                self.requests[rid]={'req_id':rid,'agent_id':str(data['agent_id']),
                    'summary':str(data.get('summary',''))[:1000],'deadline':now+ttl,'decision':None}
                return {'ok':True}
            if operation=='pending':
                return {'ok':True,'pending':[dict(r) for r in self.requests.values() if r['decision'] is None]}
            rid=str(data.get('req_id',''));req=self.requests.get(rid)
            if req is None:return {'ok':False,'decision':False,'error':'unknown or consumed req_id'}
            if operation=='decide':
                if req['decision'] is not None:return {'ok':False,'error':'already resolved'}
                if type(data.get('allow')) is not bool:return {'ok':False,'error':'boolean required'}
                req['decision']=data['allow'];return {'ok':True}
            if operation=='cancel':req['decision']=False
            if operation in ('poll','cancel'):
                decision=req['decision']
                if decision is not None:
                    self.requests.pop(rid);self.spent.add(rid)
                return {'ok':True,'decision':decision}
            return {'ok':False,'error':'unknown operation'}


def approval_call(url,operation,data,*,token=None):
    path=token_path(url)
    capability=token if token is not None else path.read_text(encoding='utf-8').strip()
    req=urllib.request.Request(url.rstrip('/')+'/approval/'+operation,
        data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-UAH-Approval':capability},method='POST')
    with urllib.request.urlopen(req,timeout=1.5) as response:return json.load(response)


def make_confirmer(url,agent_id,timeout_s=60):
    timeout=max(0.05,min(float(timeout_s),300))
    def confirm(request):
        # AUTO policy must never be silently converted into interactive approval.
        if getattr(getattr(request,'mode',None),'value',None)!='CONFIRM':return False
        rid=secrets.token_hex(24); token=None
        try:
            token=token_path(url).read_text(encoding='utf-8').strip()
            result=approval_call(url,'create',{'req_id':rid,'agent_id':agent_id,'summary':request.summary or request.action,'timeout_s':timeout},token=token)
            if not result.get('ok'):return False
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                response=approval_call(url,'poll',{'req_id':rid},token=token)
                if not response.get('ok'):return False
                if response.get('decision') is not None:return response['decision'] is True
                time.sleep(min(.1,max(0,deadline-time.monotonic())))
            return False
        except Exception:return False
        finally:
            if token:
                try:approval_call(url,'cancel',{'req_id':rid},token=token)
                except Exception:pass
    return confirm
