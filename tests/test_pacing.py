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


def test_hidden_scheduled_value_settles_without_an_action_and_survives_frozen_retry(tmp_path):
    scene=Scenario11.model_validate({"title":"时段","background":"双方独立决定。","domains":["场地"],
        "entities":[{"id":"a","name":"管理员","identity":"管理场地","fields":[{"id":"quota","domain":"场地","kind":"number","value":90}]},
                    {"id":"b","name":"访客","identity":"预约"}],
        "scheduled_events":[{"id":"limit","window":2,"text":"内部风阀计划已切换。","assignments":[{"entity_id":"a","field_id":"quota","value":30}]},
                            {"id":"invite","window":4,"text":"私下会面通知给访客。","visibility":{"readers":["b"]}}]})
    cfg=RunConfig11.model_validate({"windows":4,"rounds_per_window":1,"max_review_interval":4,"model":{"provider":"demo","model":"explicit-test"}})
    class Gateway:
        fail=True
        def complete(self,config,messages):
            d=json.loads(messages[-1]["content"])
            if d["task"]=="agent":
                assert "内部风阀计划" not in str(d)
                if d["actor_id"]=="a" or d["window"]<4: assert "私下会面通知" not in str(d)
                return '{"text":"等候进展。","review_at":8}'
            if self.fail and d["window"]==2:
                self.fail=False
                raise ModelError("结算暂不可用")
            effects=[s for e in d.get("external_events",[]) for s in e["assignments"]]
            review={"entity_id":"a","field_id":"quota","before":d["state"]["a"]["quota"],"after":d["state"]["a"]["quota"],"reason":"保持原值。"}
            return json.dumps({"reviews":[] if effects else [review],"consequences":[]})
    g=Gateway()
    sim=Simulation(scene,cfg,g,tmp_path/"external")
    assert sim.run()["status"]=="failed"
    sim=Simulation.restore(tmp_path/"external",g);sim.recover()
    assert sim.run()["status"]=="completed"
    notices=sim.journal.snapshot({"external_event"})
    change=sim.journal.settlement(2)["data"]["changes"][0]
    assert len(notices)==2 and change["event_ids"]==[notices[0]["id"]] and not change["action_ids"] and change["after"]==30
    assert not sim.journal.decision_context("b",2,1,sim._discarded)["observation"].get("external_events")
    wake=sim.journal.decision_context("b",4,1,sim._discarded)
    assert wake["resumed_plan"]["reason"]=="external_event" and wake["observation"]["external_events"][0]["record"]==notices[1]["id"]
    assert Simulation.restore(tmp_path/"external",g).state==sim.state
    altered=sim.journal.selected(transport_details=False)
    next(e for e in altered if e["kind"]=="external_event")["data"]["assignments"][0]["value"]=99
    with pytest.raises(ValueError,match="外部事件"):verify_commits(scene,cfg,altered)
