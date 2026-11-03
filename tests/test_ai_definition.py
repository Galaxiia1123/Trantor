import json

import httpx
import pytest

from chuantuo.definition import DefinitionRequest, prepare_definition
from chuantuo.demo import demo_config, demo_scenario
from chuantuo.models import ModelConfig, ModelGateway, ModelError
from chuantuo.schema import Scenario, Source


def request(**updates):
    offline = demo_config().models.configs["offline"]
    return DefinitionRequest(goal="观察港口改造协商", model=offline, world_model=offline, **updates)


@pytest.mark.parametrize("bad_output", ["invalid JSON", '{"scenario":{"entities":["wrong shape"]}}'])
def test_draft_creation_correction_estimates_and_revision_preserve_sources(bad_output):
    class Model:
        calls = []
        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            self.calls.append(payload)
            if payload["task"] == "estimate_weights":
                return json.dumps({"estimates": [{"source": a, "target": b, "weight": .55, "reason": "本地测试估计，需审阅"} for a,b in payload["missing"]]})
            if len(self.calls) == 1: return bad_output
            scene = payload["current_scenario"] or demo_scenario().model_dump(mode="json")
            scene["influences"][0]["weight"] = None
            return json.dumps({"summary": "已整理港口协商草案", "scenario": scene,
                               "questions": [{"id": "focus", "question": "侧重协作还是施工？", "options": ["协作", "施工"]}], "changes": []})
    model = Model()
    first = prepare_definition(request(sources=[Source(id="doc",name="原始资料",text="原文保持不变")]), model)
    assert first["scenario"]["sources"][0]["text"] == "原文保持不变"
    assert first["scenario"]["influences"][0]["weight"] == .55
    assert first["scenario"]["influences"][0]["provenance"]["kind"] == "ai_assumption"
    assert len(first["records"][0]["attempts"]) == 2
    assert "output_correction" in model.calls[1]
    assert first["requires_review"]
    assert "侧重协作还是施工？" in first["scenario"]["missing_information"]
    scenario = Scenario.model_validate(first["scenario"])
    second = prepare_definition(request(current_scenario=scenario, revision="强调社区协作",
                                      answers=[{"question":"侧重协作还是施工？","answer":"协作"}]), model)
    assert any("协作" in source["text"] for source in second["scenario"]["sources"])
    assert any(source["id"] == "doc" for source in second["scenario"]["sources"])


def test_existing_user_weights_not_overwritten_by_definition_model():
    class Model:
        def complete(self, config, messages):
            scene = demo_scenario().model_dump(mode="json")
            scene["influences"][0]["weight"] = .01
            return json.dumps({"summary":"草案", "scenario":scene, "questions":[], "changes":[]})
    result = prepare_definition(request(current_scenario=demo_scenario(), revision="补充身份说明"), Model())
    assert result["scenario"]["influences"][0]["weight"] == .8


def test_parser_weight_guesses_use_world_estimator_and_failure_keeps_reviewable_draft():
    class Model:
        calls = []
        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            self.calls.append(payload["task"])
            if payload["task"] == "estimate_weights":
                raise ModelError("test_unavailable", "本地测试的估权失败")
            return json.dumps({"summary":"待审阅草案", "scenario":demo_scenario().model_dump(mode="json")})
    model = Model()
    result = prepare_definition(request(), model)
    assert model.calls == ["define", "estimate_weights"]
    assert all(edge["weight"] is None for edge in result["scenario"]["influences"])
    assert any("估算失败" in warning for warning in result["warnings"])
    assert result["requires_review"]


def test_definition_sends_explicit_visual_pages_and_retains_audit_without_binary(tmp_path):
    from chuantuo.demo import respond
    from chuantuo.intake import MaterialStore
    from test_intake import picture_bytes

    store = MaterialStore(tmp_path, ocr=lambda image: ("HARBOR PLAN 2026", .99))
    source = store.ingest("source.png", picture_bytes()[1])
    class Model:
        def complete(self, config, messages):
            content = messages[-1]["content"]
            if isinstance(content, list):
                assert config.supports_images
                assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
                payload = json.loads(content[0]["text"])
                assert payload["visual_input"] == [{"source_id":source.id,"page":1}]
                messages = [{"role":"user", "content":content[0]["text"]}]
            return respond(messages)
    definition_request = request(sources=[source])
    definition_request = definition_request.model_copy(update={"model": definition_request.model.model_copy(update={"supports_images":True})})
    result = prepare_definition(definition_request, Model(), store)
    assert result["visual_pages"] == 1
    assert result["scenario"]["sources"][0]["sha256"] == source.sha256
    assert "base64," not in json.dumps(result["records"])


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_explicit_vision_wire_format_preserves_model_and_isolation(provider, monkeypatch):
    monkeypatch.setenv("VISION_TEST_KEY", "headers-only-key")
    image = "data:image/png;base64,aGVsbG8="
    messages = [{"role":"system","content":"Read the supplied image."}, {"role":"user","content":[
        {"type":"text","text":"What is shown?"}, {"type":"image_url","image_url":{"url":image}}]}]
    before = json.dumps(messages)
    def handler(http_request):
        body = json.loads(http_request.content)
        assert body["model"] == "explicit-model"
        assert "headers-only-key" not in http_request.content.decode()
        if provider == "anthropic":
            assert body["messages"][0]["content"][1]["source"]["media_type"] == "image/png"
            return httpx.Response(200, json={"content":[{"type":"text","text":"description"}]})
        assert body["messages"][1]["content"][1]["image_url"]["url"] == image
        return httpx.Response(200, json={"choices":[{"message":{"content":"description"}}]})
    model = ModelConfig(provider=provider, model="explicit-model", api_key_env="VISION_TEST_KEY", supports_images=True)
    assert ModelGateway(transport=httpx.MockTransport(handler)).complete(model,messages) == "description"
    assert json.dumps(messages) == before
    with pytest.raises(ModelError, match="invalid_messages"):
        ModelGateway().complete(model.model_copy(update={"supports_images":False}),messages)
