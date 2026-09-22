"""Automatic, non-injecting lifecycle fault probes. Scope is explicit per verdict."""
import sys,os,time,json,tempfile,threading,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.safety import controller as module
from src.adapters.computer_use import ComputerUseAdapter
from src.desktop.hotkey import clear_tracking,tracked_input

results=[]
def record(case,status,scope,**data):
    row=dict(case=case,status=status,scope=scope,**data);results.append(row);print(json.dumps(row),flush=True)

def main():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'gate.json'
        sc=module.SafetyController(state_path=path,injection_lease_s=.3)
        module._CONTROLLER=sc
        sc.set_banner_available(True)
        def grant():sc.request_control();sc.banner_visible();sc.grant_control()
        grant()
        for i in range(10):
            sc.release_control();assert not sc.allow_input;grant()
        record('§24 / §31','PARTIAL','10 real controller start-finish cycles; no physical input or backend',cycles=10)

        # Actual killed child owns its controller/file; no GUI worker is simulated as real CU.
        child_code='''import sys,time,os
from pathlib import Path
from src.safety.controller import SafetyController
c=SafetyController(state_path=Path(sys.argv[1]),injection_lease_s=.3)
c.set_banner_available(True);c.request_control();c.banner_visible();c.grant_control()
print(os.getpid(),flush=True)
time.sleep(30)
'''
        child=subprocess.Popen([sys.executable,'-u','-c',child_code,str(path)],cwd=ROOT,stdout=subprocess.PIPE,text=True)
        import signal
        actual_pid=int(child.stdout.readline().strip())
        os.kill(actual_pid,signal.SIGTERM);child.wait(5);time.sleep(.4)
        assert not module.external_gate_allows_input(path)
        record('§26 / §27','PARTIAL','real controller-owner process killed; on-disk lease expires; no independently cancellable CU worker',exit_code=child.returncode)

        # Real refused TCP connection, with production adapter failure/cleanup paths.
        grant();cu=ComputerUseAdapter('http://127.0.0.1:1',timeout=.5)
        try:cu.move_mouse(0,0)
        except Exception as error:record('§25','PARTIAL','real transport failure closes local gate; not a kill during live OS injection',error=type(error).__name__,gate_closed=not sc.allow_input)
        else:raise AssertionError('unexpected transport success')
        assert not sc.allow_input
        sc.resume_safety(explicit=True)

        for action in ('drag','key_press'):
            grant();clear_tracking()
            cu=ComputerUseAdapter('http://127.0.0.1:1',dry_run=True)
            def failure(*a,**k):raise ConnectionError('fixture fails after dispatch')
            cu._call=failure
            try:
                if action=='drag':cu.drag(0,0,1,1)
                else:cu.key_press('shift')
            except ConnectionError:pass
            ledger=tracked_input();assert ledger['buttons'] or ledger['keys']
            record('§28' if action=='drag' else '§29','PARTIAL','failure fixture retains ledger; no physical held input',ledger=ledger)
            clear_tracking();sc.resume_safety(explicit=True)

        grant();started=threading.Event();finish=threading.Event();completed=[];errors=[]
        cu=ComputerUseAdapter('http://127.0.0.1:1',dry_run=True)
        def blocking(*a,**k):started.set();finish.wait(2);completed.append('backend finished after revoke');return {'data':{}}
        cu._call=blocking
        def action():
            try:cu.move_mouse(1,1)
            except Exception as e:errors.append(type(e).__name__)
        thread=threading.Thread(target=action);thread.start();assert started.wait(1)
        sc.trigger_human_override('SIMULATED');time.sleep(.05)
        still_running=thread.is_alive();finish.set();thread.join(2)
        record('§30','FAIL','blocking backend fixture continues after revoke: UHA cannot cancel already-dispatched call',still_running_after_override=still_running,completed=completed,adapter_errors=errors)
        assert still_running and not sc.allow_input
        module.reset_safety_controller_for_tests()
    dest=ROOT/'logs/runs/p01_lifecycle_revalidation.json'
    dest.write_text(json.dumps({'p4b':'BLOCKED','results':results},indent=2),encoding='utf-8')
    print('Evidence:',dest)
    # Findings are intentionally not converted into passing acceptance.
    return 1 if any(r['status']=='FAIL' for r in results) else 0

if __name__=='__main__':raise SystemExit(main())
