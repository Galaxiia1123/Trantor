import json
from copy import deepcopy

import pytest

from chuantuo.engine import Simulation
from chuantuo.interactions import reply_chain
from chuantuo.schema import RunConfig11,Scenario11
from chuantuo.settlement import verify_commits


def test_replies_name_the_exact_received_letter_and_survive_delivery_and_replay(tmp_path):
    scene=Scenario11.model_validate({"title":"通信","background":"三方独立。","domains":[],
        "entities":[{"id":a,"name":a,"identity":a} for a in "abc"],"mail_routes":[{"source":"b","target":"a","transit_windows":1}]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":2,"rounds_per_window":2})
    denied=set()
    class Mail:
        def complete(self,model,messages):
            p=json.loads(messages[-1]["content"])
            if p["task"]=="world":return json.dumps({"reviews":[],"consequences":[]})
            who,w,r=p["actor_id"],p["window"],p["round"];letters=p["observation"]["messages"];send=[]
            if (who,w,r)==("a",1,1):
                send=[{"recipients":["b"],"text":"同一正文"}]*2
            elif (who,w,r)==("b",1,2):
                assert len(letters)==2 and letters[0]["receipt_id"]!=letters[1]["receipt_id"]
                if "output_correction" in p:denied.add("forward")
                send=[{"recipients":["a" if "output_correction" in p else "c"],"text":"答复第二封","reply_to":letters[1]["receipt_id"]}]
            elif (who,w,r)==("c",1,2):
                assert not letters and not p.get("reply_targets")
                if "output_correction" in p:denied.add("outsider")
                else:
                    stolen=run.journal.snapshot({"message_delivered"})[0]["id"]
                    send=[{"recipients":["a"],"text":"越权尝试","reply_to":stolen}]
            elif (who,w,r)==("a",2,1):
                send=[{"recipients":["b"],"text":"收到答复","reply_to":letters[0]["receipt_id"]}]
            return json.dumps({"text":"处理来信","messages":send})
    run=Simulation(scene,cfg,Mail(),tmp_path);assert run.run()["status"]=="completed" and denied=={"forward","outsider"}
    first,second,reply,last=run.journal.snapshot({"message_delivered"})
    assert reply["data"]["reply_to"]==second["id"] and last["data"]["reply_to"]==reply["id"]
    assert reply_chain(run,first["id"])["total"]==1
    early=reply_chain(run,second["id"],1)["items"];assert len(early)==2 and early[-1]["received_window"] is None
    late=reply_chain(Simulation.restore(tmp_path),last["id"])
    assert late["root"]==second["id"] and [m["depth"] for m in late["items"]]==[0,1,2]
    corrupt=deepcopy(run.journal.selected(transport_details=False))
    next(e for e in corrupt if e["id"]==last["id"])["data"]["reply_to"]=first["id"]
    with pytest.raises(ValueError,match="投递"):verify_commits(scene,cfg,corrupt)
