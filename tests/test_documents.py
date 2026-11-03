import json
from copy import deepcopy

import pytest

from chuantuo.documents import document_history
from chuantuo.engine import Simulation
from chuantuo.experiments import ExperimentRequest, fork_experiment
from chuantuo.models import ModelError
from chuantuo.schema import RunConfig11, Scenario11, Settlement, Source
from chuantuo.settlement import settled_state, verify_commits
from chuantuo.source_contract import reconcile


BODY="来料编号 G73，复核印记为蓝杉。"


class HandoffGateway:
    def __init__(self):
        self.fail_once=True
        self.denials=[]

    def complete(self,config,messages):
        p=json.loads(messages[-1]["content"]); w=p["window"]
        if p["task"]=="world":
            if w==2 and self.fail_once:
                self.fail_once=False
                raise ModelError("到件后暂缓结算")
            return json.dumps({"reviews":[],"consequences":[]})
        who,turn,view=p["actor_id"],p["round"],p["observation"]
        held={doc["id"] for doc in view.get("documents",())}
        recipients=[]
        if who=="d":
            assert BODY not in str(p) and not held
            if (w,turn)==(1,1):
                if "output_correction" not in p: recipients=["c"]
                else:self.denials.append("unheld")
        if who=="a" and (w,turn)==(1,1):
            assert "slip" in held
            recipients=["b"]
        if who=="b":
            if w==1: assert BODY not in str(p) and not held
            if (w,turn)==(2,1):
                assert view["messages"][0]["enclosures"][0]["text"]==BODY and "slip" in held
                recipients=["c"] if "output_correction" in p else ["c","d"]
                if "output_correction" in p:self.denials.append("scope")
        if who=="c" and (w,turn)<(2,2):
            assert not held and BODY not in str(p)
        if who=="c" and (w,turn)==(2,2):
            assert "slip" in held and view["messages"][0]["enclosures"][0]["text"]==BODY
        return json.dumps({"text":"交接处理","messages":[{"recipients":recipients,"text":"请核对副本。","enclosures":["slip"]}] if recipients else []})


def test_only_actual_holders_forward_identical_copies_to_authorized_recipients(tmp_path):
    scene=Scenario11.model_validate({"title":"交接","background":"四个独立岗位。","domains":["货运"],
        "entities":[{"id":k,"name":k,"identity":k,"materials":[{"domain":"货运","text":BODY,"document":{"id":"slip","title":"来料单"},
                      "sharing":{"allowed_recipients":["b","c"]}}] if k=="a" else []} for k in "abcd"],
        "mail_routes":[{"source":"a","target":"b","transit_windows":1}]})
    config=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":3,"rounds_per_window":2})
    g=HandoffGateway();run=Simulation(scene,config,g,tmp_path/"base")
    assert run.run()["status"]=="failed" and run.window==1
    pending=document_history(run,1)
    assert [h["actor"] for h in pending["holders"]]==["a"] and pending["items"][0]["receipt"] is None
    resumed=Simulation.restore(tmp_path/"base",g);resumed.recover()
    assert resumed.run()["status"]=="completed" and sorted(g.denials)==["scope","unheld"]
    finished=document_history(resumed,3)
    assert finished["total"]==2 and {h["actor"] for h in finished["holders"]}=={"a","b","c"}
    assert {r["arrived_window"] for r in finished["items"]}=={2}
    branch=fork_experiment(resumed,ExperimentRequest(window=1,through=2,kind="repeat"),"a"*12,None,tmp_path/"copy")
    assert branch.run()["status"]=="completed"
    assert [h["actor"] for h in document_history(Simulation.restore(tmp_path/"copy",g))["holders"]]==["a","b","c"]
    tampered=deepcopy(resumed.journal.selected(transport_details=False))
    next(e for e in tampered if e["kind"]=="message_dispatched")["data"]["enclosures"][0]["text"]="改写原信"
    with pytest.raises(ValueError,match="原件副本"):
        verify_commits(scene,config,tampered)


def test_source_review_restores_the_private_original_and_enforces_only_entry_deadlines():
    text=('原件标题为《验收联》，正文是“'+BODY+'”开局只有北站持有该原件。北站获准把原件副本寄给南站。'
          '北站不得将原件副本转交给西站。\n\n'
          '| 主体 | 字段 | 候选值 | 最早转入窗口 | 最晚转入窗口 |\n| --- | --- | --- | --- | --- |\n'
          '| 南站 | 签收 | 按期 | 1 | 2 |\n| 南站 | 签收 | 逾期 | 3 | 不限 |')
    src=Source(id="brief",name="交接材料",text=text)
    scene=Scenario11.model_validate({"title":"交接","background":'原件由北站保管，正文为“'+BODY+'”',
        "domains":["交接"],"sources":[src.model_dump()],"entities":[{"id":"north","name":"北站","identity":"发件"},
          {"id":"south","name":"南站","identity":"办理","fields":[{"id":"result","label":"签收","domain":"交接","kind":"string",
            "value":"待办","constraints":{"choices":["待办","按期","逾期"]}}]},{"id":"west","name":"西站","identity":"独立岗位"}]})
    original={"scenario":scene.model_dump(mode="json"),"preparation_records":[{}]}
    reviewed=reconcile(original)
    assert not reviewed["questions"] and reconcile(reviewed)["scenario"]==reviewed["scenario"]
    data=reviewed["scenario"]
    assert BODY not in data["background"] and BODY in original["scenario"]["background"] and data["sources"][0]["text"]==text
    parsed=Scenario11.model_validate(data);material=parsed.entities[0].materials[0]
    assert material.text==BODY and material.sharing.allowed_recipients==["south"]
    state=parsed.initial_state()
    def proposal(before,after):
        return Settlement.model_validate({"reviews":[{"entity_id":"south","field_id":"result","before":before,"after":after,
                                         "reason":"签收记录","action_ids":["e000002"]}],"consequences":[]})
    with pytest.raises(ValueError,match="不能把"):
        settled_state(parsed,state,proposal("待办","按期"),{"e000002"},window=3)
    changed,_=settled_state(parsed,state,proposal("待办","按期"),{"e000002"},window=2)
    kept,_=settled_state(parsed,changed,proposal("按期","按期"),{"e000002"},window=3)
    assert kept["south"]["result"]=="按期"
