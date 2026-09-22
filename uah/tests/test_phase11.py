"""Real HTTP approval and isolated failure-regression tests; no physical takeover claims."""
import sys,time,tempfile,threading,unittest,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from uah.core.transport import HubServer
from uah.core.approval import approval_call,make_confirmer
from src.core.execution_mode import ApprovalGate,ApprovalRequest,ExecutionMode,ApprovalDecision

class Approvals(unittest.TestCase):
    def setUp(self):self.hub=HubServer(port=0).start();self.url=self.hub.url()
    def tearDown(self):self.hub.stop()
    def req(self):return ApprovalRequest('task','mutate',ExecutionMode.CONFIRM,'high',summary='Move 5 Actors')
    def call(self,op,data):return approval_call(self.url,op,data)
    def test_replay_expiry(self):
        data={'req_id':'once','agent_id':'uha','timeout_s':.06}
        self.assertTrue(self.call('create',data)['ok'])
        self.assertFalse(self.call('create',data)['ok'])
        time.sleep(.08)
        self.assertFalse(self.call('decide',{'req_id':'once','allow':True})['ok'])
        self.assertIs(self.call('poll',{'req_id':'once'})['decision'],False)
        self.assertFalse(self.call('create',data)['ok'])
    def test_allow_and_reject(self):
        for allow in (True,False):
            result=[];gate=ApprovalGate(confirmer=make_confirmer(self.url,'uha',2))
            th=threading.Thread(target=lambda:result.append(gate.evaluate(self.req())));th.start()
            pending=[];end=time.monotonic()+1
            while not pending and time.monotonic()<end:
                pending=self.call('pending',{})['pending'];time.sleep(.01)
            self.assertTrue(pending)
            payload={'req_id':pending[0]['req_id'],'allow':allow}
            self.assertTrue(self.call('decide',payload)['ok'])
            self.assertFalse(self.call('decide',payload)['ok'])
            th.join(3);self.assertFalse(th.is_alive());self.assertEqual(result[0].allowed,allow)
    def test_timeout_default_deny(self):
        result=ApprovalGate(confirmer=make_confirmer(self.url,'uha',.1)).evaluate(self.req())
        self.assertEqual(result.decision,ApprovalDecision.DENY)
    def test_disconnect_fail_closed(self):
        result=[];th=threading.Thread(target=lambda:result.append(make_confirmer(self.url,'uha',2)(self.req())))
        th.start();time.sleep(.12);self.hub.stop();th.join(4)
        self.assertEqual(result,[False])
    def test_wrong_capability_and_origin(self):
        import urllib.request,urllib.error
        with self.assertRaises(urllib.error.HTTPError):approval_call(self.url,'pending',{},token='wrong')
        request=urllib.request.Request(self.url+'/approval/pending',data=b'{}',headers={'Origin':'http://evil.test'})
        with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(request)
    def test_auto_and_hard_deny_unchanged(self):
        req=self.req();req.mode=ExecutionMode.AUTO
        self.assertFalse(make_confirmer(self.url,'uha',.1)(req))
        req.action='delete';calls=[]
        result=ApprovalGate(confirmer=lambda r:calls.append(r) or True).evaluate(req)
        self.assertEqual(result.decision,ApprovalDecision.DENY);self.assertEqual(calls,[])

class SafetyRegression(unittest.TestCase):
    def setUp(self):
        from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
        reset_safety_controller_for_tests();self.tmp=tempfile.TemporaryDirectory()
        from src.safety import controller as module
        self.sc=module.SafetyController(state_path=Path(self.tmp.name)/'gate.json')
        module._CONTROLLER=self.sc  # isolated unit fixture; actual hooks tested separately
        self.sc.set_banner_available(True);self.sc.request_control();self.sc.banner_visible();self.sc.grant_control()
    def tearDown(self):
        from src.safety.controller import reset_safety_controller_for_tests
        from src.desktop.hotkey import clear_tracking
        reset_safety_controller_for_tests();clear_tracking();self.tmp.cleanup()
    def test_release_clears_permission_and_lease(self):
        self.sc.release_control();snap=self.sc.snapshot()
        self.assertFalse(snap.agent_injection_permission);self.assertIsNone(snap.lease_expires_at)
        self.assertFalse(snap.as_dict()['user_control_verified'])
    def test_failed_remote_release_still_closes_gate(self):
        from src.adapters.computer_use import ComputerUseAdapter
        cu=ComputerUseAdapter('http://127.0.0.1:1',dry_run=True)
        cu._call=lambda *a,**k:(_ for _ in ()).throw(ConnectionError('offline'))
        with self.assertRaises(ConnectionError):cu.release_control()
        self.assertFalse(self.sc.allow_input)
    def test_failed_paste_retains_ledger(self):
        from src.adapters.computer_use import ComputerUseAdapter
        from src.desktop.hotkey import tracked_input
        cu=ComputerUseAdapter('http://127.0.0.1:1',dry_run=True)
        cu._call=lambda *a,**k:(_ for _ in ()).throw(ConnectionError('mid paste'))
        with self.assertRaises(ConnectionError):cu.type_text('example')
        self.assertEqual(set(tracked_input()['keys']),{'0x11','0x56'})
    def test_ten_lifecycles(self):
        for _ in range(10):
            self.sc.release_control();self.assertFalse(self.sc.allow_input)
            self.sc.request_control();self.sc.banner_visible();self.sc.grant_control();self.assertTrue(self.sc.allow_input)
        self.sc.trigger_human_override('SIMULATED')
        for _ in range(10):
            self.sc.release_control()
            with self.assertRaises(RuntimeError):self.sc.request_control()

    def test_sendinput_failure_not_reported_as_release(self):
        from unittest.mock import patch
        from src.desktop import hotkey
        hotkey.track_key_press('F24')
        with patch.object(hotkey.user32,'GetAsyncKeyState',return_value=0x8000),patch.object(hotkey.user32,'SendInput',return_value=0):
            result=hotkey.release_all_keys_and_buttons()
        self.assertFalse(result['ok']);self.assertEqual(result['keys_released'],[])
        self.assertIn('0x87',hotkey.tracked_input()['keys'])

    def test_release_envelope_does_not_hide_tool_failure(self):
        from src.safety.cleanup import release_response_ok
        self.assertFalse(release_response_ok({'ok':True,'result':{'isError':True}}))
        self.assertFalse(release_response_ok({'ok':True,'result':{'content':[{'type':'text','text':'{"ok":false}'}]}}))
        self.assertTrue(release_response_ok({'ok':True,'result':{'content':[{'type':'text','text':'{"ok":true}'}]}}))

    def test_banner_lost_closes_input(self):
        self.sc.set_banner_probe(lambda:False)
        self.assertFalse(self.sc.allow_input)
        self.sc.release_control();self.sc.request_control();self.sc.banner_visible()
        with self.assertRaises(RuntimeError):self.sc.grant_control()

    def test_restart_preserves_human_stop(self):
        from src.safety.controller import SafetyController,SafetyState
        self.sc.trigger_human_override('fixture')
        restarted=SafetyController(state_path=self.sc.state_path)
        self.assertEqual(restarted.state,SafetyState.HUMAN_OVERRIDE)
        with self.assertRaises(RuntimeError):restarted.request_control()
        with self.assertRaises(RuntimeError):restarted.resume_safety()

    def test_corrupt_prior_state_is_latched_closed(self):
        from src.safety.controller import SafetyController,SafetyState
        self.sc.state_path.write_text('invalid json',encoding='utf-8')
        restarted=SafetyController(state_path=self.sc.state_path)
        self.assertEqual(restarted.state,SafetyState.EMERGENCY_STOP)
        with self.assertRaises(RuntimeError):restarted.request_control()

class HubLifecycle(unittest.TestCase):
    def test_hub_survives_launcher(self):
        import subprocess,os,signal
        from uah.core.transport import pick_free_port,probe_hub
        port=pick_free_port()
        code=f"from uah.core.daemon import ensure_persistent_hub; print(ensure_persistent_hub(port={port}), flush=True)"
        proc=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=15)
        self.assertEqual(proc.returncode,0,proc.stderr)
        url=f'http://127.0.0.1:{port}';health=probe_hub(url);self.assertIsNotNone(health)
        pid=health['pid']
        try:
            time.sleep(.15);self.assertEqual(probe_hub(url)['pid'],pid)
        finally:
            os.kill(pid,signal.SIGTERM)  # Only the test-owned detached Hub on its unique port.

if __name__=='__main__':unittest.main(verbosity=2)
