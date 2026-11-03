import json
from copy import deepcopy

import pytest

from chuantuo.engine import Simulation
from chuantuo.contrast import actor_contrast, field_contrast
from chuantuo.experiments import ExperimentRequest, experiment_comparison, fork_experiment
from chuantuo.interactions import conversation, dispatch_ledger
from chuantuo.models import ModelError
from chuantuo.schema import RunConfig11, Scenario11
from chuantuo.settlement import actor_evidence, verify_commits


class PostGateway:
    def __init__(self):
        self.fail_at_arrival = True
        self.decisions = []

    def complete(self, config, messages):
        data=json.loads(messages[-1]["content"])
        if data["task"]=="agent":
            owner,window,turn=data["actor_id"],data["window"],data["round"]
            self.decisions.append((owner,window,turn))
            out=[]
            if owner=="a" and window in (1,2) and turn==1:
                out=[{"recipients":["b"],"text":f"票号{window}"}]
            if owner=="c":
                assert "票号" not in str(data)
            return json.dumps({"text":"例行工作","messages":out})
        arrivals=[m for m in data.get("mail",[]) if m["status"]=="arrived"]
        if self.fail_at_arrival and arrivals:
            self.fail_at_arrival=False
            raise ModelError("送达后暂时无法结算")
        before=data["state"]["b"]["registered"]
        return json.dumps({"reviews":[{"entity_id":"b","field_id":"registered","before":before,"after":before+len(arrivals),
            "reason":"按本窗实际到件登记。","event_ids":[m["record"] for m in arrivals]}],"consequences":[]})


def test_mail_in_flight_is_inherited_private_and_settled_once(tmp_path):
    scene=Scenario11.model_validate({"title":"邮班","background":"三个独立办事点。","domains":["登记"],
        "entities":[{"id":k,"name":k,"identity":k,"fields":[{"id":"registered","kind":"number","value":0,"domain":"登记"}] if k=="b" else []}
                    for k in "abc"],"mail_routes":[{"source":"a","target":"b","transit_windows":2}]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":3,"rounds_per_window":2})
    g=PostGateway();run=Simulation(scene,cfg,g,tmp_path/"base")
    assert run.run()["status"]=="failed" and run.window==2
    at_two=dispatch_ledger(run,2)
    assert (at_two["pending"],at_two["arrived"])==(2,0)
    assert not [m for o in run.journal.private_history("b",2) if o["kind"]=="observation" for m in o["data"]["visible"]["messages"]]
    sent=run.journal.event(at_two["items"][0]["dispatch"])
    assert actor_evidence(sent,"a") and not actor_evidence(sent,"b")
    run=Simulation.restore(tmp_path/"base",g)
    decisions=len(g.decisions);run.recover()
    assert run.run()["status"]=="completed" and len(g.decisions)==decisions
    last=dispatch_ledger(run,3)
    arrived=next(m for m in last["items"] if m["stage"]=="arrived")
    assert last["pending"]==last["arrived"]==1 and last["ended"] and arrived["seen"]
    original=run.journal.directory/"events.jsonl"
    old_bytes=original.read_bytes()
    follow=fork_experiment(run,ExperimentRequest(kind="extend",window=3,through=4,extension_limit=7),"c"*12,None,tmp_path/"extended")
    assert follow.snapshot()["model_calls"]==0 and follow.state==run.state
    assert follow.run()["status"]=="completed" and follow.snapshot()["model_calls"]==7
    late=dispatch_ledger(follow,4)
    assert late["pending"]==0 and late["arrived"]==2 and original.read_bytes()==old_bytes
    comparison=experiment_comparison(run,follow,4)
    assert comparison["mode"]=="continuation" and comparison["first_change"]==4
    assert Simulation.restore(tmp_path/"extended",g).state==follow.state
    assert run.journal.settlement(3)["data"]["changes"][0]["event_ids"]==[arrived["receipt"]]
    assert conversation(run,"a","b")["items"][0]["seen"]["window"]==3
    child=fork_experiment(run,ExperimentRequest(window=1,through=3,kind="transit",route={"source":"a","target":"b","transit_windows":0}),
                          "a"*12,None,tmp_path/"branch")
    assert child.run()["status"]=="completed"
    letters=conversation(Simulation.restore(tmp_path/"branch",g),"a","b")["items"]
    assert [(m["text"],m["arrived_window"],m["seen"]["round"]) for m in letters]==[("票号2",2,2),("票号1",3,1)]
    assert actor_contrast(run,child,"a",1)["perception"] is None
    sight=actor_contrast(run,child,"a")["perception"]
    assert sight["window"]==2 and "已知通信时效" in {g["category"] for g in sight["groups"]}
    outsider=actor_contrast(run,child,"c")
    assert outsider["perception"] is None and "票号" not in str(outsider)
    focus=field_contrast(run,child,"b","registered",3)
    assert sum(focus["counts"].values())==2 and focus["counts"]["unknown"]==0
    history=deepcopy(run.journal.selected(transport_details=False))
    next(e for e in history if e["kind"]=="message_dispatched")["data"]["due_window"]=2
    with pytest.raises(ValueError,match="到达时间"):
        verify_commits(scene,cfg,history)
