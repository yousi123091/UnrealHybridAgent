"""Real OS input/crash probes; optional motion stays inside a supplied blank canvas."""
import ctypes,json,os,signal,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
from src.safety.banner import build_safety_banner
from src.safety.guarded_input import GuardedInputClient

def down(vk):return bool(ctypes.windll.user32.GetAsyncKeyState(vk)&0x8000)
def setup():
    sc=get_safety_controller(state_path=Path(tempfile.mkdtemp())/'gate.json')
    banner=build_safety_banner(sc);sc.set_banner_available(banner.available)
    assert banner.start() and banner.visible
    sc.request_control(task='Guarded input isolated probe');sc.banner_visible();sc.grant_control()
    client=GuardedInputClient(sc.state_path, cancelled=lambda:sc.action_cancelled)
    return sc,banner,client

def child():
    sc,banner,client=setup()
    try:
        client.call('computer_key_press',{'key':'F24'})
        assert down(0x87)
        print(json.dumps({'owner':os.getpid(),'worker':client.pid,'guard':client.guard_pid,'folder':str(client.folder)}),flush=True)
        time.sleep(30)
    finally:sc.release_control();client.close();banner.stop();reset_safety_controller_for_tests()

def main():
    if '--child' in sys.argv:child();return 0
    rows=[]
    def record(case,passed,**evidence):
        row={'case':case,'status':'PASS' if passed else 'FAIL',**evidence};rows.append(row);print(json.dumps(row),flush=True)
    for index in range(10):
        sc,banner,client=setup()
        process=client.process
        try:
            client.call('computer_key_press',{'key':'F24'})
            assert down(0x87)
            client.call('computer_key_release',{'key':'F24'})
            assert not down(0x87)
        finally:sc.release_control();client.close();banner.stop();reset_safety_controller_for_tests()
        record('start_finish_'+str(index+1),not down(0x87) and process.poll() is not None,
               key_up=not down(0x87),worker_exit=process.poll())
    for killed in ('owner','worker','guard'):
        meta={}
        proc=subprocess.Popen([sys.executable,'-u',__file__,'--child'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            line=proc.stdout.readline()
            if not line:raise RuntimeError(proc.stderr.read())
            meta=json.loads(line);assert down(0x87)
            start=time.monotonic();os.kill(meta[killed],signal.SIGTERM)
            while down(0x87) and time.monotonic()-start<3:time.sleep(.005)
            record('kill_'+killed,not down(0x87),key_up=not down(0x87),elapsed_ms=round((time.monotonic()-start)*1000,2),**meta)
        finally:
            if proc.poll() is None:
                if meta.get('owner'):os.kill(meta['owner'],signal.SIGTERM)
                else:proc.terminate()
                proc.wait(5)
            # Harness cleanup is separate from the observed recovery result.
            if down(0x87):
                from src.safety.guarded_input import _send_input,key_event
                _send_input(key_event(0x87,True))
    if len(sys.argv)>2:
        x,y=map(int,sys.argv[1:3]);sc,banner,client=setup()
        try:
            import threading
            for i in range(10):client.call('computer_move_mouse',{'x':x+i,'y':y})
            errors=[]
            def drag():
                try:client.call('computer_drag',{'x1':x,'y1':y,'x2':x+150,'y2':y+20})
                except Exception as exc:errors.append(type(exc).__name__)
            thread=threading.Thread(target=drag);thread.start()
            time.sleep(.15);start=time.monotonic()
            # Freeze the published gate at ALLOWED: direct cancellation must still win.
            original_write=sc._write_state
            sc._write_state=lambda *args:None
            try:sc.trigger_human_override('SIMULATED_PROBE')
            finally:sc._write_state=original_write
            thread.join(3)
            pt=ctypes.wintypes.POINT();ctypes.windll.user32.GetCursorPos(ctypes.byref(pt));before=(pt.x,pt.y)
            time.sleep(.2);ctypes.windll.user32.GetCursorPos(ctypes.byref(pt));after=(pt.x,pt.y)
            record('mid_drag_revocation',not thread.is_alive() and bool(errors) and not down(1) and before==after,
                   callback='SIMULATED',gate_publication='FAULT_INJECTED_FROZEN_ALLOWED',key_up=not down(1),stationary_after=before==after,return_ms=round((time.monotonic()-start-.2)*1000,2),errors=errors,
                   real_physical_events=sc.human_override_detector.physical_events)
        finally:sc.release_control();client.close();banner.stop();reset_safety_controller_for_tests()
    out=ROOT/'logs/runs/p4bc_guarded_probe.json';out.write_text(json.dumps(rows,indent=2),encoding='utf-8')
    return int(any(r['status']=='FAIL' for r in rows))
if __name__=='__main__':raise SystemExit(main())
