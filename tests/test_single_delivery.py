import json
from copy import deepcopy

import pytest

from chuantuo.engine import Simulation
from chuantuo.experiments import ExperimentRequest, experiment_comparison, fork_experiment, in_transit_at
from chuantuo.schema import RunConfig11, Scenario11
from chuantuo.settlement import verify_commits


def test_only_the_selected_inflight_envelope_moves_and_nested_forks_preserve_its_postmark(tmp_path):
    scene=Scenario11.model_validate({"title":"双函","background":"两家独立办事。","domains":[],
        "entities":[{"id":a,"name":a,"identity":a} for a in "ab"],
        "mail_routes":[{"source":"a","target":"b","transit_windows":2}]})
    config=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":4,"rounds_per_window":1})
    class Send:
        def complete(self,model,messages):
            p=json.loads(messages[-1]["content"])
            if p["task"]=="world":return '{"reviews":[],"consequences":[]}'
            outgoing=[{"recipients":["b"],"text":x} for x in ("第一封","第二封")] if p["actor_id"]=="a" and p["window"]==1 else []
            return json.dumps({"text":"处理来往。","messages":outgoing},ensure_ascii=False)
    base=Simulation(scene,config,Send(),tmp_path/"base");assert base.run()["status"]=="completed"
    key,other=[e["id"] for e in base.journal.snapshot({"message_dispatched"})]
    def move(parent,after,due,name):
        experiment=ExperimentRequest(kind="delivery_time",window=after,through=4,delivery_move={"dispatch_id":key,"new_window":due})
        child=fork_experiment(parent,experiment,name*12,None,tmp_path/name)
        assert child.run()["status"]=="completed"
        return Simulation.restore(tmp_path/name,gateway=parent.gateway)
    later=move(base,1,4,"c")
    restored=move(later,2,3,"d")
    earlier=move(base,1,2,"e")
    for route,day in ((base,3),(later,4),(restored,3),(earlier,2)):
        assert route.journal.arrival(key,4,set())["window"]==day
        assert route.journal.arrival(other,4,set())["window"]==3
        assert route.journal.event(key)["data"]==base.journal.event(key)["data"]
    compare=experiment_comparison(base,earlier,2)["delivery_comparison"]["sides"]
    assert compare[0]["receipt"] is None and compare[1]["arrival"]==2
    with pytest.raises(ValueError,match="已经送达"):in_transit_at(earlier,key,2)
    damaged=deepcopy(later.journal.snapshot())
    damaged[0]["data"]["experiment"]["mail_timing"][0]["seal"]="tampered"
    with pytest.raises(ValueError,match="改期依据"):verify_commits(later.scenario,later.config,damaged)
