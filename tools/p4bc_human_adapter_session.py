"""Own an isolated real service for manual public-adapter chain acceptance."""
import os,sys,socket,subprocess,time,json,urllib.request,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
from src.safety.banner_win32 import Win32SafetyBanner
from src.adapters.computer_use import ComputerUseAdapter

run=ROOT/'logs/runs'/('human-adapter-service-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(parents=True)
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
url=f'http://127.0.0.1:{port}'
log=(run/'backend.log').open('wb');node=None;banner=None;cu=None
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
    sc=get_safety_controller(state_path=run/'preflight-gate.json')
    banner=Win32SafetyBanner(sc);sc.set_banner_available(banner.available)
    assert banner.start() and banner.visible
    cu=ComputerUseAdapter(url,client_id='uha-human-preflight',timeout=5)
    with cu.control():
        before=cu.peek_lock();assert before.get('holder')==cu.client_id
    after=cu.peek_lock();assert not after.get('holder')
    cu.close();cu=None;banner.stop();banner=None;reset_safety_controller_for_tests()
    result={'status':'PASS','scope':'real service handshake/acquire/release; no input injected','service_pid':node.pid,'url':url,'health':health,'lease_before':before,'lease_after':after}
    (run/'preflight.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
    child=subprocess.Popen([sys.executable,str(ROOT/'tools/p4bc_human_acceptance.py')],cwd=ROOT,env={**os.environ,'UHA_HUMAN_ADAPTER_URL':url},creationflags=subprocess.CREATE_NO_WINDOW)
    code=child.wait()
finally:
    if cu:cu.close()
    if banner:banner.stop()
    reset_safety_controller_for_tests()
    if node:
        node.terminate();node.wait(5)
    log.close()
sys.exit(code)
