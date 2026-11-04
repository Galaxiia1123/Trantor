import json
from collections import Counter

from chuantuo.engine import Simulation
from chuantuo.models import ModelError
from chuantuo.schema import RunConfig11, Scenario11


def test_round_recovery_reuses_successful_calls_but_keeps_one_shared_observation(tmp_path):
    scene=Scenario11.model_validate({"title":"三方商议","background":"每轮各自决定。","domains":[],
        "entities":[{"id":a,"name":a,"identity":a} for a in "abc"]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":1,"rounds_per_window":2,"output_retries":0})
    class Flaky:
        def __init__(self):
            self.calls=Counter()
            self.available=False
        def complete(self,model,messages):
            p=json.loads(messages[-1]["content"])
            if p["task"]=="world":return '{"reviews":[],"consequences":[]}'
            actor,w,r=p["actor_id"],p["window"],p["round"]
            self.calls[actor,w,r]+=1
            if (actor,r)==("b",2) and not self.available:raise ModelError("Temporary request failure")
            mail=[{"recipients":["b"],"text":"本轮询问。"}] if (actor,r)==("a",1) else []
            if (actor,r)==("b",2):
                ref=p["observation"]["messages"][0]["receipt_id"]
                mail=[{"recipients":["a"],"text":"收到询问，作此回复。","reply_to":ref}]
            return json.dumps({"text":"自主处理第 "+str(r)+" 轮。","messages":mail},ensure_ascii=False)
    gateway=Flaky();directory=tmp_path/"run"
    sim=Simulation(scene,cfg,gateway,directory)
    assert sim.run()["status"]=="failed" and sim.window==0
    first=[e["id"] for e in sim.journal.snapshot({"expression"})]
    obs=[e["id"] for e in sim.journal.snapshot({"observation"})]
    saved={e["data"]["actor_id"]:e["id"] for e in sim.journal.snapshot({"model_output"}) if e["data"].get("purpose")=="agent"}
    restored=Simulation.restore(directory,gateway=gateway)
    assert restored.snapshot()["round_recovery"]["saved"]==2
    restored.recover();assert restored.run()["status"]=="failed"
    restored=Simulation.restore(directory,gateway=gateway)
    gateway.available=True;restored.recover()
    assert restored.run()["status"]=="completed",restored.error
    assert gateway.calls["a",1,2]==gateway.calls["c",1,2]==1 and gateway.calls["b",1,2]==3
    assert [e["id"] for e in restored.journal.snapshot({"observation"})]==obs
    actions=restored.journal.snapshot({"expression"})
    assert [e["id"] for e in actions[:3]]==first and len(actions)==6
    assert all(e["data"]["model_output_id"]==saved[e["data"]["actor_id"]] for e in actions[3:] if e["data"]["actor_id"]!="b")
    assert len(restored.journal.snapshot({"agent_result_reused"}))==4
    assert len(restored.journal.snapshot({"message_delivered"}))==2
    assert Simulation.restore(directory).status=="completed"
