"""Real, isolated Agent-TARS backend + scratch-window lifecycle probes."""
import os,sys,time,json,ctypes,tempfile,subprocess,threading,urllib.request,socket
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
from src.safety.banner import build_safety_banner
from src.adapters.computer_use import ComputerUseAdapter
from src.desktop.hotkey import track_key_press,release_all_keys_and_buttons,clear_tracking
rows=[]
def record(case,status,detail,**fields):
    row=dict(case=case,status=status,detail=detail,**fields);rows.append(row);print(json.dumps(row),flush=True)
def down(vk):return bool(ctypes.windll.user32.GetAsyncKeyState(vk)&0x8000)
def main():
    x,y=map(int,sys.argv[1:3]);node=None;banner=None;cu=None
    with tempfile.TemporaryDirectory() as tmp:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        url=f'http://127.0.0.1:{port}'
        log=Path(tmp)/'node.log'
        stream=log.open('wb')
        try:
            node=subprocess.Popen(['E:/node/node.exe','server/index.js','--transport','http'],cwd='E:/MCP/Agent-TARS',
                env={**os.environ,'COMPUTER_USE_PORT':str(port),'COMPUTER_USE_HOST':'127.0.0.1','COMPUTER_USE_DRY_RUN':'false'},
                stdout=stream,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            for _ in range(60):
                try:
                    with urllib.request.urlopen(url+'/health',timeout=.3) as r:health=json.load(r)
                    break
                except Exception:time.sleep(.1)
            else:raise RuntimeError('isolated backend unavailable')
            assert not health['dryRun']
            sc=get_safety_controller(state_path=Path(tmp)/'gate.json')
            banner=build_safety_banner(sc);sc.set_banner_available(banner.available);assert banner.start()
            cu=ComputerUseAdapter(url,client_id='p011-owned-test',timeout=3)
            cu.connect(required=True)
            def grant():sc.request_control(task='P0 isolated scratch test');sc.banner_visible();cu.acquire_control()
            for i in range(10):
                grant();cu.move_mouse(x+20+i,y+20);cu.release_control()
                assert not sc.allow_input and not cu.peek_lock().get('holder')
            record('§24 / §31','PASS','real backend movement + release + closed gate + server lock empty, ten cycles; physical control not verified',cycles=10)
            grant();cu.drag(x+35,y+50,x+210,y+70)
            assert not down(1);cu.release_control()
            record('§28','PASS','real drag entirely inside scratch canvas completed; OS left-button up')

            grant();cu.key_press('F24');time.sleep(.05)
            held=down(0x87)
            if held:
                cu.key_release('F24');assert not down(0x87)
                record('§29','PASS','actual F24 key-down observed at OS, explicit release observed key-up')
            else:record('§29','FAIL','backend did not establish observable held F24')
            cu.release_control()

            # An in-flight real drag receives a synthetic revocation; scratch target only.
            grant();started=threading.Event();errors=[];old_call=cu._call
            def observed(tool,args=None):
                if tool=='computer_drag':started.set()
                return old_call(tool,args)
            cu._call=observed
            def drag():
                try:cu.drag(x+30,y+80,x+300,y+90)
                except Exception as exc:errors.append(type(exc).__name__)
            thread=threading.Thread(target=drag);thread.start();assert started.wait(1)
            time.sleep(.02);revoked=time.monotonic();sc.trigger_human_override('SIMULATED_TEST_EVENT')
            thread.join(4);elapsed=time.monotonic()-revoked
            assert not thread.is_alive()
            record('§30','FAIL','real backend call was already dispatched; no backend cancellation acknowledgement; synthetic takeover, not human validation',return_after_revoke_ms=round(elapsed*1000,2),adapter_errors=errors,button_down=down(1))
            cu._call=old_call;cu.release_control()
            # Only this synthetic test stop is resumed; actual events abort the test.
            if sc.human_override_detector.last_physical_kind:
                record('§25 / §26','NOT VERIFIED','physical activity observed; test stops without automatic resume')
                return
            sc.resume_safety(explicit=True)

            # Actual agent process dies while backend-injected F24 remains held.
            agent_code = """import sys,time,os,tempfile
from pathlib import Path
from src.safety.controller import get_safety_controller
from src.safety.banner import build_safety_banner
from src.adapters.computer_use import ComputerUseAdapter
c=get_safety_controller(state_path=Path(tempfile.mkdtemp())/'childgate.json')
b=build_safety_banner(c);c.set_banner_available(b.available);b.start()
c.request_control();c.banner_visible()
cu=ComputerUseAdapter(sys.argv[1],client_id='p011-child',timeout=3)
cu.acquire_control();cu.key_press('F24')
print(os.getpid(),flush=True)
time.sleep(30)
""".replace('\n+', '\n')
            child=subprocess.Popen([sys.executable,'-u','-c',agent_code,url],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                line=child.stdout.readline().strip()
                if not line.isdigit():raise RuntimeError('child agent could not acquire test backend')
                actual_pid=int(line)
                assert down(0x87)
                import signal
                os.kill(actual_pid,signal.SIGTERM);child.wait(5);time.sleep(.3)
                stuck=down(0x87)
                record('§26 / §27','FAIL' if stuck else 'PARTIAL','actual agent + co-located safety controller killed; backend remains alive; F24 OS state measured before harness-only cleanup',key_down_after_crash=stuck,child_pid=actual_pid)
                cu._call('computer_release_control',{'clientId':'p011-child','force':True})
                grant();cu.key_release('F24');cu.release_control();assert not down(0x87)
            finally:
                if child.poll() is None:child.kill();child.wait(5)

            # Kill only the isolated backend while its F24 is held. Parent ledger survives.
            grant();cu.key_press('F24');assert down(0x87)
            node.kill();node.wait(5)
            try:cu.move_mouse(x+30,y+30)
            except Exception:pass
            assert not sc.allow_input
            released=release_all_keys_and_buttons();time.sleep(.05)
            record('§25','PASS' if not down(0x87) else 'FAIL','actual test-owned CU worker killed with tracked F24 held; local gate closed and ledger cleanup checked at OS',key_down_after=down(0x87),cleanup=released)
            # Agent/controller crash is a separate architecture gap: neither owns a surviving ledger.

        except Exception as exc:
            record('live_lifecycle','FAIL',f'{type(exc).__name__}: {exc}')
        finally:
            # Test-owned F24/left ledger only. Never blanket-release human input.
            if down(0x87):track_key_press('F24')
            release_all_keys_and_buttons();clear_tracking()
            if cu:
                try:cu.release_control()
                except Exception:pass
                cu.close()
            if banner:banner.stop()
            reset_safety_controller_for_tests()
            if node and node.poll() is None:node.terminate();node.wait(5)
            stream.close()
            (ROOT/'logs/runs/p01_owned_backend.log').write_bytes(log.read_bytes())
            (ROOT/'logs/runs/p01_live_lifecycle.json').write_text(json.dumps({'p4b':'BLOCKED','rows':rows},indent=2),encoding='utf-8')
    return 1 if any(row['status']=='FAIL' for row in rows) else 0
if __name__=='__main__':raise SystemExit(main())
