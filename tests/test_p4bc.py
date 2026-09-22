"""Targeted failure-path regressions; real UE/OS evidence lives in tools probes."""
import sys,tempfile,unittest,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.observation.task_observer import TaskObserver
from src.observation.scene_model import TaskSceneModel
from src.observation.package import ActorFact,ObservationPackage
from src.core.checkpoint import CheckpointStore,TaskCheckpoint,ActorSnapshot,rollback_task
from src.providers.gateway import Provider,ProviderGateway
from src.router.capability_cache import CapabilityCache
from src.verification.outcome import OutcomeVerifier

class Scene:
    def __init__(self):self.rows={'A':{'label':'A','found':True,'location':[1,2,3],'tags':['fixture'],'visible':False}}
    def available(self):return True
    def call_domain(self,*args):raise AssertionError('Unexpected full world scan')
    def batch_read(self,labels):return {'actors':[self.rows.get(x,{'label':x,'found':False}) for x in labels],'round_trips':1}

class Tests(unittest.TestCase):
    def test_support_requires_geometry_and_footprint(self):
        v=OutcomeVerifier()
        evidence={'actor':{'bounds_origin':[0,0,50],'bounds_extent':[50,50,50]},
                  'support':{'bounds_origin':[0,0,-50],'bounds_extent':[500,500,50]}}
        action={'type':'support_bounds','support_model':'axis_aligned_solid_box'}
        self.assertEqual(v.verify({'type':'support_bounds'},evidence).status,'UNKNOWN')
        self.assertEqual(v.verify(action,evidence).status,'PASS')
        evidence['actor']['bounds_origin']=[1000,0,50]
        self.assertEqual(v.verify(action,evidence).status,'FAIL')
        evidence['actor']['bounds_origin']=[0,0,200]
        self.assertEqual(v.verify(action,evidence).status,'FAIL')

    def test_live_world_replacement_is_rejected(self):
        from src.adapters.unreal.script_policy import validate_editor_script
        from src.core.errors import NotSupported
        for name in ('new_level','load_level','open_level','new_level_from_template'):
            with self.assertRaises(NotSupported):
                validate_editor_script('unreal.EditorLevelLibrary.'+name+'("/Game/Test")')
        validate_editor_script('unreal.EditorLevelLibrary.get_all_level_actors()')

    def test_silent_real_child_has_bounded_stdio_timeout(self):
        from src.adapters.mcp_client import StdioTransport
        from src.core.errors import TransportError
        client=StdioTransport([sys.executable,'-u','-c','import time; time.sleep(30)'],timeout=.2)
        start=time.monotonic()
        try:
            with self.assertRaises(TransportError):client._request({'id':1,'method':'probe'})
            self.assertLess(time.monotonic()-start,3)
            process=client._proc
        finally:client.close()
        self.assertIsNotNone(process.poll())

    def test_invalid_batch_vectors_never_dispatch(self):
        from src.adapters.unreal.batch_ops import UnrealBatchOps
        class Backend:
            def execute_ue_python(self,code):raise AssertionError('Invalid vector must not dispatch')
        batch=UnrealBatchOps(Backend())
        for v in ([],[1,2],[1,2,3,4],[0,0,float('nan')],[0,float('inf'),0]):
            with self.assertRaises(ValueError):batch.batch_set_locations({'A':v})
            with self.assertRaises(ValueError):batch.batch_translate(['A'],v)

    def test_explicit_scope_never_full_scan(self):
        scene=Scene();observer=TaskObserver(scene,batch_ops=scene)
        package=observer.observe(['A']);self.assertEqual(package.stats['rtt'],1)
        self.assertFalse(package.targets[0].visible);self.assertEqual(package.targets[0].tags,['fixture'])
        self.assertIsNone(package.targets[0].support_hit)

    def test_external_change_delta_and_generation(self):
        scene=Scene();observer=TaskObserver(scene,batch_ops=scene)
        before=observer.observe(['A']);scene.rows['A']={**scene.rows['A'],'location':[1,2,30]}
        after,delta=observer.observe_delta(targets=['A'])
        self.assertGreater(after.generation,before.generation)
        self.assertEqual(delta.changed[0]['fields']['location']['from'],[1,2,3])

    def test_missing_actor_not_a_zero_location(self):
        scene=Scene();observer=TaskObserver(scene,batch_ops=scene)
        observer.observe(['A']);scene.rows.clear()
        package=observer.observe(['A'])
        self.assertEqual(package.targets,[]);self.assertEqual(observer.scene.snapshot_facts(),{})
        self.assertEqual(package.provenance['missing_targets'],['A'])

    def test_scope_shrink_drops_unobserved_facts(self):
        model=TaskSceneModel('t');model.apply_package(ObservationPackage('t','basic',1,targets=[ActorFact('A'),ActorFact('B')]))
        model.apply_package(ObservationPackage('t','basic',2,targets=[ActorFact('B')]))
        self.assertNotIn('A',model.snapshot_facts())

    def test_empty_readback_cannot_verify(self):
        v=OutcomeVerifier().verify({'type':'batch_set_locations','targets':{'A':[0,0,0]}},{'after':{'A':[]}})
        self.assertEqual(v.status,'FAIL')

    def test_wrong_level_refuses_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=CheckpointStore(tmp);store.save(TaskCheckpoint('t',0,'original','AUTO',{},[ActorSnapshot('A',[1,2,3])]))
            class Backend:
                def current_level(self):return 'another'
                def set_actor_location(self,*args):raise AssertionError('Must not mutate')
            with self.assertRaises(ValueError):rollback_task(Backend(),store,'t')

    def test_rotation_failure_is_not_successful_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=CheckpointStore(tmp);store.save(TaskCheckpoint('t',0,None,'AUTO',{},[ActorSnapshot('A',[1,2,3],[0,0,0])]))
            class Backend:
                def set_actor_location(self,*args):pass
                def set_actor_rotation(self,*args):raise RuntimeError('rotation unavailable')
            result=rollback_task(Backend(),store,'t');self.assertFalse(result['ok']);self.assertEqual(result['failed'],1)

    def test_provider_mutation_error_never_retries(self):
        class Backend:
            calls=0
            def set_actor_location(self):self.calls+=1;raise TimeoutError('uncertain write')
        backend=Backend();provider=Provider('test',backend)
        with self.assertRaises(TimeoutError):provider.execute('set_actor_location')
        self.assertEqual(backend.calls,1);self.assertEqual(provider.active,0)

    def test_cancel_requires_explicit_reconnect(self):
        class Backend:
            def available(self):return True
            def capabilities(self):return {'get_actors'}
            def get_actors(self):return ['actual response']
            def close(self):pass
        provider=Provider('test',Backend());provider.cancel()
        self.assertFalse(provider.available())
        with self.assertRaises(PermissionError):provider.get_actors()
        self.assertEqual(provider.reconnect()['state'],'healthy');self.assertEqual(provider.get_actors(),['actual response'])

    def test_reconnect_refuses_inflight(self):
        provider=Provider('test',object());provider.active=1
        with self.assertRaises(RuntimeError):provider.reconnect()

    def test_doctor_preserves_configuration_error(self):
        gateway=ProviderGateway();gateway.assembly_errors['MCP']='missing command'
        row=gateway.doctor()['providers']['MCP'];self.assertEqual(row['state'],'misconfigured');self.assertEqual(row['reason'],'missing command')

    def test_capability_cache_preserves_failure_detail(self):
        snapshot=CapabilityCache().get(lambda:{'MCP':{'available':False,'error':'TCP refused'}})
        self.assertFalse(snapshot.ready['MCP']);self.assertEqual(snapshot.errors['MCP'],'TCP refused')

if __name__=='__main__':unittest.main(verbosity=2)
