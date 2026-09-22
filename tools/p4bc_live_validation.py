"""Real UE acceptance on a new disposable level; never edits the original level.

Fixture creation is setup, not attributed to Planner/Executor. Only Test 1/10
claim the real runtime path; other rows name their actual API path explicitly.
"""
from __future__ import annotations
import json,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.runtime import build_bundle
from src.core.config import load_config
from src.core.log import RunLogger
from src.core.checkpoint import CheckpointStore,TaskCheckpoint,capture_actors,rollback_task
from src.scheduler.executor import Executor
from src.planner.task import Plan,PlanStep
from src.skills.registry import get_skill
from src.adapters.unreal.batch_ops import UnrealBatchOps,_parse_execute,primitive_read_cost
from src.observation.task_observer import TaskObserver
from src.verification.outcome import OutcomeVerifier

def main():
    cfg=load_config(reload=True)
    # Test-owned routing/checkpoint state, keeping the user's health ledger intact.
    run=ROOT/'logs/runs'/('p4bc-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(parents=True)
    cfg.data.setdefault('workspace',{})['state_dir']=str(run/'state')
    cfg.data.setdefault('router',{}).update(stats_file=str(run/'routing.json'),quality_file=str(run/'quality.json'))
    cfg.data.setdefault('execution',{})['mode']='AUTO'
    b=build_bundle(cfg,only=('UNREAL_MCP','UE_PYTHON'))
    log=RunLogger(run,run_name='acceptance',console=False)
    exe=Executor(b,cfg,log);p=b.unreal['UNREAL_MCP'];batch=UnrealBatchOps(p)
    rows=[];fixture_started=False
    path=''
    def record(name,status,**evidence):
        row=dict(test=name,status=status,**evidence);rows.append(row)
        (run/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps(row,ensure_ascii=False,default=str),flush=True)
    def script(body):
        code='import unreal,json\n'+body+'\n__UHA_RESULT__={"ok":True,"result":payload}\ntry:\n unreal.MCPythonHelper.submit_result(json.dumps(payload))\nexcept Exception:\n print(json.dumps(payload))'
        return _parse_execute(p.execute_ue_python(code))
    def case(name,fn):
        try:
            evidence=fn();record(name,'PASS',**evidence)
        except Exception as exc:record(name,'FAIL',error=f'{type(exc).__name__}: {exc}')
    try:
        assert p.available(),'UE unavailable'
        state=script('payload={"level":str(unreal.EditorLevelLibrary.get_editor_world().get_path_name()),"dirty":[str(x.get_name()) for x in unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()]}')
        path=state['level'].split('.')[0]
        if state['dirty']:raise RuntimeError('Original level has unsaved changes; do not switch')
        if not path.startswith('/Game/UHAValidation/'):
            raise RuntimeError('Launch editor into a dedicated /Game/UHAValidation/ level first; world switching inside RPC is disabled')
        fixture_started=True
        setup=script(f'''ok=True
if any(str(a.get_actor_label()).startswith('UHAProbe_') for a in unreal.EditorLevelLibrary.get_all_level_actors()):
 raise RuntimeError('Use a fresh test fixture level; refusing to overwrite existing probe actors')
mesh=unreal.load_asset('/Engine/BasicShapes/Cube.Cube')
labels=[]
for i in range(36):
 a=unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(i*180,0,50))
 a.set_actor_label('UHAProbe_'+str(i))
 a.static_mesh_component.set_static_mesh(mesh)
 a.tags=[unreal.Name('GroundFurniture')]
 labels.append(str(a.get_actor_label()))
ground=unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(2500,0,-50))
ground.set_actor_label('UHAProbe_GroundFloor')
ground.static_mesh_component.set_static_mesh(mesh)
ground.set_actor_scale3d(unreal.Vector(70,30,1))
unreal.EditorLevelLibrary.save_current_level()
payload={{'created':ok,'labels':labels,'level':str(unreal.EditorLevelLibrary.get_editor_world().get_path_name())}}''')
        record('setup','PASS',fixture=setup,scope='already-open dedicated test level; no world replacement')
        def happy():
            from src.planner.planner import Planner
            plan=Planner().plan_from_text('把「UHAProbe_0」抬高 20，然后保存')
            result=exe.run_plan(plan)
            observed=list(p.get_actor_transform('UHAProbe_0').location)
            assert result.ok and abs(observed[2]-70)<1.5,(result.as_dict(),observed)
            return {'plan':plan.as_dict(),'runtime':result.as_dict(),'independent_readback':observed}
        case('1_structured_runtime',happy)
        def batch_edit():
            result=batch.batch_set_locations({'UHAProbe_1':[180,0,80],'UHAProbe_2':[360,0,90],'UHAProbe_MISSING':[0,0,0]})
            observed=batch.batch_read(['UHAProbe_1','UHAProbe_2','UHAProbe_MISSING'])
            verdict=OutcomeVerifier().verify({'type':'batch_set_locations','targets':{'UHAProbe_1':[180,0,80],'UHAProbe_2':[360,0,90],'UHAProbe_MISSING':[0,0,0]}},
                                             {'after':{r['label']:r.get('location') for r in observed['actors']}})
            assert result['success'] is False and verdict.status=='FAIL' and 'UHAProbe_MISSING' in verdict.affected_targets
            assert abs(observed['actors'][0]['location'][2]-80)<1
            return {'atomic':False,'write':result,'independent_readback':observed,'expected_partial_failure':verdict.as_dict()}
        case('2_batch_partial',batch_edit)
        def observation():
            observer=TaskObserver(p,task_id='acceptance')
            small=observer.observe(['UHAProbe_3','UHAProbe_4'])
            full=observer.observe(intent='basic',scope={'discover':True},max_neighbors=40)
            assert small.stats['scene_rows']==0 and small.stats['rtt']==1
            old=observer.observe(['UHAProbe_3'])
            p.set_actor_location('UHAProbe_3',[540,0,75])
            new,delta=observer.observe_delta(targets=['UHAProbe_3'])
            assert new.generation>old.generation and any(row.get('kind')=='changed' for row in delta.changed)
            # Unknown targets discovered from actual class/tags, not a fixed label lookup.
            discovered=script("payload={'actors':[{'label':str(a.get_actor_label()),'tags':[str(t) for t in a.tags]} for a in unreal.EditorLevelLibrary.get_all_level_actors() if 'GroundFurniture' in [str(t) for t in a.tags]]}")
            assert len(discovered['actors'])==36
            return {'scoped':small.as_dict(),'expanded_bytes':full.payload_size(),'delta':delta.as_dict(),'tag_selected_n':len(discovered['actors'])}
        case('3_scoped_semantic_observation',observation)
        def support():
            p.set_actor_location('UHAProbe_5',[900,0,200]);p.set_actor_location('UHAProbe_6',[1080,0,52])
            # Explicit support bounds, not an inferred global ground.
            read=batch.batch_read(['UHAProbe_4','UHAProbe_5','UHAProbe_6','UHAProbe_GroundFloor'])
            by={r['label']:r for r in read['actors']};floor=by['UHAProbe_GroundFloor']
            top=floor['bounds_origin'][2]+floor['bounds_extent'][2]
            gaps={k:v['bounds_origin'][2]-v['bounds_extent'][2]-top for k,v in by.items() if k!=floor['label']}
            def verdict(row):
                return OutcomeVerifier().verify({'type':'support_bounds','actor':row['label'],'support_model':'axis_aligned_solid_box'}, {'actor':row,'support':floor})
            assert verdict(by['UHAProbe_4']).status=='PASS'
            assert verdict(by['UHAProbe_5']).status=='FAIL'
            assert verdict(by['UHAProbe_6']).status=='FAIL'
            assert abs(gaps['UHAProbe_4'])<1 and gaps['UHAProbe_5']>100 and 0<gaps['UHAProbe_6']<=5
            p.set_actor_location('UHAProbe_5',[900,0,50])
            after=batch.batch_read(['UHAProbe_5'])['actors'][0]
            final_gap=after['bounds_origin'][2]-after['bounds_extent'][2]-top
            assert abs(final_gap)<1
            checked=verdict(after);assert checked.status=='PASS'
            return {'method':'production OutcomeVerifier with explicit solid box support; controlled axis-aligned fixtures only','gaps_cm':gaps,'final_gap_cm':final_gap,'independent_verification':checked.as_dict(),'evidence':read}
        case('4_support_repair',support)
        record('5_structured_to_GUI','NOT VERIFIED',reason='Separate guarded-input acceptance required; no fake GUI fallback via a structured call')
        store=CheckpointStore(run/'checkpoints')
        def recovery():
            cp=TaskCheckpoint(store.new_id(),time.time(),p.current_level(),'AUTO',{},capture_actors(p,['UHAProbe_7']))
            store.save(cp)
            p.set_actor_location('UHAProbe_7',[1260,0,140])
            got=list(p.get_actor_transform('UHAProbe_7').location)
            verdict=OutcomeVerifier().verify({'type':'set_location','target':[1260,0,150]},{'after':got})
            assert verdict.status=='FAIL'
            restored=rollback_task(p,store,cp.task_id);assert restored['ok']
            return {'expected_verify_failure':verdict.as_dict(),'rollback':restored}
        case('6_verify_failure_recovery',recovery)
        def reconnect():
            before=list(p.get_actor_transform('UHAProbe_8').location)
            cancelled=p.cancel()
            refused=False
            try:p.get_actor_transform('UHAProbe_8')
            except PermissionError:refused=True
            assert refused
            h=p.reconnect();after=list(p.get_actor_transform('UHAProbe_8').location)
            alt=b.unreal['UE_PYTHON'];alternative=list(alt.get_actor_transform('UHAProbe_8').location)
            assert h['state']=='healthy' and before==after==alternative
            return {'cancel':cancelled,'dispatch_refused':refused,'fresh_connection':h,'alternate_actual_read':alternative,'scope':'real session close/reconnect; network outage tested separately'}
        case('7_provider_reconnect',reconnect)
        def rollback():
            cp=TaskCheckpoint(store.new_id(),time.time(),p.current_level(),'AUTO',{},capture_actors(p,['UHAProbe_9']))
            store.save(cp);p.set_actor_rotation('UHAProbe_9',[10,20,30]);p.set_actor_scale('UHAProbe_9',[2,2,2]);p.set_actor_location('UHAProbe_9',[1620,0,99])
            restored=rollback_task(p,store,cp.task_id);assert restored['ok'];return {'rollback':restored}
        case('8_full_transform_rollback',rollback)
        def long_task():
            plan=Plan('real-five-step',[PlanStep('actor_find',{'actor':'UHAProbe_10'}),PlanStep('actor_inspect',{'actor':'UHAProbe_10'}),
                PlanStep('actor_move',{'actor':'UHAProbe_10','axis':'z','delta':10},prefer_method='UNREAL_MCP'),
                PlanStep('actor_inspect',{'actor':'UHAProbe_10'}),PlanStep('level_save',{},prefer_method='UNREAL_MCP')])
            result=exe.run_plan(plan);observed=list(p.get_actor_transform('UHAProbe_10').location)
            assert result.ok and abs(observed[2]-60)<1.5
            return {'plan':plan.as_dict(),'runtime':result.as_dict(),'final_readback':observed,'save_readback':p.level_file_stat()}
        case('10_five_step_runtime',long_task)
        def history():
            events=[json.loads(line) for line in log.jsonl.path.read_text(encoding='utf-8').splitlines()]
            kinds={x.get('kind') for x in events}
            assert 'provider_execute' in kinds and 'provider_response' in kinds and 'plan_start' in kinds
            return {'log':str(log.jsonl.path),'events':len(events),'kinds':sorted(str(k) for k in kinds)}
        case('9_audit',history)
        def performance():
            labels=['UHAProbe_'+str(i) for i in range(12)]
            primitive=primitive_read_cost(p,labels);batched=batch.batch_read(labels)
            assert len(batched['actors'])==12 and primitive['round_trips']==12
            return {'primitive':primitive,'batch_ms':batched['latency_ms'],'batch_calls':1,'roundtrip_reduction':11/12}
        case('performance_batch',performance)
    except Exception as exc:record('environment_or_setup','FAIL',error=f'{type(exc).__name__}: {exc}')
    finally:
        try:
            if fixture_started and path.startswith('/Game/UHAValidation/'):
                p.save_level()
                record('test_level_saved','PASS',level=path,original_world_not_loaded_or_modified=True)
        finally:b.close()
    print('EVIDENCE='+str(run),flush=True)
    return int(any(r['status']=='FAIL' for r in rows))
if __name__=='__main__':raise SystemExit(main())
