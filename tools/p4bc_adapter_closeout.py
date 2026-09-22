"""Post-hardening public adapter regression. Synthetic cancellation, not human."""
import os,sys,socket,subprocess,time,json,urllib.request,ctypes
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
from src.safety.banner import build_safety_banner
from src.adapters.computer_use import ComputerUseAdapter
run=ROOT/'logs/runs'/('adapter-closeout-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(parents=True)
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
url=f'http://127.0.0.1:{port}';node=banner=cu=None;result={'status':'FAIL'}
log=(run/'backend.log').open('wb')
try:
    node=subprocess.Popen(['E:/node/node.exe','server/index.js','--transport','http'],cwd='E:/MCP/Agent-TARS',env={**os.environ,'COMPUTER_USE_PORT':str(port),'COMPUTER_USE_HOST':'127.0.0.1','COMPUTER_USE_DRY_RUN':'false'},stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            with opener.open(url+'/health',timeout=.3) as response:health=json.load(response)
            break
        except Exception:time.sleep(.1)
    else:raise RuntimeError('Desktop service unavailable')
    assert health.get('dryRun') is False
    sc=get_safety_controller(state_path=run/'gate.json');banner=build_safety_banner(sc)
    sc.set_banner_available(banner.available);assert banner.start() and banner.visible
    cu=ComputerUseAdapter(url,client_id='uha-final-adapter',timeout=5)
    with cu.control():
        before=cu.peek_lock();assert before.get('holder')==cu.client_id
        cu.key_press('F24');assert ctypes.windll.user32.GetAsyncKeyState(0x87)&0x8000
        worker=cu._input_worker
        time.sleep(1.2)  # exercise repeated hook renewal while holding a real key
        assert ctypes.windll.user32.GetAsyncKeyState(0x87)&0x8000
        sc.trigger_human_override('AUTOMATED_CLOSEOUT_NOT_HUMAN')
        blocked=False
        try:cu.key_press('F24')
        except PermissionError:blocked=True
        assert blocked
    after=cu.peek_lock();assert not after.get('holder')
    assert sc.state.value=='HUMAN_OVERRIDE' and not sc.allow_input
    assert not ctypes.windll.user32.GetAsyncKeyState(0x87)&0x8000
    cleanup=json.loads((worker.folder/'hook-cleanup.json').read_text(encoding='utf-8'))
    assert cleanup['cleanup_complete'] and cleanup['refresh_count']>=3
    result={'status':'PASS','cancellation':'SIMULATED; prior human evidence remains separate','lease_before':before,'lease_after':after,'key_up':True,'terminal_after_release':sc.state.value,'hook_cleanup':cleanup,'worker_exit':worker.process.poll()}
except Exception as exc:result.update(error=type(exc).__name__+': '+str(exc))
finally:
    if cu:cu.close()
    if banner:banner.stop()
    reset_safety_controller_for_tests()
    if node:node.terminate();node.wait(5)
    log.close()
    (run/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
    print('EVIDENCE='+str(run),flush=True)
sys.exit(int(result['status']!='PASS'))
