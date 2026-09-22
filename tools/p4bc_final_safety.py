"""Bounded real Windows faults; no physical input is simulated or claimed human."""
import ctypes,json,os,signal,subprocess,sys,tempfile,time,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.safety.guarded_input import GuardedInputClient,InputWorker,key_event,_send_input,atomic_json
from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
from src.safety.banner_win32 import Win32SafetyBanner
from src.safety.human_override import HumanOverrideDetector

def down():return bool(ctypes.windll.user32.GetAsyncKeyState(0x87)&0x8000)
def setup():
    folder=Path(tempfile.mkdtemp(prefix='uha-final-safety-'))
    sc=get_safety_controller(state_path=folder/'gate.json')
    banner=Win32SafetyBanner(sc);sc.set_banner_available(banner.available)
    assert banner.start() and banner.visible
    sc.request_control(task='Final safety fault test');sc.banner_visible();sc.grant_control()
    return sc,banner

def child(kind):
    sc,banner=setup();client=None
    try:
        if kind in ('unhook','pump_stall','silent_unhook'):
            folder=Path(tempfile.mkdtemp(prefix='uha-worker-fault-'))
            worker=InputWorker(folder,sc.state_path,os.getpid())
            worker.execute('computer_key_press',{'key':'F24'});assert down()
            release=threading.Event();restore=None
            start=time.monotonic()
            try:
                if kind=='unhook':worker.detector.stop()
                elif kind=='pump_stall':
                    from src.safety import human_override as hm
                    restore=hm.user32.PeekMessageW
                    def stalled(*args):
                        if threading.get_ident()==worker.detector._hook_thread.ident:release.wait(2)
                        return restore(*args)
                    hm.user32.PeekMessageW=stalled
                else:
                    from src.safety.human_override import user32
                    assert user32.UnhookWindowsHookEx(worker.detector._mouse_hook)
                    assert user32.UnhookWindowsHookEx(worker.detector._kb_hook)
                deadline=time.monotonic()+2
                while down() and time.monotonic()<deadline:time.sleep(.005)
                print(json.dumps({'key_up':not down(),'elapsed_ms':(time.monotonic()-start)*1000,'revoked':worker.revoked.is_set(),'detector':worker.detector.stats(),'folder':str(folder)}),flush=True)
            finally:
                release.set()
                if restore:
                    from src.safety import human_override as hm
                    hm.user32.PeekMessageW=restore
                worker.close()
            return
        client=GuardedInputClient(sc.state_path,cancelled=lambda:sc.action_cancelled)
        client.call('computer_key_press',{'key':'F24'});assert down()
        print(json.dumps({'owner':os.getpid(),'worker':client.pid,'guard':client.guard_pid,'folder':str(client.folder)}),flush=True)
        time.sleep(20)
    finally:
        sc.release_control()
        if client:client.close()
        banner.stop();reset_safety_controller_for_tests()

def main():
    if '--child' in sys.argv:child(sys.argv[-1]);return 0
    run=ROOT/'logs/runs'/('final-safety-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(parents=True)
    rows=[]
    def record(name,status,**data):
        row=dict(case=name,status=status,**data);rows.append(row)
        (run/'results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8');print(json.dumps(row),flush=True)
    # Actual native unhook results plus no callbacks after removal.
    det=HumanOverrideDetector();det.start();assert det.hook_alive
    det.stop();stats=det.stats();before=det.injected_events
    _send_input(key_event(0x87));_send_input(key_event(0x87,True));time.sleep(.05)
    record('native_unhook', 'PASS' if stats['cleanup_complete'] and len(stats['unhook_results'])==2 and all(x['ok'] for x in stats['unhook_results']) and det.injected_events==before else 'FAIL',stats=stats,no_callback_after_unhook=det.injected_events==before)
    for cycle in range(30):
        sc,banner=setup();client=None
        try:
            client=GuardedInputClient(sc.state_path,cancelled=lambda:sc.action_cancelled)
            client.call('computer_key_press',{'key':'F24'});assert down()
            client.call('computer_key_release',{'key':'F24'});assert not down()
            sc.release_control();client.close()
            clean=json.loads((client.folder/'hook-cleanup.json').read_text())
            record('lifecycle_'+str(cycle+1),'PASS' if clean['cleanup_complete'] and not down() and client.process.poll()==0 else 'FAIL',worker_exit=client.process.poll(),hook_cleanup=clean['cleanup_complete'])
        finally:
            if client and client.process.poll() is None:client.close()
            banner.stop();reset_safety_controller_for_tests()
        record('banner_cleanup_'+str(cycle+1),'PASS' if len(banner.cleanup_results)==2 and all(r.get('unregistered') for r in banner.cleanup_results) and banner._thread is None else 'FAIL',classes_unregistered=banner.cleanup_results,thread_exited=banner._thread is None)
    for fault in ('missing_gate','corrupt_gate','expired_lease','heartbeat_false','banner_false','cancel_frozen_gate'):
        sc,banner=setup();client=None
        try:
            client=GuardedInputClient(sc.state_path,cancelled=lambda:sc.action_cancelled)
            client.call('computer_key_press',{'key':'F24'});assert down()
            start=time.monotonic();data=json.loads(sc.state_path.read_text(encoding='utf-8'))
            if fault=='missing_gate':sc.state_path.unlink()
            elif fault=='corrupt_gate':sc.state_path.write_text('{bad',encoding='utf-8')
            elif fault=='cancel_frozen_gate':sc._action_cancel_event.set()
            else:
                data[{'expired_lease':'lease_expires_at','heartbeat_false':'heartbeat_ok','banner_false':'banner_visible'}[fault]]=0 if fault=='expired_lease' else False
                atomic_json(sc.state_path,data)
            while down() and time.monotonic()-start<3:time.sleep(.005)
            refused=False
            try:client.call('computer_key_press',{'key':'F24'})
            except PermissionError:refused=True
            record(fault,'PASS' if not down() and refused else 'FAIL',key_up=not down(),new_input_refused=refused,elapsed_ms=(time.monotonic()-start)*1000)
        finally:
            sc.release_control()
            if client:client.close()
            banner.stop();reset_safety_controller_for_tests()
    for killed in (('owner',),('worker',),('guard',),('owner','worker'),('owner','guard'),('guard','worker')):
        proc=subprocess.Popen([sys.executable,'-u',__file__,'--child','crash'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        meta={}
        try:
            line=proc.stdout.readline();assert line,proc.stderr.read();meta=json.loads(line);assert down()
            start=time.monotonic()
            for key in killed:
                try:os.kill(meta[key],signal.SIGTERM)
                except ProcessLookupError:pass
            while down() and time.monotonic()-start<3:time.sleep(.005)
            record('kill_'+'_'.join(killed),'PASS' if not down() else 'FAIL',key_up=not down(),elapsed_ms=(time.monotonic()-start)*1000,folder=meta['folder'])
        finally:
            if proc.poll() is None:proc.terminate()
            proc.wait(5)
            if down():_send_input(key_event(0x87,True))
            time.sleep(.1)
    for kind in ('unhook','pump_stall','silent_unhook'):
        proc=subprocess.run([sys.executable,'-u',__file__,'--child',kind],cwd=ROOT,capture_output=True,text=True,timeout=12,creationflags=subprocess.CREATE_NO_WINDOW)
        try:data=json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:data={'error':proc.stderr[-1800:]}
        record(kind,'PASS' if proc.returncode==0 and data.get('key_up') else 'FAIL',exit_code=proc.returncode,**data)
        if down():_send_input(key_event(0x87,True))
    print('EVIDENCE='+str(run),flush=True)
    return int(any(x['status']=='FAIL' for x in rows))
if __name__=='__main__':raise SystemExit(main())
