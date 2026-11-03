"""Open a local scripted comparison without recreating previously finished routes."""
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import Simulation
from .experiments import fork_experiment


def sample_pair(root, gateway, scenario, config, spec, choice, window):
    root.mkdir(parents=True,exist_ok=True)
    model=config.model.model
    def obtain(label,make):
        rid=uuid.uuid5(uuid.NAMESPACE_URL,"trantor:"+model+":"+label).hex[:12]
        target=root/rid
        if target.is_dir():
            saved=Simulation.restore(target,gateway)
            if saved.run_id!=rid or saved.config.model.model!=model or saved.status!="completed":
                raise ValueError("同名案例记录不完整，请使用其他资料目录以保留原件")
            return saved
        with TemporaryDirectory(dir=root,prefix="_case-") as folder:
            stage=Path(folder)/"run"
            sim=make(rid,stage)
            if sim.run()["status"]!="completed":
                raise ValueError("本机案例未完成："+str(sim.error))
            stage.rename(target)
            sim.journal.directory=target
            return sim
    parent=obtain("baseline",lambda rid,path:Simulation(scenario,config,gateway,path,_run_id=rid))
    variant=obtain(choice,lambda rid,path:fork_experiment(parent,spec,rid,None,path))
    return {"run_id":variant.run_id,"parent_id":parent.run_id,"window":window}
