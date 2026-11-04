import json
from copy import deepcopy

import pytest

from chuantuo.engine import Simulation
from chuantuo.interactions import reply_chain
from chuantuo.correspondence import requested_replies
from chuantuo.mail_opening import check_opening_inputs, opening_guards, scope_generated_opening
from chuantuo.schema import RunConfig11,Scenario11
from chuantuo.settlement import verify_commits


def test_undelivered_source_text_survives_omitted_model_privacy_labels():
    from pathlib import Path
    data={"title":"稿件","background":"两家机构在排期。","domains":[],"entities":[
        {"id":"a","name":"桦湾排字室","identity":"拟在第一窗寄出“晚潮场”。"},
        {"id":"b","name":"南桥剧场","identity":"负责核对。","preferences":"收到信再决定是否接受“晚潮场”。"}]}
    text=(Path(__file__).resolve().parents[1]/"chuantuo/templates/proof-reply.md").read_text("utf8")
    guards=opening_guards(data["entities"],[{"id":"ms","text":text}])
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"}})
    assert [(g["term"],g["holders"],g["withheld"]) for g in guards]==[("晚潮场",["a"],["b"])]
    with pytest.raises(ValueError,match="当前不符的主体为 b"):
        check_opening_inputs(Scenario11.model_validate(data),cfg,guards)
    before=deepcopy(data)
    data["influences"]=[{"source":"a","target":"b","description":"寄出写有“晚潮场”的信请剧场核对。"}]
    edits=scope_generated_opening(data,guards,set())
    assert len(edits)==2 and "决定是否接受" in data["entities"][1]["preferences"]
    assert edits[1]["original_description"]=="寄出写有“晚潮场”的信请剧场核对。" and "晚潮场" not in data["influences"][0]["description"]
    check_opening_inputs(Scenario11.model_validate(data),cfg,guards)
    assert not scope_generated_opening(before,guards,{id(before["entities"][1])})
    assert not opening_guards(data["entities"],[{"id":"p","text":text,"public_reference":{"url":"https://example.org"}}])
    explicit=opening_guards(data["entities"],[{"id":"spec","text":"开局仅桦湾排字室知道“白桦三号”。"}])
    assert explicit[0]["withheld"]==["b"]


def test_request_deadline_follows_the_return_delivery_and_survives_short_private_history(tmp_path):
    scene=Scenario11.model_validate({"title":"两封问询","background":"三方分别核对来信。","domains":[],
        "entities":[{"id":a,"name":a,"identity":a} for a in "abc"],"mail_routes":[
            {"source":"a","target":"b","transit_windows":1},{"source":"b","target":"a","transit_windows":2}]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":5,"rounds_per_window":1,"history_windows":1})
    class Post:
        def complete(self,model,messages):
            p=json.loads(messages[-1]["content"])
            if p["task"]=="world":return '{"reviews":[],"consequences":[]}'
            who,w=p["actor_id"],p["window"];send=[]
            if who=="a" and w==1:
                send=[{"recipients":["b"],"text":"请核对。","reply_by":3},{"recipients":["b"],"text":"请核对。"},
                      {"recipients":["c"],"text":"单独问蓝帆。","reply_by":"open"}]
            elif who=="b" and w==2:
                first=next(m for m in p["observation"]["messages"] if m.get("reply_by")==3)
                send=[{"recipients":["a"],"text":"暂不能确认。","reply_to":first["receipt_id"]}]
            elif who=="c" and w==5:
                old=next(m for m in p["reply_targets"] if m.get("reply_by")=="open")
                send=[{"recipients":["a"],"text":"已阅。","reply_to":old["receipt_id"]}]
            if who=="b":assert "单独问蓝帆" not in str(p)
            return json.dumps({"text":"处理本窗来信。","messages":send},ensure_ascii=False)
    sim=Simulation(scene,cfg,Post(),tmp_path/"mail")
    assert sim.run()["status"]=="completed"
    restored=Simulation.restore(tmp_path/"mail")
    def to_b(w):return requested_replies(restored,w,"b")["items"][0]
    assert to_b(1)["status"]=="outbound" and to_b(1)["receipt"] is None
    assert to_b(2)["status"]=="returning" and to_b(2)["reply_arrival"] is None
    assert to_b(3)["timing"]=="due"
    assert to_b(4)["status"]=="answered" and to_b(4)["timing"]=="late"
    assert requested_replies(restored,5,stage="answered")["total"]==2


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
