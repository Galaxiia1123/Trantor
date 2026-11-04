import json
import re

from chuantuo.engine import Simulation
from chuantuo.replay_export import _settings, export_replay
from chuantuo.schema import RunConfig11, Scenario11


def test_readonly_replay_preserves_large_values_but_removes_connection_information(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANTOR_REPLAY_TEST_KEY","synthetic-export-key")
    number=10**40+1
    scene=Scenario11.model_validate({"title":"导出","background":"</script><script>window.INJECTED=1</script>","domains":["计数"],
        "entities":[{"id":"a","name":"观测者","identity":"独立观察","fields":[
            {"id":"count","domain":"计数","kind":"number","value":number}]}]})
    cfg=RunConfig11.model_validate({"model":{"provider":"custom","model":"test","base_url":"https://gateway.example.invalid/v1",
                                            "api_key_env":"TRANTOR_REPLAY_TEST_KEY"},"windows":1,"rounds_per_window":1})
    class Still:
        def complete(self,model,messages):
            p=json.loads(messages[-1]["content"])
            if p["task"]=="agent":return '{"text":"继续观察。","messages":[]}'
            return json.dumps({"reviews":[{"entity_id":"a","field_id":"count","before":number,"after":number,"reason":"保持原值。","action_ids":[]}],"consequences":[]})
    sim=Simulation(scene,cfg,Still(),tmp_path/"run");assert sim.run()["status"]=="completed"
    document=export_replay(sim)
    assert "gateway.example.invalid" not in document and "TRANTOR_REPLAY_TEST_KEY" not in document and "synthetic-export-key" not in document
    assert "</script><script>window.INJECTED" not in document
    assert "Content-Security-Policy" in document and "default-src 'none'" in document
    payload=json.loads(re.search(r'<script id="replay" type="application/json">(.*?)</script>',document,re.S)[1])
    start=payload["routes"][0]["events"][0]["data"]["scenario"]
    assert start["background"]==scene.background and start["entities"][0]["fields"][0]["value"]=={"exact_integer":str(number)}
    early=export_replay(sim,0)
    early_payload=json.loads(re.search(r'<script id="replay" type="application/json">(.*?)</script>',early,re.S)[1])
    assert all(e["window"]==0 for e in early_payload["routes"][0]["events"])
    legacy={"models":{"configs":{"old":{"provider":"custom","model":"old","base_url":"private-url","api_key_env":"OLD_KEY"}},"world":"old"}}
    assert _settings(legacy)["models"]["configs"]["old"]=={"provider":"custom","model":"old"}
