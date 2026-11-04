import json

import pytest

from chuantuo.checkpoints import arm_goal, cancel_goal, pause_goals
from chuantuo.engine import Simulation
from chuantuo.experiments import ExperimentRequest, fork_experiment
from chuantuo.models import ModelConfig
from chuantuo.observatory import FieldWatch
from chuantuo.schema import RunConfig11, Scenario11


class CalendarGateway:
    def __init__(self):
        self.calls = 0

    def complete(self, config, messages):
        self.calls += 1
        assert "operator-only-target" not in json.dumps(messages)
        data = json.loads(messages[-1]["content"])
        if data["task"] == "agent":
            return '{"text":"查看公告，决定等候。","messages":[]}'
        reviews = [{"entity_id":a,"field_id":f,"before":data["state"][a][f],"after":data["state"][a][f],"reason":"保持原值。"}
                   for a,fields in data["review_fields"].items() for f in fields]
        return json.dumps({"reviews":reviews,"consequences":[]})


def calendar_run(path, gateway):
    scene = Scenario11.model_validate({"title":"公告","background":"虚构公告日程。","domains":["公告"],
        "entities":[{"id":"a","name":"管理方","identity":"管理公告","fields":[
            {"id":"posted","kind":"boolean","value":None,"domain":"公告"}]},
            {"id":"b","name":"来访者","identity":"自主决定"}],
        "scheduled_events":[{"id":"notice","window":2,"text":"公告挂出。",
                             "assignments":[{"entity_id":"a","field_id":"posted","value":True}]}]})
    config = RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":3,"rounds_per_window":1})
    return Simulation(scene, config, gateway, path)


def test_pause_goal_survives_restart_runs_once_and_stays_out_of_branches(tmp_path):
    gateway = CalendarGateway()
    sim = calendar_run(tmp_path/"base", gateway)
    question = FieldWatch(actor_id="a",field_id="posted",operator="equals",target=True,title="operator-only-target")
    goal = arm_goal(sim,"a"*32,question)
    assert arm_goal(sim,"a"*32,question) == goal
    assert len(sim.journal.snapshot({"pause_goal_set"})) == 1
    result = sim.run()
    assert result["status"] == "paused" and result["window"] == 2 and gateway.calls == 6
    assert result["pause_goals"][0]["status"] == "reached"
    commit = sim.journal.settlement(2)["id"]
    assert result["pause_goals"][0]["commit_id"] == commit
    restored = Simulation.restore(tmp_path/"base", gateway)
    assert restored.run()["status"] == "completed" and gateway.calls == 9
    child = fork_experiment(restored,ExperimentRequest(kind="repeat",window=1,through=3),"c"*12,None,tmp_path/"child")
    assert pause_goals(child) == [] and child.run()["status"] == "completed"
    with pytest.raises(ValueError,match="已经结束"):
        arm_goal(restored,"b"*32,question)


def test_goal_rechecks_after_interrupted_boundary_and_unknown_deadline_expires(tmp_path,monkeypatch):
    gateway = CalendarGateway()
    sim = calendar_run(tmp_path/"interrupted",gateway)
    question = FieldWatch(actor_id="a",field_id="posted",operator="equals",target=True)
    arm_goal(sim,"a"*32,question)
    append = sim.journal.append
    def fail_receipt(kind,data,window=0):
        if kind == "pause_goal_reached":
            raise OSError("fixture interrupted write")
        return append(kind,data,window)
    monkeypatch.setattr(sim.journal,"append",fail_receipt)
    assert sim.run()["status"] == "failed" and sim.window == 2 and gateway.calls == 6
    changed=fork_experiment(sim,ExperimentRequest(kind="model_continue",window=2,through=3,extension_limit=5),
                            "e"*12,None,tmp_path/"changed",continuation_model=ModelConfig(
                                provider="custom",model="current-model",base_url="http://127.0.0.1:9320"))
    assert pause_goals(changed)[0]["armed_after"]==0
    assert changed.run()["status"]=="paused" and changed.window==2 and gateway.calls==6
    assert pause_goals(changed)[0]["status"]=="reached"
    assert pause_goals(sim)[0]["status"]=="watching"
    restored = Simulation.restore(tmp_path/"interrupted",gateway)
    restored.recover()
    assert restored.run()["status"] == "paused" and restored.window == 2 and gateway.calls == 6
    assert len(restored.journal.snapshot({"pause_goal_reached"})) == 1
    other = calendar_run(tmp_path/"deadline",gateway)
    arm_goal(other,"b"*32,question.model_copy(update={"deadline":1}))
    arm_goal(other,"c"*32,question)
    cancel_goal(other,"c"*32)
    arm_goal(other,"d"*32,question.model_copy(update={"target":False}))
    assert other.run()["status"] == "completed"
    assert [g["status"] for g in pause_goals(other)] == ["expired","cancelled","ended"]
