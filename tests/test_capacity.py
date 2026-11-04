import json
from copy import deepcopy

import pytest

from chuantuo.capacity import capacity_use
from chuantuo.engine import Simulation
from chuantuo.schema import RunConfig11, Scenario11
from chuantuo.source_capacity import apply_capacity_tables
from chuantuo.source_contract import _resolve_read_copies, _reader_named


def shared_scene():
    return {"title":"分用","background":"两处分别申请。","domains":["资源"],
        "entities":[{"id":"s","name":"站点","identity":"分配","fields":[
            {"id":"supply","domain":"资源","kind":"number","unit":"桶","value":6,"constraints":{"fixed":True},"visibility":{"public":True}}]},
            *[{"id":a,"name":a,"identity":"独立申请","fields":[{"id":"taken","domain":"资源","kind":"number","unit":"桶","value":0}]} for a in "ab"]],
        "capacity_pools":[{"id":"water","name":"共用量","limit":{"entity_id":"s","field_id":"supply"},
            "members":[{"entity_id":a,"field_id":"taken"} for a in "ab"],"visibility":{"public":True}}]}


def test_capacity_rejection_is_atomic_and_private_values_stay_private(tmp_path):
    scene=Scenario11.model_validate(shared_scene())
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":1,"rounds_per_window":1,"settlement_retries":0})
    class Requests:
        fair=False
        def complete(self,model,messages):
            p=json.loads(messages[-1]["content"])
            if p["task"]=="world":
                return json.dumps({"reviews":[{"entity_id":a,"field_id":"taken","before":0,"after":3 if self.fair else 4,
                    "reason":"站点实际发放。","action_ids":[x["id"] for x in p["actions"] if x["actor_id"]=="s"]} for a in "ab"],"consequences":[]})
            if p["actor_id"] in "ab":
                assert "余量" not in " ".join(p["observation"].get("shared_limits",[]))
                assert p["observation"]["state"].keys()=={p["actor_id"],"s"}
            return '{"text":"各自申请，站点据当班容量实际办理。","messages":[]}'
    gateway=Requests();sim=Simulation(scene,cfg,gateway,tmp_path/"test")
    assert sim.run()["status"]=="failed" and "共享上限" in sim.error
    assert sim.state==scene.initial_state() and not sim.journal.snapshot({"window_committed"})
    actions=[e["id"] for e in sim.journal.snapshot({"expression"})]
    gateway.fair=True;sim.recover();assert sim.run()["status"]=="completed"
    assert [e["id"] for e in sim.journal.snapshot({"expression"})]==actions
    restored=Simulation.restore(tmp_path/"test")
    assert capacity_use(scene.capacity_pools[0],restored.state)["remaining"]=="0"


def test_unknown_allocation_is_not_zero_and_large_decimal_totals_are_exact():
    raw=shared_scene();raw["entities"][1]["fields"][0]["value"]=3;raw["entities"][2]["fields"][0]["value"]=None
    scene=Scenario11.model_validate(raw);use=capacity_use(scene.capacity_pools[0],scene.initial_state())
    assert use["remaining"] is None and use["known_used"]=="3" and use["unknown"]==1
    invalid=deepcopy(raw);invalid["entities"][1]["fields"][0]["value"]=7
    with pytest.raises(ValueError,match="超过"):Scenario11.model_validate(invalid)
    large=10**40;raw["entities"][0]["fields"][0]["value"]=large
    raw["entities"][1]["fields"][0]["value"]=large;raw["entities"][2]["fields"][0]["value"]=1
    with pytest.raises(ValueError,match="超过 1"):Scenario11.model_validate(raw)
    document=shared_scene();document.pop("capacity_pools")
    document["sources"]=[{"id":"limits","text":"| 共享上限 | 容量字段 | 占用字段 |\n| --- | --- | --- |\n| 共用量 | s.supply | a.taken、b.taken |"}]
    review={};apply_capacity_tables(document,review)
    assert not review["capacity_review"]["unresolved"] and document["capacity_pools"][0]["limit"]=={"entity_id":"s","field_id":"supply"}
    original=shared_scene();original["entities"][0]["name"]="林湾水站";original["entities"][1]["name"]="西坡菜园"
    original["sources"]=[{"id":"reading","text":"水站有权查阅两方的实领水量。"}]
    original["entities"][1]["fields"][0]["label"]="实领水量"
    copied={"id":"copy","label":"西坡菜园实领水量（查阅）","domain":"资源","kind":"number","value":999,
            "provenance":{"kind":"material","quotes":[{"source_id":"reading","quote":"水站有权查阅两方的实领水量"}]}}
    original["entities"][0]["fields"].append(copied);record={}
    _resolve_read_copies(original,record)
    assert len(original["entities"][0]["fields"])==1
    target=original["entities"][1]["fields"][0]
    assert target["value"]==0 and target["visibility"]["readers"]==["s"]
    assert _reader_named("s","水站有权查阅两方的实领水量",original["entities"])
