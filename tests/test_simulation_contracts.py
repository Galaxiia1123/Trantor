import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from chuantuo.demo import demo_config, demo_scenario, example_scenario, respond
from chuantuo.engine import Simulation
from chuantuo.experiments import Adoption, adopt_suggestion
from chuantuo.models import ModelConfig, ModelGateway
from chuantuo.schema import RunConfig, Scenario, StateField
from chuantuo.services import generate_report, plan_report


def test_report_budget_prevents_calls_and_only_reuses_exact_verified_batches(tmp_path):
    class Count:
        def __init__(self): self.counts = Counter()
        def complete(self, model, messages):
            self.counts[json.loads(messages[-1]["content"])["task"]] += 1
            return respond(messages)
    data=demo_config().model_dump(mode="json")
    data["windows"]=2
    data["models"]["report"]="small"
    data["models"]["configs"]["small"]={"provider":"demo","model":"batch-fixture","context_limit_chars":9000}
    sim=Simulation(demo_scenario(),RunConfig.model_validate(data),directory=tmp_path)
    sim.run()
    gateway=Count()
    plan=plan_report(sim)
    assert plan["first_level_batches"]>1 and plan["minimum_new_calls"]>1
    with pytest.raises(ValueError,match="上限"):
        generate_report(sim,gateway,max_calls=1)
    assert not gateway.counts and not sim.journal.snapshot({"report_batch_input"})
    first=generate_report(sim,gateway)
    assert first["coverage"]["reused_batches"]==0
    assert first["coverage"]["new_model_calls"]==sum(gateway.counts.values())
    reopened=Simulation.restore(tmp_path)
    assert plan_report(reopened)["minimum_new_calls"]==1
    second=generate_report(reopened,gateway,max_calls=1)
    assert second["coverage"]["new_model_calls"]==1
    assert second["coverage"]["reused_batches"]==first["coverage"]["batches"]
    assert first["record_id"]!=second["record_id"]
    with pytest.raises(ValueError,match="上限"):
        generate_report(reopened,gateway,max_calls=1,reuse=False)
    reopened.journal.append("runtime_verification",{"source":"scope changed"},reopened.window)
    assert plan_report(reopened)["reusable_first_level"]==0


def test_evidence_type_and_source_cannot_be_invented_in_report_claim():
    class Bad:
        calls=0
        def complete(self,cfg,messages):
            self.calls+=1
            return json.dumps({"title":"fixture","sections":[{"title":"a","text":"b","evidence_ids":["e000001"],
              "claims":[{"basis":"simulation","text":"not observed","evidence_ids":["e000001"]}]}]})
    sim=Simulation(demo_scenario(),demo_config().model_copy(update={"windows":1}))
    sim.run()
    gateway=Bad()
    with pytest.raises(ValueError,match="已提交"):
        generate_report(sim,gateway)
    assert gateway.calls==3 and not sim.journal.snapshot({"report_created"})


def test_world_suggestions_become_typed_fields_only_in_explicit_new_project(tmp_path):
    sim=Simulation(example_scenario("release"),demo_config(),directory=tmp_path)
    sim.run()
    proposal={"window":1,"suggestion_index":0,"entity_id":"tester","field":{"id":"blocked_steps","domain":"无障碍测试","kind":"number","value":None}}
    raw=(tmp_path/"events.jsonl").read_bytes()
    result=adopt_suggestion(sim,Adoption.model_validate(proposal))
    derived=Scenario.model_validate(result["scenario"])
    f=next(a for a in derived.entities if a.id=="tester").fields[-1]
    assert (f.id,f.value,f.provenance.kind)==("blocked_steps",None,"user")
    assert not f.visibility.public and "建议" in f.provenance.explanation
    assert (tmp_path/"events.jsonl").read_bytes()==raw and "blocked_steps" not in sim.state["tester"]
    with pytest.raises(ValueError,match="同编号"):
        adopt_suggestion(sim,Adoption.model_validate({**proposal,"field":{**proposal["field"],"id":"available_sessions"}}))


@pytest.mark.parametrize("bad",["wrong_source","reversed","absent_edge"])
def test_direct_attribution_rejects_false_direction_or_authorship(bad):
    class Change:
        def complete(self,config,messages):
            payload=json.loads(messages[-1]["content"])
            result=json.loads(respond(messages))
            if payload["task"]=="world":
                ref=result["reviews"][0]["influence_refs"][0]
                if bad=="wrong_source":
                    ref["action_ids"]=[next(a["id"] for a in payload["actions"] if a["actor_id"]=="port")]
                    result["reviews"][0]["action_ids"]+=ref["action_ids"]
                elif bad=="reversed":ref["source"],ref["target"]=ref["target"],ref["source"]
                else:ref["source"]="company"
            return json.dumps(result)
    sim=Simulation(demo_scenario(),demo_config().model_copy(update={"windows":1}),Change())
    assert sim.run()["status"]=="failed" and sim.state==sim.scenario.initial_state()


@pytest.mark.parametrize("rule,value,allowed",[
    ({"minimum":0,"maximum":1},1,True),({"minimum":0,"maximum":1},2,False),
    ({"allow_unknown":False},None,False),({"minimum":0},None,True),
    ({"choices":["x","y"]},"x",True),({"choices":["x","y"]},"z",False)])
def test_user_defined_field_constraints(rule,value,allowed):
    base={"id":"field","domain":"custom","kind":"string" if "choices" in rule else "number","value":value,"constraints":rule}
    if allowed:
        assert StateField.model_validate(base).value==value
    else:
        with pytest.raises(ValidationError):
            StateField.model_validate(base)


def test_fixed_field_still_reviewed_and_cannot_be_changed():
    raw=demo_scenario().model_dump(mode="json")
    raw["entities"][0]["fields"][0]["constraints"]={"fixed":True}
    sim=Simulation(Scenario.model_validate(raw),demo_config())
    assert sim.run()["status"]=="failed" and "readiness" in sim.error
    assert sim.window==0


def test_reused_http_connection_preserves_request_and_cookie_isolation(monkeypatch):
    ports, requests, cookies, keys = [], [], [], []
    class Handler(BaseHTTPRequestHandler):
        protocol_version="HTTP/1.1"
        def do_POST(self):
            ports.append(self.client_address[1])
            requests.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            cookies.append(self.headers.get("Cookie"))
            keys.append(self.headers.get("Authorization"))
            body=b'{"choices":[{"message":{"content":"ok"}}]}'
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Set-Cookie","actor_private=one; Path=/")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    client=ModelGateway()
    try:
        for n in range(2):
            monkeypatch.setenv("LOOPBACK_FIXTURE_KEY","mock-key-"+str(n))
            cfg=ModelConfig(provider="custom",model="local",api_key_env="LOOPBACK_FIXTURE_KEY",base_url=f"http://127.0.0.1:{server.server_port}/v1")
            assert client.complete(cfg,[{"role":"user","content":"actor-"+str(n)}])=="ok"
        assert len(set(ports))==1 and cookies==[None,None]
        assert keys==["Bearer mock-key-0","Bearer mock-key-1"]
        assert "actor-0" not in json.dumps(requests[1]) and "mock-key" not in json.dumps(requests)
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()
