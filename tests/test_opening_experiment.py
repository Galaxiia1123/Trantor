import json

import pytest

from chuantuo.contrast import field_contrast
from chuantuo.engine import Simulation
from chuantuo.experiments import ExperimentRequest, experiment_comparison, fork_experiment, prepare_experiment
from chuantuo.paired_notes import brief_markdown, paired_brief
from chuantuo.schema import RunConfig11, Scenario11


def test_opening_edit_is_private_from_first_decision_and_stays_fixed_on_restore(tmp_path):
    scene=Scenario11.model_validate({"title":"委托","background":"两家工坊分别决定如何排期。","domains":["印刷"],
        "entities":[{"id":"a","name":"编辑部","identity":"签样","fields":[{"id":"use","domain":"印刷","kind":"string",
                     "value":"一种用途","constraints":{"fixed":True,"choices":["一种用途","备用用途"]}}]},
                    {"id":"b","name":"印坊","identity":"排期"}]})
    config=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":1,"rounds_per_window":1})
    class Quiet:
        def complete(self, model, messages):
            data=json.loads(messages[-1]["content"])
            if data["task"]=="world":
                assert data["review_fields"]=={"a":[],"b":[]}
                return '{"reviews":[],"consequences":[]}'
            return '{"text":"等候本轮安排。","messages":[]}'
    original=Simulation(scene,config,Quiet(),tmp_path/"base")
    assert original.run()["status"]=="completed"
    args={"window":0,"through":1,"assignment":{"entity_id":"a","field_id":"use","value":"备用用途"}}
    with pytest.raises(ValueError,match="固定"):
        prepare_experiment(original,ExperimentRequest(kind="field",**args))
    with pytest.raises(ValueError,match="原有的可见范围"):
        ExperimentRequest(kind="opening",audience="public",**args)
    child=fork_experiment(original,ExperimentRequest(kind="opening",**args),"b"*12,None,tmp_path/"variant")
    assert child.state["a"]["use"]=="备用用途" and original.state["a"]["use"]=="一种用途"
    assert experiment_comparison(original,child,0)["first_state_difference"]==0
    assert field_contrast(original,child,"a","use",0)["opening"]["values"]==["一种用途","备用用途"]
    assert child.run()["status"]=="completed"
    for e in child.journal.snapshot({"model_input"}):
        if e["data"]["purpose"]=="agent":
            assert ("备用用途" in e["data"]["messages"][-1]["content"]) == (e["data"]["actor_id"]=="a")
    restored=Simulation.restore(tmp_path/"variant")
    assert restored.state==child.state and restored.journal.settlement(1)["data"]["reviews"][0]["after"]=="备用用途"
    at_open=paired_brief(original,restored,"a","use",0)
    assert [row["window"] for row in at_open["points"]]==[0] and sum(at_open["counts"].values())==0
    notes=brief_markdown(at_open)
    assert original.run_id+"/e000001" in notes and restored.run_id+"/e000001" in notes
    assert "一种用途" in notes and "备用用途" in notes
