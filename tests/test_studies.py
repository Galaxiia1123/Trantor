import json
import threading
import time

from fastapi.testclient import TestClient

from chuantuo.engine import Simulation, encode
from chuantuo.letter_case import opening, scripted_response
from chuantuo.repository import atomic_json
from chuantuo.schema import RunConfig11
from chuantuo.studies import outcomes_for
from chuantuo.web import create_app


class LetterGateway:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.calls = 0
        self.guard = threading.Lock()

    def complete(self, config, messages):
        with self.guard:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        return encode(scripted_response(json.loads(messages[-1]["content"])))


def until(client, uri, statuses):
    for _ in range(500):
        result = client.get(uri).json()
        if result["status"] in statuses:
            return result
        time.sleep(.01)
    raise AssertionError(result)


def test_partial_budget_and_explicit_restart_share_a_sealed_prefix(tmp_path):
    gateway = LetterGateway()
    config = RunConfig11.model_validate({"model":{"provider":"custom","model":"fixture","base_url":"http://127.0.0.1:1"},
                                          "windows":3,"rounds_per_window":1,"window_duration":"一日"})
    parent_id = "b"*12
    parent = Simulation(opening(),config,gateway,tmp_path/parent_id,_run_id=parent_id)
    assert parent.run()["status"]=="completed"
    original_calls = gateway.calls
    study_id = "a"*32
    launch = f"/api/runs/{parent_id}/studies/"
    path = "/api/studies/"+study_id
    spec = {"experiment":{"window":1,"through":3,"kind":"repeat"}, "watch":{"actor_id":"desk","field_id":"agenda"},
            "repetitions":2,"call_limit":12}
    with TestClient(create_app(tmp_path,gateway)) as client:
        assert client.put("/api/model",json={"model":config.model.model_dump(mode="json")}).is_success
        gateway.entered.clear()
        gateway.release.clear()
        assert client.put(launch+study_id,json=spec).is_success
        try:
            assert gateway.entered.wait(5)
            assert client.put(launch+study_id,json={**spec,"call_limit":14}).status_code==409
            assert client.post(path+"/pause").is_success
        finally:
            gateway.release.set()
        paused = until(client,path,{"paused"})
        assert (paused["calls"],paused["completed"],paused["members"][0]["window"]) == (3,0,2)
        assert client.post("/api/runs/"+paused["members"][0]["run_id"]+"/resume").status_code==409
    # Recover the case where the group receipt lagged the actual committed window.
    file = tmp_path/"_studies"/(study_id+".json")
    record = json.loads(file.read_text("utf-8"))
    record.update(status="running"); record["members"][0].update(status="opening",calls=0,window=1)
    atomic_json(file,record)
    with TestClient(create_app(tmp_path,gateway)) as client:
        found = client.get(path).json()
        assert (found["status"],found["calls"],gateway.calls) == ("interrupted",3,original_calls+3)
        assert client.put(launch+study_id,json=spec).json()["status"]=="interrupted"
        assert client.post(path+"/resume").is_success
        done = until(client,path,{"completed"})
        assert done["calls"]==12 and done["outcomes"][0]["indices"]==[1,2]
        assert gateway.calls==original_calls+12
        small_id="c"*32
        small={**spec,"experiment":{**spec["experiment"],"through":2},"repetitions":3,"call_limit":4}
        assert client.put(launch+small_id,json=small).is_success
        capped=until(client,"/api/studies/"+small_id,{"limit"})
        assert capped["completed"]==1 and capped["calls"]==4 and capped["members"][-1]["status"]=="queued"
        assert capped["outcomes"][0]["indices"]==[1] and gateway.calls==original_calls+16
        paired_id="d"*32
        paired={**small,"compare_original":True,"repetitions":1,"experiment":{**small["experiment"],"kind":"condition","condition":"窗口中有一项新的通知。"}}
        assert client.put(launch+paired_id,json=paired).is_success
        partial=until(client,"/api/studies/"+paired_id,{"limit"})
        assert [(arm["arm"],arm["completed"]) for arm in partial["arms"]]==[("original",1),("changed",0)]
        assert partial["arms"][0]["outcomes"][0]["indices"]==[1] and not partial["arms"][1]["outcomes"]
        assert partial["calls"]==4 and not partial["outcomes"]


def test_numeric_distributions_merge_equivalent_representations_and_keep_unknowns():
    results=[(1,"1"),(1.0,"1.0"),(9007199254740993,"9007199254740993"),(None,None)]
    members=[{"index":i,"calls":1,"status":"completed","outcome":{"status":"tracking","value":v,"numeric_text":text}}
             for i,(v,text) in enumerate(results,1)]
    summary=outcomes_for(members)
    assert [r["indices"] for r in summary["outcomes"]]==[[1,2],[3],[4]]
    assert summary["numeric"]=={"known":3,"unknown":1,"min":"1","max":"9007199254740993","median":"1.0"}
