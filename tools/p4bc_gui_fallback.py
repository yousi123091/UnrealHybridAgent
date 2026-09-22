"""Real UE Ctrl+S fallback after a controlled structured-save failure.

Pauses before input so the operator can inspect the actual target window.
"""
import sys,os,time,json,socket,subprocess,urllib.request,tempfile,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.runtime import build_bundle
from src.core.config import load_config
from src.core.log import RunLogger
from src.scheduler.executor import Executor
from src.skills.registry import get_skill
from src.adapters.unreal.batch_ops import _parse_execute
from src.adapters.computer_use import ComputerUseAdapter
from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
from src.safety.banner import build_safety_banner
from src.desktop.calibration import find_ue_window,SessionGuiCalibrator
from src.core.errors import TransportError

def _backend_cmd():
    """Resolve the local Computer Use backend launcher from environment.

    No machine-specific path is baked into this repository. Set
    UHA_AGENT_TARS_ROOT to the local install dir; optionally set UHA_NODE_EXE
    if node is not on PATH. Fails loudly when unconfigured (never guesses).
    """
    root=os.getenv('UHA_AGENT_TARS_ROOT','')
    if not root or not Path(root).is_dir():
        raise RuntimeError(
            'UHA_AGENT_TARS_ROOT is unset or not a directory; this live tool '
            'cannot locate the local Computer Use service'
        )
    return [os.getenv('UHA_NODE_EXE','node'),'server/index.js','--transport','http'],root

def main():
    run=ROOT/'logs/runs'/('gui-fallback-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(parents=True)
    cfg=load_config(reload=True);cfg.data.setdefault('execution',{})['mode']='AUTO'
    cfg.data.setdefault('workspace',{})['state_dir']=str(run/'state')
    cfg.data.setdefault('router',{}).update(stats_file=str(run/'routing.json'),quality_file=str(run/'quality.json'),max_attempts_per_method=1)
    cfg.data.setdefault('desktop',{}).update(gui_cache_file=str(run/'gui.json'),editor_focus_point=None)
    b=build_bundle(cfg,only=('UNREAL_MCP',));p=b.unreal['UNREAL_MCP']
    node=None;banner=None;original=None;save=None;switched=False
    def script(body):
        return _parse_execute(p.execute_ue_python('import unreal,json\n'+body+'\nunreal.MCPythonHelper.submit_result(json.dumps(payload))'))
    result={}
    try:
        state=script("payload={'level':str(unreal.EditorLevelLibrary.get_editor_world().get_path_name()),'dirty':[str(x.get_name()) for x in unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()]}")
        assert not state['dirty'],'Original scene has unsaved changes'
        path=state['level'].split('.')[0]
        assert path.startswith('/Game/UHAValidation/'),'Launch UE into a dedicated /Game/UHAValidation/ test level first; never switch worlds inside MCP tick'
        script("hits=[a for a in unreal.EditorLevelLibrary.get_all_level_actors() if a.get_actor_label()=='UHAGuiProbe']\nif not hits:\n a=unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(0,0,50))\n a.set_actor_label('UHAGuiProbe')\n a.static_mesh_component.set_static_mesh(unreal.load_asset('/Engine/BasicShapes/Cube.Cube'))\nelse:\n a=hits[0]\na.modify()\na.set_actor_location(unreal.Vector(0,0,50),False,False)\nunreal.EditorLevelLibrary.save_current_level()\npayload={'ok':True}")
        before=p.level_file_stat()
        script("a=next(a for a in unreal.EditorLevelLibrary.get_all_level_actors() if a.get_actor_label()=='UHAGuiProbe')\na.modify()\na.set_actor_location(unreal.Vector(0,0,80),False,False)\npayload={'changed':list(a.get_actor_location().to_tuple())}")
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        log=(run/'backend.log').open('wb')
        cmd,cwd=_backend_cmd()
        node=subprocess.Popen(cmd,cwd=cwd,env={**os.environ,'COMPUTER_USE_PORT':str(port),'COMPUTER_USE_HOST':'127.0.0.1','COMPUTER_USE_DRY_RUN':'false'},stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));url=f'http://127.0.0.1:{port}'
        for _ in range(100):
            try:
                with opener.open(url+'/health',timeout=.2) as response:health=json.load(response)
                break
            except Exception:time.sleep(.1)
        else:raise RuntimeError('Isolated desktop service unavailable')
        assert not health['dryRun']
        b.computer_use=ComputerUseAdapter(url,client_id='p4bc-gui-fallback',timeout=5)
        b.gateway.register('DESKTOP',b.computer_use,family='desktop')
        win=find_ue_window();assert win
        (run/'ready.json').write_text(json.dumps({'window':win['hwnd'],'title':win['title'],'run':str(run),'level':path},ensure_ascii=False),encoding='utf-8')
        print('READY='+str(run),flush=True)
        deadline=time.monotonic()+180
        while not (run/'go').exists() and time.monotonic()<deadline:time.sleep(.1)
        assert (run/'go').exists(),'No inspected-window go signal'
        calibration=SessionGuiCalibrator(run/'gui.json').calibrate(force=True)
        cfg.data['desktop']['editor_focus_point']=list(calibration.focus_point)
        sc=get_safety_controller(state_path=run/'gate.json');banner=build_safety_banner(sc)
        sc.set_banner_available(banner.available);assert banner.start() and banner.visible
        save=p.adapter.save_level
        attempts=[]
        def refused_save():
            attempts.append(time.time())
            raise TransportError('CONTROLLED_FAULT: structured save rejected before dispatch; test GUI fallback')
        p.adapter.save_level=refused_save
        logger=RunLogger(run,run_name='gui-fallback',console=False)
        executor=Executor(b,cfg,logger)
        outcome=executor.run(get_skill('level_save'),{'capture_screenshot':True},prefer_method='UNREAL_MCP')
        after=p.level_file_stat();location=list(p.get_actor_transform('UHAGuiProbe').location)
        passed=outcome.ok and outcome.method=='KEYBOARD' and bool(attempts) and after['mtime']>before['mtime'] and abs(location[2]-80)<1
        result={'status':'PASS' if passed else 'FAIL','fault':'explicit pre-dispatch structured-save rejection',
                'outcome':outcome.as_dict(),'before_file':before,'after_file':after,'readback':location,
                'structured_attempts':len(attempts),'calibration':calibration.as_dict(),'log':str(logger.jsonl.path)}
    except Exception as exc:result={'status':'FAIL','error':f'{type(exc).__name__}: {exc}'}
    finally:
        if save:p.adapter.save_level=save
        if banner:banner.stop()
        b.close();reset_safety_controller_for_tests()
        if node:
            node.terminate();node.wait(5)
        (run/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,default=str),flush=True)
    return int(result['status']!='PASS')
if __name__=='__main__':raise SystemExit(main())
