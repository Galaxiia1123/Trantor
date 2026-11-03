import json
from copy import deepcopy

import pytest

from chuantuo.engine import Simulation
from chuantuo.experiments import ExperimentRequest, fork_experiment
from chuantuo.models import ModelError
from chuantuo.schema import RunConfig11, Scenario11
from chuantuo.settlement import verify_commits


class WaitingGateway:
    def __init__(self, fail_world=False):
        self.decisions = []
        self.fail_world = fail_world

    def complete(self, config, messages):
        data=json.loads(messages[-1]["content"])
        if data["task"]=="world":
            if self.fail_world and data["window"]==4:
                self.fail_world=False
                raise ModelError("临时结算故障")
            return json.dumps({"reviews":[],"consequences":[]})
        actor,window,turn=data["actor_id"],data["window"],data["round"]
        self.decisions.append((actor,window,turn,(data.get("resume_plan") or {}).get("reason")))
        if actor=="b":
            assert "私下签章" not in str(data)
        result={"text":"等候复核后再行动。","review_at":99}
        if (actor,window,turn)==("a",1,1):
            result["matters"]=[{"key":"review","text":"第三窗主动核对签收。","due_window":3}]
        if (actor,window,turn)==("a",2,1):
            result["messages"]=[{"recipients":["b"],"text":"请查收普通进度通知。"}]
        if (actor,window,turn)==("a",3,1):
            result["matters"]=[{"key":"review","status":"closed"}]
        return json.dumps(result,ensure_ascii=False)


def test_wait_wakes_only_for_visible_change_delivery_deadline_or_review_and_survives_recovery(tmp_path):
    scene=Scenario11.model_validate({"title":"留样","background":"两个办事方独立工作。","domains":[],
        "entities":[{"id":i,"name":i,"identity":i} for i in ("a","b")],
        "rules":[{"text":"私下签章已送达","active_from":2,"visibility":{"readers":["a"]}}]})
    cfg=RunConfig11.model_validate({"windows":5,"rounds_per_window":2,"max_review_interval":3,
                                    "model":{"provider":"demo","model":"explicit-test"}})
    strict=Simulation(scene,cfg.model_copy(update={"max_review_interval":0}),WaitingGateway(),tmp_path/"strict")
    assert strict.run()["model_calls"]==25
    gateway=WaitingGateway(fail_world=True)
    sim=Simulation(scene,cfg,gateway,tmp_path/"paced")
    assert sim.run()["status"]=="failed" and sim.window==3
    restored=Simulation.restore(tmp_path/"paced",gateway)
    restored.recover()
    result=restored.run()
    assert result["status"]=="completed" and result["deferred_reviews"]==14
    assert result["model_calls"]==12  # six decisions, five settlements, one failed settlement call
    assert sorted(gateway.decisions)==[
        ("a",1,1,None),("a",2,1,"visible_change"),("a",3,1,"due"),
        ("b",1,1,None),("b",2,2,"message"),("b",5,1,"due")]
    events=restored.journal.selected(transport_details=False)
    assert verify_commits(scene,cfg,events)["verified_expressions"]==6
    view=restored.journal.decision_context("b",2,1,restored._discarded)
    assert view["deferred"] and not view["observation"]["messages"] and "input_id" not in view
    altered=deepcopy(events)
    next(e for e in altered if e["kind"]=="decision_deferred")["data"]["review_at"]=99
    with pytest.raises(ValueError,match="等待记录"):
        verify_commits(scene,cfg,altered)
    fork=fork_experiment(restored,ExperimentRequest(window=1,through=3,kind="repeat"),"c"*12,None,tmp_path/"fork")
    assert fork.run()["status"]=="completed" and fork.state==strict.state
    assert Simulation.restore(tmp_path/"fork",gateway).snapshot()["deferred_reviews"]==5
