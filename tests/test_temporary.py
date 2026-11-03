import json

from chuantuo.engine import Simulation
from chuantuo.experiments import branch_payload
from chuantuo.perception import Perception
from chuantuo.schema import RunConfig11, Scenario11


def test_expiry_wakes_only_a_reader_and_snapshot_rebases_effective_dates(tmp_path):
    secret="应急隔音室可供借用"
    scene=Scenario11.model_validate({"title":"借用时段","background":"两个独立办事处。","domains":["排班"],"entities":[
        {"id":"a","name":"甲","identity":"排班","fields":[{"id":"s","domain":"排班","kind":"string","value":"已录",
         "constraints":{"choices":["待录","已录"],"entry_windows":[{"value":"已录","last":2}]}}]},
        {"id":"b","name":"乙","identity":"协调"}],
        "rules":[{"text":secret,"active_from":2,"active_until":3,"visibility":{"readers":["a"]}}],
        "mail_routes":[{"source":"a","target":"b","transit_windows":n,"active_from":w} for w,n in [(1,2),(3,1),(5,3)]]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":4,"rounds_per_window":1,"max_review_interval":4})
    class Gateway:
        def complete(self,model,messages):
            p=json.loads(messages[-1]["content"])
            if p["task"]=="agent":
                v=p["observation"];who=p["actor_id"]
                assert any(r["text"]==secret for r in v["rules"])==(who=="a" and 2<=p["window"]<=3)
                assert bool(v.get("ended_conditions"))==(who=="a" and p["window"]==4)
                return json.dumps({"text":"继续观察时段。","review_at":p["window"]+3})
            assert bool(p.get("ended_conditions"))==(p["window"]==4)
            return json.dumps({"reviews":[{"entity_id":"a","field_id":"s","before":"已录","after":"已录","reason":"登记保持。"}],"consequences":[]})
    run=Simulation(scene,cfg,Gateway(),tmp_path/"root");assert run.run()["status"]=="completed"
    a=run.journal.decision_context("a",4,1,run._discarded)
    assert a["resumed_plan"]["reason"]=="visible_change" and a["observation"]["ended_conditions"][0]["text"]==secret
    restored=Simulation.restore(tmp_path/"root")
    assert restored.notebooks["a"].records==run.notebooks["a"].records and secret not in str(restored.notebooks["b"].records)
    fresh=Scenario11.model_validate(branch_payload(run,3,None)["scenario"])
    assert not fresh.rules and not fresh.entities[0].fields[0].entry_period("已录").allows(1)
    routes=Perception(fresh,fresh.depths(),cfg);assert [routes.transit("a","b",w) for w in (1,2)]==[1,3]
    earlier=Scenario11.model_validate(branch_payload(run,1,None)["scenario"])
    assert earlier.rules[0].active_from==1 and earlier.rules[0].active_until==2
