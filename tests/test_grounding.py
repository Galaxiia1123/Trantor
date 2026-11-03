import json
from collections import Counter

import httpx
import pytest
from pydantic import ValidationError

from chuantuo.definition import DefinitionRequest, prepare_definition
from chuantuo.demo import demo_config, demo_scenario, respond
from chuantuo.engine import Simulation
from chuantuo.insights import timeline
from chuantuo.models import ModelConfig, ModelGateway
from chuantuo.schema import RunConfig, Scenario, Source
from chuantuo.settlement import verify_commits


def test_quote_page_is_checked_and_never_broadcast_from_public_field():
    raw = demo_scenario().model_dump(mode="json")
    raw["sources"] = [{"id":"brief","name":"说明", "text":"公开范围。\n仅供核验 PRIVATE_SOURCE。", "pages":[
        {"number":1,"method":"pdf_text","text_start":0,"text_end":5},
        {"number":2,"method":"pdf_text","text_start":6,"text_end":25}]}]
    quote = {"source_id":"brief","page":2,"quote":"PRIVATE_SOURCE"}
    f = raw["entities"][0]["fields"][0]
    f["provenance"] = {"kind":"material","source_ids":["brief"],"quotes":[quote],"explanation":"PRIVATE_RATIONALE"}
    scene = Scenario.model_validate(raw)
    sim = Simulation(scene, demo_config())
    assert "PRIVATE_" not in json.dumps(sim.observation("engineer"),ensure_ascii=False)
    assert sim.scenario.entities[0].fields[0].provenance.quotes[0].quote == "PRIVATE_SOURCE"
    quote["page"] = 1
    with pytest.raises(ValidationError,match="原文"):
        Scenario.model_validate(raw)
    quote["page"] = 15
    with pytest.raises(ValidationError,match="页码"):
        Scenario.model_validate(raw)


def test_long_seed_covers_original_and_stops_before_overspending():
    raw = ("地质组记录：甲班向乙组提供观测，最终结果待定。\n" * 1900) + "独有结尾"
    source = Source(id="document", name="调查记录", text=raw)
    model = ModelConfig(provider="demo", model="offline", context_limit_chars=18500)
    request = DefinitionRequest(goal="观察沟通",sources=[source],model=model,world_model=model,max_source_batches=12)
    class Capture:
        def __init__(self):
            self.calls = []
        def complete(self, config, messages):
            data = json.loads(messages[-1]["content"])
            assert sum(len(m["content"]) for m in messages) <= config.context_limit_chars
            self.calls.append(data)
            return respond(messages)
    limited = Capture()
    with pytest.raises(ValueError, match="调用前"):
        prepare_definition(request.model_copy(update={"max_source_batches":1}), limited)
    assert not limited.calls
    gateway = Capture()
    result = prepare_definition(request, gateway)
    record = result["records"][0]
    assert result["scenario"]["sources"][0]["text"] == raw
    fragments = [seg for trace in record["source_batches"] for seg in trace["segments"] if seg["source_id"] == "document"]
    assert "".join(raw[s["start"]:s["start"]+s["length"]] for s in fragments) == raw
    parser_payload = next(d for d in gateway.calls if d["task"] == "define")
    assert parser_payload["sources"][0]["excerpt_only"]
    assert record["source_coverage"]["forwarded_quotes"] > 0


def test_settlement_repair_reuses_committed_round_and_traces_failure(tmp_path):
    class Repair:
        calls = Counter()
        def complete(self, model, messages):
            p = json.loads(messages[-1]["content"])
            self.calls[p["task"]] += 1
            data = json.loads(respond(messages))
            if p["task"] == "world":
                if self.calls["world"] == 1:
                    self.actions = p["actions"]
                    data["reviews"][0]["before"] = 999
                else:
                    assert self.actions == p["actions"]
                    assert "前值" in p["settlement_correction"]["issue"]
            return json.dumps(data)
    gateway = Repair()
    config = demo_config().model_copy(update={"windows":1,"settlement_retries":1})
    sim = Simulation(demo_scenario(), config, gateway, tmp_path)
    assert sim.run()["status"] == "completed"
    assert gateway.calls == {"agent":10, "world":2}
    assert len(sim.journal.snapshot({"settlement_rejected"})) == 1
    assert Simulation.restore(tmp_path).state == sim.state


def test_http_receipts_distinguish_transport_and_usage_without_credentials(tmp_path):
    def handler(request):
        data = json.loads(request.content)
        return httpx.Response(200, json={"model":"mock-model", "usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10},
            "choices":[{"message":{"content":respond(data["messages"])},"finish_reason":"stop"}]})
    configs = demo_config().model_dump(mode="json")
    configs.update(windows=1, rounds_per_window=1)
    configs["models"]["configs"]["offline"] = {"provider":"custom","model":"fixture","base_url":"http://127.0.0.1/v1"}
    sim = Simulation(demo_scenario(), RunConfig.model_validate(configs), ModelGateway(transport=httpx.MockTransport(handler)),tmp_path)
    assert sim.run()["status"] == "completed"
    attempts = sim.journal.snapshot({"provider_attempt"})
    assert len(attempts) == 6
    assert all(sim.journal.event(e["data"]["input_id"])["kind"] == "model_input" for e in attempts)
    usage = timeline(sim)["provider_usage"]
    assert usage["transports"] == {"mock_http":6} and usage["reported"]["total_tokens"] == 60
    assert all("headers" not in e["data"] for e in attempts)


@pytest.mark.parametrize("corruption", ["state","messages","privacy","rounds"])
def test_audit_detects_semantically_corrupt_but_valid_json(corruption):
    sim = Simulation(demo_scenario(),demo_config().model_copy(update={"windows":1}))
    sim.run()
    events = sim.journal.snapshot()
    if corruption == "state":
        next(e for e in events if e["kind"]=="window_committed")["data"]["state"]["city"]["readiness"] = True
    elif corruption == "messages":
        next(e for e in events if e["kind"]=="message_delivered")["data"]["to"] = "port"
    elif corruption == "privacy":
        next(e for e in events if e["kind"]=="observation" and e["data"]["actor_id"]=="city")["data"]["visible"]["visible_state"]["engineer"] = {"capacity":{"value":3}}
    else:
        next(e for e in events if e["kind"]=="expression")["data"]["round"] = 2
    with pytest.raises(ValueError):
        verify_commits(sim.scenario,sim.config,events)
