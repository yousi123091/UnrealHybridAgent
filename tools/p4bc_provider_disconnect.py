"""Kill only the MCP subprocess created by this probe, then use real UE Python."""
import sys,json,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.runtime import build_bundle
from src.core.config import load_config
from src.core.log import RunLogger
from src.scheduler.executor import Executor
from src.skills.registry import get_skill

def main():
    cfg=load_config(reload=True);run=ROOT/'logs/runs'/('disconnect-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir()
    cfg.data.setdefault('router',{}).update(stats_file=str(run/'health.json'),quality_file=str(run/'quality.json'))
    b=build_bundle(cfg,only=('UNREAL_MCP','UE_PYTHON'));log=RunLogger(run,run_name='disconnect',console=False)
    try:
        p=b.unreal['UNREAL_MCP'];assert p.available()
        actors=p.get_actors(limit=1);assert actors
        label=actors[0].label or actors[0].name
        before=list(p.get_actor_transform(label).location)
        process=p.adapter._client._proc;assert process and process.poll() is None
        pid=process.pid;process.terminate();process.wait(5)
        failed=p.health();assert failed['state']=='disconnected',failed
        executor=Executor(b,cfg,log);executor.capability_cache.invalidate('real process killed')
        result=executor.run(get_skill('actor_find'),{'actor':label},prefer_method='UNREAL_MCP')
        after=list(b.unreal['UE_PYTHON'].get_actor_transform(label).location)
        assert result.ok and result.method=='UE_PYTHON' and before==after
        reconnected=p.reconnect();assert reconnected['state']=='healthy'
        assert list(p.get_actor_transform(label).location)==before
        evidence={'status':'PASS','killed_owned_stdio_pid':pid,'exit_code':process.returncode,'failed_health':failed,
                  'runtime_alternative':result.as_dict(),'readback':after,'reconnect':reconnected,'log':str(log.jsonl.path)}
        (run/'result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(evidence,ensure_ascii=False),flush=True)
    finally:b.close()
if __name__=='__main__':main()
